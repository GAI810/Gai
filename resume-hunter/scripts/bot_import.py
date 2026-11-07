"""飞书机器人收简历：同事把简历 PDF 发给机器人，自动入库飞书招聘人才库

用法：
  .venv/bin/python scripts/bot_import.py            # 前台运行
  .venv/bin/python scripts/bot_import.py --reply    # 入库后回复发送人

原理：
  1. `lark-cli event consume im.message.receive_v1 --as bot` 长连接收消息事件
  2. 收到 file 消息 → `im +messages-mget --download-resources` 下载文件
  3. 复用 hire_import 的上传/建人才逻辑入库
  4. 可选：回复发送人结果（含人才链接）

前置：
  - 应用已开启机器人能力 + 事件订阅（im.message.receive_v1，长连接模式）
  - 应用已开通 hire 4 项权限、im 消息读取与资源下载权限
"""

import argparse
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import DATA_DIR  # noqa: E402
import hire_import as h  # noqa: E402

LOG_FILE = DATA_DIR / "logs" / "bot_import.log"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def find_key(obj, key):
    """在嵌套 dict/list 里找第一个 key"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def reply(message_id: str, text: str) -> None:
    try:
        subprocess.run(
            ["lark-cli", "im", "+messages-reply", "--as", "bot",
             "--message-id", message_id, "--text", text],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        log(f"回复失败: {e}")


def allowed_sender(sender_id: str) -> bool:
    """发送人白名单（RESUME_BOT_ALLOW，逗号分隔 open_id；为空表示不限制但会记日志）"""
    allow = {x.strip() for x in (os.environ.get("RESUME_BOT_ALLOW") or "").split(",") if x.strip()}
    if not allow:
        return True
    return sender_id in allow


def handle_file_message(message_id: str, chat_id: str, reply_enabled: bool,
                        sender_id: str = "") -> None:
    """下载消息里的文件并入库"""
    if not allowed_sender(sender_id):
        log(f"✗ 发送人不在白名单，忽略: {sender_id}")
        if reply_enabled:
            reply(message_id, "你不在允许使用该机器人的名单里，请联系 HR 管理员。")
        return
    if not (os.environ.get("RESUME_BOT_ALLOW") or "").strip():
        log("⚠ 未设置 RESUME_BOT_ALLOW，机器人接受租户内任何人发来的简历")
    workdir = Path(tempfile.mkdtemp(prefix="bot-res-", dir="/tmp"))
    try:
        proc = subprocess.run(
            ["lark-cli", "im", "+messages-mget", "--as", "bot",
             "--message-ids", message_id, "--download-resources", "--format", "json"],
            cwd=str(workdir), capture_output=True, text=True, timeout=120,
        )
        raw = (proc.stdout or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1:
            log(f"下载资源失败: {(proc.stderr or raw)[:200]}")
            return
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            log("下载资源返回解析失败")
            return

        # 从消息 content 里取原始文件名：<file key="..." name="张三_.pdf"/>
        messages = (data.get("data") or {}).get("messages") or []
        content = (messages[0].get("content") if messages else "") or ""
        m = re.search(r'name="([^"]+)"', content)
        original_name = m.group(1) if m else ""

        # 找到下载下来的文件
        files = [p for p in workdir.rglob("*")
                 if p.is_file() and p.suffix.lower() in h.SUPPORTED_SUFFIXES]
        if not files:
            log("消息里没有找到简历文件（可能是图片或非 PDF/DOC）")
            if reply_enabled:
                reply(message_id, "没找到可入库的简历文件（支持 PDF/DOC/DOCX）")
            return

        # 还原原始文件名（下载后是 file_v3_xxx.pdf）
        if original_name:
            safe_name = Path(original_name).name
            if not safe_name or Path(safe_name).suffix.lower() not in h.SUPPORTED_SUFFIXES:
                safe_name = ""
        else:
            safe_name = ""
        if safe_name:
            for src in list(files):
                dst = (workdir / safe_name).resolve()
                try:
                    dst.relative_to(workdir.resolve())
                except ValueError:
                    log(f"✗ 非法文件名，已忽略: {original_name[:60]}")
                    continue
                if src != dst:
                    try:
                        shutil.move(str(src), str(dst))
                        files = [dst if x == src else x for x in files]
                    except OSError:
                        pass

        for f in files:
            status, detail = h.import_file(f, h.load_preferred_source_id())
            log(f"入库 {f.name} -> {status}: {json.dumps(detail, ensure_ascii=False)}")
            if status in ("ok", "merged", "duplicate") and detail.get("talent_id"):
                h.log_import(detail.get("name") or "", detail.get("mobile"),
                             detail.get("email"), None, str(detail["talent_id"]),
                             str(f), source_name="飞书机器人")
                url = h.TALENT_URL_TMPL.replace("{talent_id}", str(detail["talent_id"]))
                if reply_enabled:
                    note = detail.get("note") or "已入库"
                    reply(message_id,
                          f"✅ {detail.get('name','')} {note}\n人才页：{url}")
            else:
                if reply_enabled:
                    reply(message_id, f"❌ 入库失败：{detail.get('reason','')[:120]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="飞书机器人收简历并入库")
    parser.add_argument("--reply", action="store_true", help="入库后回复发送人")
    parser.add_argument("--source-name", default="BOSS直聘", help="默认简历来源")
    args = parser.parse_args()

    if args.source_name:
        os.environ.setdefault("BOT_SOURCE_NAME", args.source_name)

    log("启动机器人监听（im.message.receive_v1）…")
    # 注意：consume 把 stdin EOF 当退出信号，这里保持 stdin 管道不关闭
    proc = subprocess.Popen(
        ["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    try:
        buf = ""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                log(f"[event] {line[:160]}")
                continue
            # 事件是多行 JSON，累积到能解析为止
            buf += line
            try:
                ev = json.loads(buf)
                buf = ""
            except json.JSONDecodeError:
                continue

            sender = find_key(ev, "sender") or {}
            sender_id = ""
            if isinstance(sender, dict):
                sid = sender.get("sender_id") or {}
                sender_id = sid.get("open_id") or sid.get("user_id") or "" or find_key(ev, "open_id") or ""
            mid = find_key(ev, "message_id")
            mtype = find_key(ev, "message_type") or find_key(ev, "msg_type")
            chat_id = find_key(ev, "chat_id") or ""
            if not mid:
                continue
            log(f"收到消息 {mid} type={mtype}")
            if mtype in ("file", "media"):
                handle_file_message(mid, chat_id, args.reply, str(sender_id or ""))
            else:
                log(f"忽略非文件消息 type={mtype}")
    except KeyboardInterrupt:
        log("停止监听")
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
