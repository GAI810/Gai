"""邮箱自动入库：从飞书邮箱读取 BOSS 直聘等平台的简历邮件，自动入库到飞书招聘

复用 hire_import 的解析 / 去重 / 选岗 / 建投递 / 打开人才页能力。

用法：
  .venv/bin/python scripts/mail_import.py            # 扫描并入库（默认最近 20 封，可用 --limit 调整）
  .venv/bin/python scripts/mail_import.py --dry-run  # 只看会处理哪些，不写入
  .venv/bin/python scripts/mail_import.py --limit 50 # 扫描更多邮件（本机可调；测试报告「最近 60 封」多为本机改过）
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import hire_import as h  # noqa: E402
from lib.state_store import read_json, update_json, file_lock  # noqa: E402

MAIL_LOCK = PROJECT_DIR / "data" / "mail_process.lock"

DATA_DIR = PROJECT_DIR / "data"
MAIL_STATE = DATA_DIR / "mail_state.json"
MAIL_LOG = DATA_DIR / "logs" / "mail_import.log"
MAIL_DOWNLOADS = DATA_DIR / "mail_downloads"
ARCHIVE_DIR = DATA_DIR / "mail_archive"

SUPPORTED = {".pdf", ".doc", ".docx"}
BOSS_SOURCE_ID = "18"
SOURCE_MAP = {
    "bosszhipin.com": ("18", "BOSS直聘"),
    "zhipin.com": ("18", "BOSS直聘"),
    "liepin.com": ("4", "猎聘"),
    "lietou-edm.com": ("4", "猎聘"),
    "lietou.com": ("4", "猎聘"),
    "51job.com": ("3", "前程无忧"),
    "zhaopin.com": ("2", "智联招聘"),
}


def source_for(sender: str) -> tuple[str, str]:
    domain = (sender or "").rsplit("@", 1)[-1].lower()
    for d, pair in SOURCE_MAP.items():
        if domain == d or domain.endswith("." + d):
            return pair
    return ("null", "其他")
DEFAULT_SENDERS = ("bosszhipin.com", "zhipin.com", "liepin.com", "51job.com", "zhaopin.com",
                   "lietou-edm.com", "lietou.com")
JOB_TEXT_RE = re.compile(r"应聘\s*([^|｜【\n]{2,40})")


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    MAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with MAIL_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


class LarkCallError(Exception):
    """邮箱 API 调用失败：区分权限 / 网络 / 一般调用错误。"""

    def __init__(self, kind: str, message: str, detail: str = ""):
        self.kind = kind  # permission | network | call
        self.detail = detail
        super().__init__(message)


def _classify_lark_failure(returncode: int, stdout: str, stderr: str) -> LarkCallError:
    blob = f"{stdout or ''}\n{stderr or ''}"
    low = blob.lower()
    # 常见权限码与文案
    perm_markers = (
        "permission", "scope", "access denied", "没有权限", "权限不足",
        "app_scope_not_applied", "99991663", "99991664", "99991668",
        "99991672", "99991679", "missing_scopes", "permission_violations",
    )
    if any(m.lower() in low if m.isascii() else m in blob for m in perm_markers):
        missing = ""
        for token in ("mail:user_mailbox.message:readonly", "mail:user_mailbox.message.attachment:readonly",
                      "mail:user_mailbox", "user_mailbox"):
            if token in blob:
                missing = token
                break
        tip = f"（疑似缺少邮件权限{('：' + missing) if missing else ''}，请按 INSTALL 一次申请齐全）"
        return LarkCallError("permission", f"邮件接口权限不足{tip}", blob[:300])
    net_markers = ("timed out", "timeout", "connection", "network", "临时", "无法连接",
                   "name or service not known", "could not resolve", "connection refused")
    if returncode < 0 or any(m in low for m in net_markers):
        return LarkCallError("network", "邮件接口网络/调用超时失败", blob[:300])
    return LarkCallError("call", f"邮件接口调用失败（exit={returncode}）", blob[:300])


def lark(args: list[str], *, allow_empty: bool = False) -> dict:
    """调用 lark-cli；失败时抛 LarkCallError，不再静默返回 {{}}。"""
    try:
        proc = subprocess.run(["lark-cli", *args], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as e:
        raise LarkCallError("network", f"lark-cli 超时: {' '.join(args[:3])}") from e
    except FileNotFoundError as e:
        raise LarkCallError("call", "未找到 lark-cli，请先安装并登录") from e
    out = proc.stdout or ""
    err = proc.stderr or ""
    start = out.find("{")
    end = out.rfind("}")
    parsed = None
    if start >= 0 and end > start:
        try:
            parsed = json.loads(out[start:end + 1])
        except json.JSONDecodeError:
            parsed = None
    if parsed is None and err:
        s2, e2 = err.find("{"), err.rfind("}")
        if s2 >= 0 and e2 > s2:
            try:
                parsed = json.loads(err[s2:e2 + 1])
            except json.JSONDecodeError:
                parsed = None
    if proc.returncode != 0:
        # 有 JSON 时也先看是否权限类
        if isinstance(parsed, dict) and (
            (parsed.get("error") or {}).get("permission_violations")
            or (parsed.get("error") or {}).get("missing_scopes")
            or not parsed.get("ok", True)
        ):
            raise _classify_lark_failure(proc.returncode, out, err)
        raise _classify_lark_failure(proc.returncode, out, err)
    if parsed is None:
        if allow_empty:
            return {}
        raise LarkCallError("call", f"lark-cli 无有效 JSON: {' '.join(args[:3])}", (err or out)[:300])
    # 业务层权限失败（exit=0 但 body 报错）
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        err_obj = parsed.get("error") or {}
        if err_obj.get("permission_violations") or err_obj.get("missing_scopes"):
            raise _classify_lark_failure(1, out, err)
        msg = str(err_obj.get("message") or parsed)
        low = msg.lower()
        if any(x in low for x in ("scope", "permission", "access denied")) or "权限" in msg:
            raise LarkCallError("permission", f"邮件接口权限不足：{msg[:120]}", msg[:300])
    return parsed


def load_state() -> dict:
    return read_json(MAIL_STATE, default={"processed": []}) or {"processed": []}


def save_state(state: dict) -> None:
    state["processed"] = state["processed"][-2000:]
    update_json(MAIL_STATE, lambda d: d.update(state), default={})


def job_from_subject(subject: str) -> str | None:
    m = JOB_TEXT_RE.search(subject or "")
    return m.group(1).strip() if m else None


def list_messages(limit: int, known: set | None = None, max_pages: int = 3) -> list[str]:
    """按需翻页：直到遇到已处理过的邮件或达到页数上限。

    调用失败会抛 LarkCallError（由 main 区分权限/网络）；真正空箱返回 []。
    """
    known = known or set()
    out: list[str] = []
    token = None
    for _ in range(max_pages):
        params = {"folder_id": "INBOX", "page_size": min(limit, 20)}
        if token:
            params["page_token"] = token
        result = lark(["mail", "user_mailbox.messages", "list", "--as", "user",
                       "--user-mailbox-id", "me", "--params", json.dumps(params)])
        data = result.get("data") or {}
        items = data.get("items") or []
        out.extend(items)
        token = data.get("page_token")
        if not data.get("has_more") or not token:
            break
        if items and all(m in known for m in items):
            break
    return out


def fetch_messages(message_ids: list[str]) -> list[dict]:
    if not message_ids:
        return []
    result = lark(["mail", "+messages", "--message-ids", ",".join(message_ids)])
    return ((result.get("data") or {}).get("messages")) or []


def download_attachment(message_id: str, attachment: dict, target: Path) -> bool:
    result = lark(["mail", "user_mailbox.message.attachments", "download_url", "--as", "user",
                   "--user-mailbox-id", "me",
                   "--params", json.dumps({"message_id": message_id,
                                           "attachment_ids": [attachment["id"]]})])
    urls = ((result.get("data") or {}).get("download_urls")) or []
    if not urls:
        return False
    url = urls[0].get("download_url")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(["curl", "-sSL", "--max-time", "120", "-o", str(target), url],
                              capture_output=True, timeout=150)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0 and target.exists() and target.stat().st_size > 0


def pick_job(text: str, name: str, applied: str | None, years: float) -> dict | None:
    """复用 hire_import 的统一决策函数"""
    decision = h.decide_job(text, name, applied_job=applied, llm_match=h.llm_enabled())
    return decision



# 系统通知关键词（Boss「邮箱设置」等）——误提醒抑制
_SYSTEM_SUBJECT_RE = re.compile(
    r"(邮箱设置|邮件设置|绑定邮箱|换绑|验证码|安全提醒|登录提醒|密码|订阅通知|系统通知)",
    re.I,
)
_RESUME_NOTICE_RE = re.compile(
    r"(简历|附件简历|候选人|应聘|投递|推荐人才|新的简历|查看简历|下载简历)",
    re.I,
)


def classify_platform_mail(subject: str, msg: dict | None = None) -> str:
    """平台域名 + 无附件邮件三类：resume_notice / system / other。

    只有 resume_notice 才提醒「需手动下载」；system 静默；other 只记日志。
    """
    subj = subject or ""
    body = ""
    if isinstance(msg, dict):
        body = str(msg.get("body_html") or msg.get("body_plain") or msg.get("snippet") or "")[:500]
    blob = f"{subj}\n{body}"
    if _SYSTEM_SUBJECT_RE.search(subj) or _SYSTEM_SUBJECT_RE.search(body):
        # 若同时强匹配简历，仍按简历通知（避免误杀）
        if _RESUME_NOTICE_RE.search(subj) and not re.search(r"(邮箱设置|邮件设置|绑定邮箱)", subj):
            return "resume_notice"
        return "system"
    if _RESUME_NOTICE_RE.search(blob):
        return "resume_notice"
    return "other"


def process_message(msg: dict, dry_run: bool, done_atts: set,
                    fail_counts: dict | None = None) -> tuple[int, int, bool]:
    """返回 (成功数, 失败数, 是否已处理完)"""
    mid = msg.get("message_id") or msg.get("id") or ""
    subject = msg.get("subject") or ""
    sender = (msg.get("head_from") or {}).get("mail_address", "")
    attachments = [a for a in (msg.get("attachments") or [])
                   if Path(a.get("filename", "")).suffix.lower() in SUPPORTED]
    if not attachments:
        domain = sender.rsplit("@", 1)[-1].lower()
        is_platform = any(domain == d or domain.endswith("." + d) for d in DEFAULT_SENDERS)
        if is_platform and not dry_run:
            kind = classify_platform_mail(subject, msg)
            if kind == "system":
                # Boss 邮箱设置等系统通知：静默或只记日志，不弹提醒
                log(f"· 系统通知已忽略：[{sender}] {subject[:40]}")
            elif kind == "resume_notice":
                log(f"⚠ 无附件简历邮件：[{sender}] {subject[:40]}（请在平台上下载后放入 待整理下载）")
                h.notify("有简历需要手动下载", f"{subject[:30]}（该平台邮件无附件）", sound=False)
            else:
                log(f"· 平台邮件无附件（未识别为简历通知）：[{sender}] {subject[:40]}")
        return 0, 0, True

    job_hint = job_from_subject(subject)
    done = 0
    failed = 0
    for att in attachments:
        att_key = f"{mid}|{att.get('id')}"
        if att_key in done_atts:
            continue
        raw_name = Path(att.get("filename") or "resume.pdf").name
        fname = re.sub(r"[^\w\u4e00-\u9fa5.\-（）【】]", "_", raw_name)[:80] or "resume.pdf"
        target = MAIL_DOWNLOADS / f"{att.get('id') or 'att'}_{os.getpid()}_{fname}"
        display_name = fname
        if dry_run:
            log(f"[dry-run] 将下载: {fname}（{sender}）主题={subject[:30]} 岗位提示={job_hint}")
            done += 1
            continue

        if not download_attachment(mid, att, target):
            log(f"✗ 下载失败: {fname}")
            failed += 1
            if fail_counts is not None:
                fail_counts[att_key] = fail_counts.get(att_key, 0) + 1
            continue

        final_path = target.with_name(fname)
        try:
            target.replace(final_path)
        except OSError:
            final_path = target
        target = final_path
        src_id, src_name = source_for(sender)
        kind, detail = h.import_file(target, source_id=src_id)
        if kind == "portfolio":
            pf = ARCHIVE_DIR / "作品集"
            pf.mkdir(parents=True, exist_ok=True)
            try:
                target.rename(pf / target.name)
            except OSError:
                pass
            log(f"📁 作品集，已归档: {fname}")
            done_atts.add(att_key)
            done += 1
            continue
        if kind == "fail":
            attempts = (fail_counts or {}).get(att_key, 0) + 1
            if fail_counts is not None:
                fail_counts[att_key] = attempts
            log(f"✗ 入库失败({attempts}/3): {fname} — {detail.get('reason')}")
            if attempts >= 3:
                log(f"⏭ 放弃该附件（已重试 3 次）: {fname}")
                done_atts.add(att_key)
            failed += 1
            continue

        text = ""
        try:
            from lib.resume_parser import parse_resume
            text = parse_resume(target) or ""
        except Exception:
            pass
        applied = h.extract_boss_job(fname) or job_hint
        years = h.experience_years(text)
        picked = pick_job(text, detail.get("name") or "", applied, years)

        app_id = None
        note = h._decision_note(picked).strip() if picked else "未匹配到岗位"
        if picked and picked.get("job_id"):
            app_id = h.try_create_application(str(detail["talent_id"]), picked["job_id"],
                                              BOSS_SOURCE_ID)
            if app_id:
                h.record_job_choice(picked["job_id"], picked.get("title", ""), 1)

        h.log_import(detail.get("name") or "", detail.get("mobile"), detail.get("email"),
                     BOSS_SOURCE_ID, str(detail["talent_id"]), fname,
                     (picked.get("job_id") if picked else None) if app_id else None,
                     app_id, src_name, channel="mail")

        if not app_id and not h.has_any_application(str(detail["talent_id"])):
            h.add_unassigned(detail.get("name") or "", str(detail["talent_id"]), fname,
                             note or "未匹配到岗位", picked if picked else None, applied=applied)

        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            target.rename(ARCHIVE_DIR / target.name)
        except OSError:
            pass

        url = h.open_talent(str(detail["talent_id"]), background=True)
        h.notify("简历已入库", f"{detail.get('name', '')} {note}"[:60], sound=True)
        log(f"✓ {detail.get('name')} [{kind}] {note} → {url}")
        done_atts.add(att_key)
        done += 1
    return done, failed, False


def watch_realtime(dry_run: bool = False) -> int:
    """实时监听邮件到达事件（WebSocket），收到即入库"""
    log("实时监听启动：mail.user_mailbox.event.message_received_v1")
    proc = subprocess.Popen(
        ["lark-cli", "mail", "+watch", "--as", "user", "--format", "data",
         "--msg-format", "full"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=open(PROJECT_DIR / "data" / "logs" / "mailwatch.err.log", "a"),
        text=True, bufsize=1)
    state = load_state()
    processed = set(state.get("processed") or [])
    done_atts = set(state.get("attachments") or [])
    fail_counts = dict(state.get("fail_counts") or {})
    import threading
    def _alive():
        while True:
            time.sleep(60)
            if proc.poll() is not None:
                log("⚠ 实时监听进程已退出，等待 launchd 拉起")
                return
    threading.Thread(target=_alive, daemon=True).start()
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            print(f"RAW {line[:300]}", file=sys.stderr, flush=True)
            msg = payload.get("message") if isinstance(payload.get("message"), dict) else payload
            if not isinstance(msg, dict) or "message_id" not in msg:
                ev = payload.get("event") if isinstance(payload.get("event"), dict) else {}
                if ev.get("message_id"):
                    msg = {"message_id": ev["message_id"]}
            mid = msg.get("message_id") or msg.get("id") or ""
            # 只有 id 没有正文时，按 id 拉一次完整邮件
            if mid and "attachments" not in msg:
                try:
                    fetched = lark(["mail", "+messages", "--message-ids", mid])
                except LarkCallError as e:
                    log(f"✗ 推送拉取详情失败: {e.kind}: {e}")
                    continue
                got = ((fetched.get("data") or {}).get("messages") or [None])[0]
                if isinstance(got, dict):
                    msg = got
            if not mid or mid in processed:
                continue
            sender = ((msg.get("head_from") or {}).get("mail_address") or "").lower()
            domain = sender.rsplit("@", 1)[-1]
            if not any(domain == d or domain.endswith("." + d) for d in DEFAULT_SENDERS):
                log(f"· 推送非平台邮件，跳过: {sender} | {(msg.get('subject') or '')[:24]}")
                processed.add(mid)
                continue
            log(f"📨 收到邮件推送: {(msg.get('subject') or '')[:36]}")
            try:
                with file_lock(MAIL_LOCK, timeout=120):
                    count, failed, handled = process_message(msg, dry_run, done_atts, fail_counts)
            except Exception as e:
                log(f"✗ 推送处理异常: {type(e).__name__}: {e}")
                count, failed = 0, 1
            if handled or (count and not failed):
                processed.add(mid)
            state["processed"] = list(processed)
            state["attachments"] = sorted(done_atts)[-5000:]
            state["fail_counts"] = {k: v for k, v in fail_counts.items() if k not in done_atts}
            save_state(state)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
    return 0


def _print_scan_summary(limit: int, ids: list, messages: list[dict],
                        processed: set, dry_run: bool) -> dict:
    """首次/dry-run 时打印扫描范围与候选附件数（C10）。"""
    candidates = 0
    platform = 0
    for msg in messages:
        sender = ((msg.get("head_from") or {}).get("mail_address") or "").lower()
        domain = sender.rsplit("@", 1)[-1].strip().lower()
        if not any(domain == d or domain.endswith("." + d) for d in DEFAULT_SENDERS):
            continue
        platform += 1
        atts = [a for a in (msg.get("attachments") or [])
                if Path(a.get("filename", "")).suffix.lower() in SUPPORTED]
        candidates += len(atts)
    summary = {
        "limit": limit,
        "listed": len(ids),
        "fetched": len(messages),
        "platform": platform,
        "candidate_attachments": candidates,
        "already_processed": sum(1 for m in messages
                                 if (m.get("message_id") or m.get("id") or "") in processed),
    }
    log(f"扫描范围：--limit={limit}（默认 20，可调）"
        f"｜列表 {summary['listed']}｜拉取 {summary['fetched']}"
        f"（列表可能因 page_size×页数多于 limit）"
        f"｜平台邮件 {summary['platform']}｜候选附件 {summary['candidate_attachments']}"
        f"｜已处理跳过 {summary['already_processed']}"
        + ("｜dry-run" if dry_run else ""))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="邮箱简历自动入库")
    parser.add_argument(
        "--limit", type=int, default=20,
        help="扫描目标封数（默认 20；可在本机/launchd 用 RESUME_MAIL_LIMIT 调大。"
             "列表接口可能按 page_size×页数多取，摘要里的「列表/拉取」是实际条数）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只看不写")
    parser.add_argument("--watch", action="store_true", help="实时监听邮件到达（WebSocket）")
    args = parser.parse_args()

    if args.watch:
        return watch_realtime(args.dry_run)

    h.harden_permissions()
    state = load_state()
    processed = set(state.get("processed") or [])
    done_atts = set(state.get("attachments") or [])
    fail_counts = dict(state.get("fail_counts") or {})
    stats = {"processed_runs": 0, "created": 0, "merged": 0, "applied": 0,
             "skipped": 0, "failed": 0}

    try:
        # 快速通道：最新一封没变且全部已处理 -> 只花 1 次列表调用就退出
        first_page = list_messages(args.limit, known=processed, max_pages=1)
    except LarkCallError as e:
        if e.kind == "permission":
            log(f"✗ 权限错误：{e}（不是空邮箱）")
        elif e.kind == "network":
            log(f"✗ 网络错误：{e}")
        else:
            log(f"✗ 调用失败：{e}")
        return 2

    if first_page and all(m in processed for m in first_page) and state.get("last_top") == first_page[0]:
        log("无新邮件（快速检查）")
        return 0
    if first_page:
        state["last_top"] = first_page[0]

    try:
        ids = list_messages(args.limit, known=processed)
    except LarkCallError as e:
        if e.kind == "permission":
            log(f"✗ 权限错误：{e}（不是空邮箱）")
        elif e.kind == "network":
            log(f"✗ 网络错误：{e}")
        else:
            log(f"✗ 调用失败：{e}")
        return 2

    if not ids:
        # 只有确认调用成功且列表为空时，才算真正空箱
        log("没有读到邮件（收件箱为空或本页无数据）")
        return 0

    try:
        messages = fetch_messages(ids)
    except LarkCallError as e:
        if e.kind == "permission":
            log(f"✗ 权限错误（读邮件详情失败）：{e}")
        elif e.kind == "network":
            log(f"✗ 网络错误（读邮件详情失败）：{e}")
        else:
            log(f"✗ 调用失败（读邮件详情失败）：{e}")
        return 2

    first_run = not state.get("scan_announced")
    if first_run or args.dry_run:
        _print_scan_summary(args.limit, ids, messages, processed, args.dry_run)
        state["scan_announced"] = True

    total = 0
    for msg in messages:
        mid = msg.get("message_id") or msg.get("id") or ""
        sender = ((msg.get("head_from") or {}).get("mail_address") or "").lower()
        if mid in processed:
            stats["skipped"] += 1
            continue
        domain = sender.rsplit("@", 1)[-1].strip().lower()
        if not any(domain == d or domain.endswith("." + d) for d in DEFAULT_SENDERS):
            continue
        try:
            with file_lock(MAIL_LOCK, timeout=120):
                count, failed, handled = process_message(msg, args.dry_run, done_atts, fail_counts)
        except LarkCallError as e:
            failed = 1
            count = 0
            handled = False
            log(f"✗ 邮件 API 失败: {e.kind}: {e}")
        except Exception as e:
            failed = 1
            count = 0
            handled = False
            log(f"✗ 邮件处理异常（已跳过）: {type(e).__name__}: {e}")
        stats["processed_runs"] += 1
        if count:
            total += count
            stats["created"] += count  # 次数口径：本轮成功处理份数（含合并）；详见汇总说明
        if failed:
            stats["failed"] += failed
        if handled or (count and not failed):
            processed.add(mid)
        elif failed:
            log(f"⚠ 邮件 {mid[:12]} 有 {failed} 个附件未完成，下次重试")

    if not args.dry_run:
        state["processed"] = list(processed)
        state["attachments"] = sorted(done_atts)[-5000:]
        state["fail_counts"] = {k: v for k, v in fail_counts.items() if k not in done_atts}
        save_state(state)
    from datetime import datetime as _dt
    try:
        h.atomic_write_json(h.DATA_DIR / "mail_heartbeat.json", {
            "ts": _dt.now().isoformat(timespec="seconds"),
            "scanned": len(messages), "processed": total,
            "stats": stats, "limit": args.limit,
        })
    except OSError:
        pass
    log(f"本次处理 {total} 份（扫描 {len(messages)} 封｜"
        f"处理轮次 {stats['processed_runs']}｜成功 {stats['created']}｜"
        f"跳过 {stats['skipped']}｜失败 {stats['failed']}；"
        f"口径：同一附件重复扫描不增加新建，依赖 attachments 去重）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
