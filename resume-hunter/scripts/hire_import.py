"""飞书招聘一键入库：把已下载的简历 PDF 导入飞书招聘人才库

对应飞书招聘网页上的手动流程「新简历入库」：
  1. 上传简历文件      -> POST /open-apis/hire/v1/attachments
  2. 创建人才（入库）  -> POST /open-apis/hire/v1/talents/combined_create

三种用法：
  A. 数据库批量模式：处理 resume-hunter 已分析的候选人
  B. 单文件模式：--file 指定任意下载的简历 PDF（Boss/猎聘都行），导入后可用 --open 在浏览器打开
  C. 监听模式：--watch 指定文件夹，新下载的简历出现即自动导入并打开浏览器

用法：
  # 检查权限（缺权限时打印开发者后台开通指引）
  .venv/bin/python scripts/hire_import.py --check

  # 单文件导入（比如刚下载的 Boss 简历），导入后在浏览器打开人才详情
  .venv/bin/python scripts/hire_import.py --file ~/Downloads/xxx.pdf --open
  .venv/bin/python scripts/hire_import.py --file ~/Downloads/xx.pdf --source-name 猎聘

  # 监听文件夹：新简历出现即自动入库
  .venv/bin/python scripts/hire_import.py --watch --open
  .venv/bin/python scripts/hire_import.py --watch "~/Downloads/招聘简历/待整理下载" --open

  # 列出待入库候选人（不执行任何写操作）
  .venv/bin/python scripts/hire_import.py --list

  # 干跑：打印将要执行的请求，不真正调用
  .venv/bin/python scripts/hire_import.py --dry-run --min-score 7

  # 真正入库：默认全部待入库候选人，可用过滤条件收窄
  .venv/bin/python scripts/hire_import.py --yes
  .venv/bin/python scripts/hire_import.py --yes --min-score 8 --limit 5
  .venv/bin/python scripts/hire_import.py --yes --names 张三,李四
  .venv/bin/python scripts/hire_import.py --yes --ids 1,2,3

简历来源默认填「BOSS直聘」（自动按名称解析来源 ID），可用
--source-name / --source-id 换成其他来源（如 猎聘），--no-source 则不填。
人才详情页 URL 模板来自 data/config.json 的 talent_url 或 tenant_domain，
可用环境变量 HIRE_TALENT_URL 或 --talent-url 覆盖；仅在都未配置时才回退占位域名 example.feishu.cn。
"""

import argparse
import json
import logging
import os
import re
import gc
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from datetime import datetime
from pathlib import Path

# 压掉 pdfplumber/pdfminer 的 FontBBox 等告警
logging.getLogger("pdfminer").setLevel(logging.ERROR)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import DATA_DIR  # noqa: E402
from lib.db import Database  # noqa: E402
from lib.resume_parser import parse_resume  # noqa: E402
from lib.state_store import (read_json, update_json, atomic_write_json,  # noqa: E402
                             atomic_write_bytes, file_lock)

ATTACHMENT_API = "/open-apis/hire/v1/attachments"
TALENT_API = "/open-apis/hire/v1/talents/combined_create"
JOB_LIST_API = "/open-apis/hire/v1/jobs"
APPLICATION_API = "/open-apis/hire/v1/applications"
def _load_site_config() -> dict:
    p = DATA_DIR / "config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


_SITE = _load_site_config()
CONSOLE_URL = _SITE.get("console_url") or "https://open.feishu.cn/app/<APP_ID>"

# 默认监听目录：从 Boss/猎聘 下载的简历通常落在 ~/Downloads
DEFAULT_WATCH_DIR = Path.home() / "Downloads" / "招聘简历" / "待整理下载"
# 人才详情页 URL 模板（{talent_id} 会被替换）；可用环境变量覆盖
def _default_talent_url() -> str:
    """优先 config.talent_url → tenant_domain → 最后才回退占位域名 example.feishu.cn。"""
    if _SITE.get("talent_url"):
        return str(_SITE["talent_url"])
    tenant = str(_SITE.get("tenant_domain") or "").strip()
    if tenant:
        tenant = tenant.replace("https://", "").replace("http://", "").strip("/")
        return f"https://{tenant}/hire/talent/{{talent_id}}"
    # 最后兜底：未配置时仍能打开，但安装时应写入本租户域名
    return "https://example.feishu.cn/hire/talent/{talent_id}"


TALENT_URL_TMPL = os.environ.get("HIRE_TALENT_URL") or _default_talent_url()
WATCH_STATE_FILE = DATA_DIR / "hire_watch_state.json"
IMPORT_LOG_FILE = DATA_DIR / "hire_import_log.jsonl"
ACTIVE_JOB_FILE = DATA_DIR / "active_job.json"
FOCUS_JOBS_FILE = DATA_DIR / "focus_jobs.json"
JOB_ALIAS_FILE = DATA_DIR / "job_alias.json"
LLM_CONFIG_FILE = DATA_DIR / "llm_config.json"
MY_JOBS_CACHE = DATA_DIR / "my_jobs.json"
JOB_PROFILE_FILE = DATA_DIR / "job_profile.json"
DECISION_EVAL_FILE = DATA_DIR / "decision_eval.json"
JOBS_CACHE_FILE = DATA_DIR / "jobs_cache.json"
UNASSIGNED_FILE = DATA_DIR / "unassigned.json"
CORRECTIONS_FILE = DATA_DIR / "corrections.json"
MAP_SUGGESTIONS_FILE = DATA_DIR / "map_suggestions.json"
LEARN_SEEN_FILE = DATA_DIR / "learn_seen.json"
REVIEW_FILE = DATA_DIR / "needs_review.json"
IMPORT_INDEX = DATA_DIR / "import_index.json"
NOTIFY_STATE = DATA_DIR / "notify_state.json"
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"
JOB_INTEREST_FILE = DATA_DIR / "job_interest.json"
REPORT_DIR = DATA_DIR / "reports"
SUPPORTED_SUFFIXES = (".pdf", ".docx", ".doc")

MOBILE_RE = re.compile(r"1[3-9]\d{9}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fa5]{2,4}")

# 姓名标签模式：Boss 简历常见「姓名 张三」「姓 名 张三」
NAME_LABEL_PATTERNS = [
    re.compile(r"姓\s*名\s*([\u4e00-\u9fa5]{2,8})"),
    re.compile(r"名\s*字\s*([\u4e00-\u9fa5]{2,8})"),
    re.compile(r"姓\s*氏\s*([\u4e00-\u9fa5]{2,8})"),
]

CJK_RUN_LONG = re.compile(r"[\u4e00-\u9fa5]{2,}")

# 姓名启发式提取时的排除词（简历常见栏目词、城市、状态词等）
NAME_STOPWORDS = {
    "个人概述", "求职意向", "基本信息", "工作经历", "教育经历", "项目经历",
    "自我评价", "联系方式", "出生日期", "政治面貌", "婚姻状况", "所在地",
    "期望职位", "期望薪资", "期望城市", "离职状态", "求职状态", "目前",
    "本科", "硕士", "博士", "大专", "高中", "未婚", "已婚", "离职", "到岗",
    "男", "女", "岁", "年", "月", "日", "邮箱", "电话", "手机", "微信",
    "简历", "姓名", "在线简历", "微简历", "求职", "个人简历", "个人信息",
    "个人简介", "联系邮箱", "联系电话",
    "教育背景", "教育经历", "工作经历", "项目经验", "实习经历", "校园经历",
    "技能特长", "专业技能", "语言能力", "获奖情况", "荣誉奖项", "证书",
    "兴趣爱好", "个人优势", "个人评价", "基本信息", "联系方式", "求职意向",
    "北京", "上海", "深圳", "广州", "杭州", "南京", "成都", "武汉", "西安",
    "苏州", "长沙", "郑州", "重庆", "天津", "厦门", "青岛", "济南", "合肥",
    "福州", "大连", "宁波", "无锡", "佛山", "东莞", "珠海", "南昌", "贵阳",
    "昆明", "太原", "石家庄", "哈尔滨", "长春", "沈阳", "海口", "南宁",
    "浙江", "江苏", "广东", "山东", "四川", "湖南", "湖北", "河南", "河北",
    "福建", "安徽", "江西", "山西", "陕西", "辽宁", "吉林", "黑龙江", "云南",
    "贵州", "广西", "甘肃", "青海", "宁夏", "新疆", "西藏", "内蒙古", "海南",
    "台湾", "香港", "澳门", "北京", "上海", "天津", "重庆",
}


def lark_json(args: list[str], timeout: int = 120, stdin: str | None = None,
              fmt: bool = True) -> dict:
    """执行 lark-cli 命令并解析其 JSON 输出（stdin 用于 --data - 传请求体）

    注意：lark-cli 在接口报错时会把 JSON 打到 stderr 并以非 0 退出，
    因此 stdout 为空时要从 stderr 里提取 JSON。
    """
    cmd = ["lark-cli", *args] + (["--format", "json"] if fmt else [])
    proc = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=timeout, input=stdin,
    )
    for raw in (proc.stdout, proc.stderr):
        out = (raw or "").strip()
        if not out:
            continue
        start, end = out.find("{"), out.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(out[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise RuntimeError(
        f"lark-cli 无有效输出（exit={proc.returncode}）: "
        f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
    )


def _is_permission_error(result: dict) -> bool:
    """判断 lark-cli 返回的失败是否是权限不足（而非参数错误）"""
    err = result.get("error") or {}
    if err.get("permission_violations") or err.get("missing_scopes"):
        return True
    msg = (err.get("message") or "").lower()
    return ("scope" in msg or "权限" in msg or "access denied" in msg
            or err.get("code") in (99991663, 99991664, 99991668, 99991672, 99991679))


def probe_scope(talent: bool) -> bool:
    """安全探针：用非法参数调用写接口。

    权限不足 -> 报权限错误；权限已开通 -> 先过权限校验，再报参数错误。
    参数错误不会创建任何数据，因此可以安全用于 --check。
    """
    if talent:
        result = lark_json(["api", "POST", TALENT_API, "--as", "bot", "--data", "-"], stdin="{}")
    else:
        result = lark_json(["api", "POST", ATTACHMENT_API, "--as", "bot"])
    if result.get("ok"):
        return True  # 意外成功（理论上不会），视为有权限
    return not _is_permission_error(result)


def probe_job_scope() -> bool:
    """探针：读取职位列表权限"""
    result = lark_json(["api", "GET", JOB_LIST_API, "--as", "bot",
                        "--params", json.dumps({"page_size": 1})])
    if result.get("ok"):
        return True
    return not _is_permission_error(result)


def probe_application_scope() -> bool:
    """探针：创建投递权限（用不存在的人才 ID，不会产生数据）"""
    result = lark_json([
        "api", "POST", APPLICATION_API, "--as", "bot", "--data", "-",
    ], stdin=json.dumps({"talent_id": "0", "job_id": "0", "delivery_type": 1}))
    if result.get("ok"):
        return True
    return not _is_permission_error(result)


# 权限申请说明预填（C5 / C12）：开发者后台「申请说明」可直接粘贴
APPLICATION_REASON = "招聘提效工具-简历自动入库"

# 授权四态（C5）
SCOPE_STATE_ACTIVE = "已生效"
SCOPE_STATE_NEED_APPLY = "待申请"
SCOPE_STATE_NEED_ADMIN = "待管理员审批"
SCOPE_STATE_NEED_USER = "待用户授权"

# 一次列全：招聘（应用级）+ 邮件（用户级）
REQUIRED_SCOPE_SPECS = [
    # scope, kind(bot|user), label, probe key
    ("hire:talent", "bot", "更新人才信息", "hire_talent"),
    ("hire:attachment", "bot", "上传招聘附件", "hire_attachment"),
    ("hire:job:readonly", "bot", "读取职位列表", "hire_job"),
    ("hire:application", "bot", "创建投递", "hire_application"),
    ("mail:user_mailbox.message:readonly", "user", "邮件列表", "mail_list"),
    ("mail:user_mailbox.message", "user", "邮件详情（发件人/主题）", "mail_detail"),
    ("mail:user_mailbox.message.attachment:readonly", "user", "邮件附件下载", "mail_attachment"),
]


def _user_auth_ready() -> bool:
    """用户 token 是否已登录（邮件权限依赖 user）。"""
    try:
        proc = subprocess.run(
            ["lark-cli", "auth", "status"],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    blob = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if proc.returncode != 0:
        return False
    low = blob.lower()
    if any(x in low for x in ("not login", "not logged", "unauthenticated", "未登录", "no user")):
        return False
    return True


def _classify_scope_state(*, ok: bool, result: dict | None, user_scope: bool,
                          probe_error: str = "") -> str:
    """把探针结果映射到四态之一。"""
    if ok:
        return SCOPE_STATE_ACTIVE
    blob_parts = [probe_error or ""]
    if isinstance(result, dict):
        err = result.get("error") or {}
        blob_parts.append(json.dumps(err, ensure_ascii=False))
        blob_parts.append(str(err.get("message") or ""))
        blob_parts.append(str(err.get("code") or ""))
    blob = "\n".join(blob_parts)
    low = blob.lower()
    if any(k in blob for k in ("审批中", "待审批", "审核中", "等待管理员")) or        any(k in low for k in ("pending", "under review", "awaiting approval", "approval_pending")):
        return SCOPE_STATE_NEED_ADMIN
    if user_scope:
        if (not _user_auth_ready()) or any(
            k in low for k in ("user not authorized", "authorize", "user_access_token", "需要用户授权", "未授权")
        ) or any(k in blob for k in ("待用户授权", "用户授权", "重新登录")):
            return SCOPE_STATE_NEED_USER
    # 缺 scope / 未申请：最常见
    return SCOPE_STATE_NEED_APPLY


def _probe_mail_once(args: list[str]) -> tuple[bool, dict | None, str]:
    """用户身份邮件探针。返回 (有权限?, 原始结果, 错误摘要)。"""
    try:
        proc = subprocess.run(
            ["lark-cli", *args],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as e:
        return False, None, f"未找到 lark-cli: {e}"
    except subprocess.TimeoutExpired:
        return False, None, "邮件探针超时"
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    parsed = None
    start, end = out.find("{"), out.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(out[start:end + 1])
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        if parsed.get("ok") is True or (parsed.get("data") is not None and parsed.get("ok") is not False):
            # 业务成功或带 data：权限已过
            if parsed.get("ok") is False and _is_permission_error(parsed):
                return False, parsed, str((parsed.get("error") or {}).get("message") or "permission")
            if parsed.get("ok") is False:
                # 参数/业务错误但已过权限校验 → 视为有权限
                return (not _is_permission_error(parsed)), parsed, str((parsed.get("error") or {}).get("message") or "")
            return True, parsed, ""
        if _is_permission_error(parsed):
            return False, parsed, str((parsed.get("error") or {}).get("message") or "permission")
    # 无 JSON：根据文案判断
    low = out.lower()
    if any(m in low for m in ("permission", "scope", "access denied", "missing_scopes")) or "权限" in out:
        return False, parsed, out[:200]
    if proc.returncode == 0:
        return True, parsed, ""
    # 非权限失败（网络等）——不记入 missing，记 errors
    return True, parsed, f"非权限类失败 exit={proc.returncode}: {out[:120]}"


def probe_mail_list_scope() -> tuple[bool, dict | None, str]:
    return _probe_mail_once([
        "mail", "user_mailbox.messages", "list", "--as", "user",
        "--params", json.dumps({"folder_id": "INBOX", "page_size": 1}),
    ])


def probe_mail_detail_scope() -> tuple[bool, dict | None, str]:
    # +messages：详情（含发件人/主题）；用不存在 id，有权限会报参数/找不到，无权限报 scope
    return _probe_mail_once([
        "mail", "+messages", "--message-ids", "0",
    ])


def probe_mail_attachment_scope() -> tuple[bool, dict | None, str]:
    return _probe_mail_once([
        "mail", "user_mailbox.message.attachments", "download_url", "--as", "user",
        "--params", json.dumps({"message_id": "0", "attachment_ids": ["0"]}),
    ])


def check_scopes() -> dict:
    """检查招聘 + 邮件权限；返回四态明细。

    注意：lark-cli auth check 只能查用户 token 的 scope，查不到应用（租户）token
    的权限，所以招聘侧用非法参数探针；邮件侧用 list/详情/附件探针。
    """
    items = []
    missing, errors = [], []

    def _add(scope: str, kind: str, label: str, ok: bool, result=None, err: str = ""):
        state = _classify_scope_state(ok=ok, result=result if isinstance(result, dict) else None,
                                      user_scope=(kind == "user"), probe_error=err)
        # 非权限类探测失败：不算缺权，但记 errors，状态标已生效（探针本身不稳定时不误伤）
        if (not ok) and err and ("非权限类失败" in err or "超时" in err or "未找到 lark-cli" in err):
            errors.append(f"{label}（{scope}）: {err}")
            state = SCOPE_STATE_ACTIVE if "非权限类失败" in err else (
                SCOPE_STATE_NEED_USER if kind == "user" and "未找到" not in err else SCOPE_STATE_NEED_APPLY
            )
            if "未找到 lark-cli" in err:
                state = SCOPE_STATE_NEED_APPLY
                ok = False
            elif "超时" in err:
                errors.append(f"{label} 探针超时，请检查网络后重试")
                # 超时不把该项算 missing
                items.append({
                    "scope": scope, "kind": kind, "label": label,
                    "state": "探测失败（网络）", "ok": None,
                })
                return
        items.append({
            "scope": scope, "kind": kind, "label": label,
            "state": state, "ok": ok if state == SCOPE_STATE_ACTIVE else False,
        })
        if state != SCOPE_STATE_ACTIVE:
            missing.append(scope)

    # 招聘（bot）
    hire_probes = [
        ("hire:talent", "bot", "更新人才信息", lambda: probe_scope(talent=True)),
        ("hire:attachment", "bot", "上传招聘附件", lambda: probe_scope(talent=False)),
        ("hire:job:readonly", "bot", "读取职位列表", probe_job_scope),
        ("hire:application", "bot", "创建投递", probe_application_scope),
    ]
    for scope, kind, label, fn in hire_probes:
        try:
            ok = bool(fn())
            _add(scope, kind, label, ok, result=None, err="")
        except RuntimeError as e:
            # RuntimeError 可能是无输出；尽量按权限不足处理
            msg = str(e)
            if any(x in msg.lower() for x in ("scope", "permission", "权限")):
                _add(scope, kind, label, False, err=msg)
            else:
                errors.append(f"{label}探针失败: {e}")
                _add(scope, kind, label, False, err=msg)

    # 邮件（user）
    if not _user_auth_ready():
        for scope, kind, label, _key in REQUIRED_SCOPE_SPECS:
            if kind != "user":
                continue
            items.append({
                "scope": scope, "kind": kind, "label": label,
                "state": SCOPE_STATE_NEED_USER, "ok": False,
            })
            missing.append(scope)
        errors.append("用户未登录 lark-cli（邮件权限需 user token）：请执行 lark-cli auth login")
    else:
        mail_probes = [
            ("mail:user_mailbox.message:readonly", "user", "邮件列表", probe_mail_list_scope),
            ("mail:user_mailbox.message", "user", "邮件详情（发件人/主题）", probe_mail_detail_scope),
            ("mail:user_mailbox.message.attachment:readonly", "user", "邮件附件下载", probe_mail_attachment_scope),
        ]
        for scope, kind, label, fn in mail_probes:
            try:
                ok, result, err = fn()
                # ok=True 且 err 含非权限类（如 lark-cli validation）→ 权限已过，不刷屏
                if ok and err and "非权限类失败" in err:
                    pass  # 信息级噪音，最终仍标「已生效」
                _add(scope, kind, label, ok, result=result, err=err)
            except Exception as e:
                errors.append(f"{label}探针异常: {e}")
                _add(scope, kind, label, False, err=str(e))

    # 去重 missing 保序
    seen = set()
    missing_uniq = []
    for s in missing:
        if s not in seen:
            seen.add(s)
            missing_uniq.append(s)

    return {
        "ok": not missing_uniq,
        "missing": missing_uniq,
        "errors": errors,
        "items": items,
        "application_reason": APPLICATION_REASON,
    }


_SOURCE_CACHE: dict[str, str | None] = {}


def load_preferred_source_id(source_name: str = "BOSS直聘") -> str | None:
    """带缓存的来源解析，供机器人/监听等高频调用"""
    if source_name in _SOURCE_CACHE:
        return _SOURCE_CACHE[source_name]
    try:
        sid = resolve_source_id(source_name)
    except RuntimeError:
        sid = None
    _SOURCE_CACHE[source_name] = sid
    return sid


def resolve_source_id(source_name: str) -> str | None:
    """按名称解析简历来源 ID（如 BOSS直聘 -> 18）；找不到返回 None"""
    result = lark_json([
        "api", "GET", "/open-apis/hire/v1/resume_sources", "--as", "bot",
        "--params", json.dumps({"page_size": 100}), "--page-all",
    ])
    data = result.get("data") or {}
    items = data.get("items") or []
    for item in items:
        if item.get("zh_name") == source_name:
            return str(item.get("id"))
    return None


def clean_mobile(v: str) -> str:
    """只保留 11 位大陆手机号"""
    digits = re.sub(r"\D", "", v or "")
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits if re.fullmatch(r"1[3-9]\d{9}", digits or "") else ""


def clean_email(v: str) -> str:
    """清洗并校验邮箱（去掉尾部标点/中文）"""
    e = (v or "").strip().strip("。，,;；()（）<>《》[]【】 ")
    m = re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", e)
    return e if m else ""


def extract_contact(text: str) -> dict:
    """从简历纯文本中提取手机号、邮箱

    注意：带水印的 PDF 里邮箱常被拆成多行（如 abcabc⏎x⏎12345@example.com），
    直接全文正则会把水印碎片拼成错误邮箱。因此邮箱只信任「同一行内」的完整匹配；
    若全文兜底匹配到的邮箱本地部分过短（<6 字符，通常是水印截断），宁可不填。
    """
    compact = re.sub(r"[\s\-()（）]", "", text)
    mobile = None
    m = MOBILE_RE.search(compact)
    if m:
        mobile = m.group(0)

    email = None
    # 1) 逐行查找：带「邮箱/E-mail」标签的行直接信任；否则要求本地部分够长
    #    （水印 PDF 会把邮箱拆行，残留的碎片往往很短，如 12345@example.com）
    for line in text.splitlines():
        m = EMAIL_RE.search(line)
        if not m:
            continue
        local = m.group(0).split("@")[0]
        has_label = any(k in line for k in ("邮箱", "E-mail", "Email", "EMAIL", "邮件"))
        looks_clean = len(local) >= 6 and not (local.isdigit() and len(local) < 8)
        if has_label or looks_clean:
            email = m.group(0)
            break
    # 2) 兜底：全文匹配，同样要求本地部分不短
    if not email:
        m = EMAIL_RE.search(text)
        if m and len(m.group(0).split("@")[0]) >= 6:
            email = m.group(0)
    return {"mobile": mobile, "email": email}


def _collapse_dup_chars(word: str) -> str:
    """折叠「每字印两遍」的 PDF 水印文本：李李四四 -> 李四"""
    if len(word) % 2 == 0 and word[::2] == word[1::2]:
        return word[::2]
    return word


def _normalize_text(s: str) -> str:
    """把文本里所有「成对重复」的中文串折叠成单份（处理双印水印简历）"""
    return CJK_RUN_LONG.sub(lambda m: _collapse_dup_chars(m.group(0)), s)


NAME_MASK_WORDS = ("女士", "先生", "小姐", "老师", "同学")
ROLE_WORDS_IN_NAME = ("运营", "产品", "设计", "开发", "工程", "实习", "经理", "总监",
                      "数据", "算法", "前端", "后端", "测试", "市场", "销售", "简历",
                      "背景", "经历", "技能", "评价", "意向", "信息",
                      "经验", "工作", "年限", "应届", "毕业", "学历", "专业")


def name_is_valid(cand: str) -> bool:
    """判断一个候选姓名是否可信"""
    if not cand:
        return False
    c = cand.strip()
    if len(c) < 2 or len(c) > 4:
        return False
    if c in NAME_STOPWORDS:
        return False
    if any(w in c for w in NAME_MASK_WORDS):
        return False
    if any(w in c for w in ROLE_WORDS_IN_NAME):
        return False
    if not all("\u4e00" <= ch <= "\u9fa5" for ch in c):
        return False
    if any(ch in "男女岁年月份日号" for ch in c):
        return False
    return True


def name_from_filename(name: str) -> str:
    """从文件名提取姓名兜底：Boss 格式「【岗位…】姓名 经验.pdf」或「姓名 其他.pdf」"""
    stem = Path(name).stem
    m = re.search(r"】\s*([\u4e00-\u9fa5]{2,4})", stem)
    if m:
        cand = m.group(1)
    else:
        m = re.match(r"\s*([\u4e00-\u9fa5]{2,4})", stem)
        cand = m.group(1) if m else ""
    if cand and name_is_valid(cand):
        return cand
    # 英文名：文件名里「】Sam_13年」「Alex_Lee_一年以内」
    m = re.search(r"】\s*([A-Za-z][A-Za-z]+(?:[_ ][A-Za-z][A-Za-z]+)*)", stem)
    if m:
        en = m.group(1).replace("_", " ").strip()
        if en and all(len(w) >= 2 for w in en.split()) and max(len(w) for w in en.split()) >= 3:
            return en
    return ""


def guess_name(text: str, db_name: str, trusted_name: str = "") -> str:
    """从简历文本中猜测真实姓名，失败则回退数据库姓名。

    按可靠性从高到低：
      1. 「姓名 张三 / 姓 名 张三」标签模式；
      2. 数据库非脱敏姓名出现在正文中（来自招聘平台，最权威）；
      3. 页眉区第一个非栏目词的中文 2-4 字词；
      4. 离联系方式最近的中文词；
      5. 数据库姓名。
    """
    # 0) 文件名里的姓名（如「【岗位】张三 27年应届生.pdf」）优先可信
    if trusted_name and name_is_valid(trusted_name):
        return trusted_name

    def is_usable(word: str) -> bool:
        return word not in NAME_STOPWORDS and len(_collapse_dup_chars(word)) >= 2

    def is_masked(word: str) -> bool:
        return any(s in word for s in ("女士", "先生", "小姐", "老师"))

    # 先规范化文本：折叠「每字印两遍」的水印文本（李李四四 -> 李四）
    norm = _normalize_text(text)

    # 0.5) 「李 小 明」这类字与字之间带空格的姓名
    m = re.search(r"(?m)^\s*((?:[\u4e00-\u9fa5]\s+){1,2}[\u4e00-\u9fa5])\s*(?=\n|$|求职|电话|邮箱|年龄|性别|手机|现居)",
                  norm[:400])
    if m:
        joined = re.sub(r"\s+", "", m.group(1))
        if name_is_valid(joined):
            return joined

    # 1) 姓名标签模式
    for pat in NAME_LABEL_PATTERNS:
        m = pat.search(norm[:400])
        if m and is_usable(m.group(1)):
            return _collapse_dup_chars(m.group(1))

    # 2) 数据库姓名（非脱敏）出现在正文中：平台来源，最权威。
    #    精确匹配优先；Boss 下载件有时姓名各字之间夹着水印字符或空格
    #    （如「张y 4 三」「李 四」），因此再做一次允许少量夹杂字符的模糊匹配。
    if db_name and not is_masked(db_name):
        if db_name in norm[:1000]:
            return db_name
        joiner = r"[\sA-Za-z0-9]{0,6}"
        # 中文部分（如「李四Alex Lee」取「李四」）优先：中文名是 ATS 里的主名
        cjk_part = "".join(ch for ch in db_name if "\u4e00" <= ch <= "\u9fa5")
        if len(cjk_part) >= 2:
            fuzzy_cjk = re.compile(joiner.join(re.escape(ch) for ch in cjk_part))
            if fuzzy_cjk.search(norm[:1200]):
                return cjk_part
        # 纯英文名：大小写不敏感匹配
        if db_name.lower() in norm[:1000].lower():
            return db_name
        fuzzy = re.compile(joiner.join(re.escape(ch) for ch in db_name), re.IGNORECASE)
        if fuzzy.search(norm[:1200]):
            return db_name

    # 3) 页眉区第一个中文词
    for m in CJK_RUN_RE.finditer(norm[:400]):
        if is_usable(m.group(0)):
            return _collapse_dup_chars(m.group(0))

    # 4) 离手机号/邮箱最近的中文词（限制在联系方式之前）
    compact = re.sub(r"[\s\-()（）]", "", norm)
    contact_pos = None
    m = MOBILE_RE.search(compact)
    if m:
        contact_pos = m.start()
    else:
        m = EMAIL_RE.search(norm)
        if m:
            contact_pos = m.start()

    # 中文启发式都没命中时，再退回英文名（中文名优先）
    # 0.4) 英文名（Alex Kim / Sam）
    m = re.search(r"(?m)^\s*([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,2})\s*$", norm[:400])
    if m:
        cand_en = m.group(1).strip()
        words = cand_en.split()
        looks_watermark = (len(words) >= 2 and len({w.lower() for w in words}) == 1)
        if not looks_watermark and all(len(w) >= 2 for w in words) and max(len(w) for w in words) >= 3:
            return cand_en

    best, best_dist = None, None
    for m in CJK_RUN_RE.finditer(norm[: max(600, contact_pos or 0)]):
        word = m.group(0)
        if not is_usable(word):
            continue
        dist = m.start()
        if best_dist is None or dist > best_dist:
            best, best_dist = word, dist
    return _collapse_dup_chars(best) if best else db_name


def upload_attachment(path: str) -> str:
    """上传简历文件，返回附件 ID

    注意：lark-cli 读取文件受运行时沙箱/工作目录影响，直接传原始路径可能报
    "cannot open file"（例如在项目目录下读 ~/Downloads）。这里统一先复制到
    临时目录（保留原文件名）再上传，彻底绕开路径可读性问题。
    """
    src = Path(path)
    tmpdir = Path(tempfile.mkdtemp(prefix="hire-upload-", dir="/tmp"))
    try:
        dst = tmpdir / src.name
        shutil.copy2(src, dst)
        result = lark_json(["api", "POST", ATTACHMENT_API, "--as", "bot",
                            "--file", f"content={dst}"])
        return _unwrap(result, "附件上传")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _talent_schools(talent_id: str) -> set[str]:
    """读取已有人才的学校名（用于同名归并的证据校验）"""
    try:
        r = lark_json(["api", "GET", f"/open-apis/hire/v2/talents/{talent_id}", "--as", "bot"])
        data = (r.get("data") or {})
        return {str(e.get("school") or "") for e in (data.get("education_list") or []) if e.get("school")}
    except Exception:
        return set()


def _file_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _index_load() -> dict:
    if not IMPORT_INDEX.exists():
        return {"hashes": {}, "names": {}}
    try:
        return json.loads(IMPORT_INDEX.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"hashes": {}, "names": {}}


def _index_record(sha: str, name: str, talent_id: str) -> None:
    def _mut(d: dict) -> None:
        if sha:
            d.setdefault("hashes", {})[sha] = str(talent_id)
        if name and name_is_valid(name):
            names = d.setdefault("names", {})
            lst = names.setdefault(name, [])
            if str(talent_id) not in lst:
                lst.append(str(talent_id))
            d["names"] = {k: v[-20:] for k, v in list(names.items())[-2000:]}
    try:
        update_json(IMPORT_INDEX, _mut, default={"hashes": {}, "names": {}})
    except (OSError, TimeoutError):
        pass


def find_existing_talent(contact: dict) -> str | None:
    """按手机号/邮箱查已有人才（用于避免重复建人、改为追加简历）"""
    body: dict = {}
    if contact.get("mobile"):
        body["mobile_code"] = "86"
        body["mobile_number_list"] = [contact["mobile"]]
    if contact.get("email"):
        body["email_list"] = [contact["email"]]
    if not body:
        return None
    result = lark_json([
        "api", "POST", "/open-apis/hire/v1/talents/batch_get_id", "--as", "bot",
        "--data", "-",
    ], stdin=json.dumps(body, ensure_ascii=False))
    if not result.get("ok"):
        return None
    talents = (result.get("data") or {}).get("talent_list") or []
    return str(talents[0]["talent_id"]) if talents else None


def talent_attachment_names(talent_id: str) -> list[str]:
    """读取已有人才的附件文件名（用于避免重复追加同一份简历）"""
    result = lark_json([
        "api", "GET", f"/open-apis/hire/v2/talents/{talent_id}", "--as", "bot",
    ])
    if not result.get("ok"):
        return []
    data = result.get("data") or {}
    return [str(a.get("name") or "") for a in (data.get("resume_attachment_list") or [])]


def append_resume(talent_id: str, attachment_id: str, name: str = "") -> None:
    """给已有人才追加简历附件；name 仅在文件名可信时传入（会更新人才姓名）"""
    payload = {
        "talent_id": talent_id,
        "resume_attachment_id": attachment_id,
        "basic_info": {"name": name} if name else {},
    }
    result = lark_json([
        "api", "POST", "/open-apis/hire/v1/talents/combined_update", "--as", "bot",
        "--data", "-",
    ], stdin=json.dumps(payload, ensure_ascii=False))
    _unwrap(result, "追加简历")


def create_talent(payload: dict) -> str:
    """创建人才，返回 talent_id（请求体走 stdin，避免 --data @file 的兼容问题）"""
    result = lark_json([
        "api", "POST", TALENT_API, "--as", "bot",
        "--data", "-",
        "--params", json.dumps({"user_id_type": "open_id"}),
    ], stdin=json.dumps(payload, ensure_ascii=False))
    return _unwrap(result, "创建人才")


def _unwrap(result: dict, step: str) -> str:
    """解析 lark-cli 输出：成功时返回业务 ID，失败时抛异常

    lark-cli 成功时会剥掉飞书响应外层，直接给业务体
    （附件接口 -> data.id，创建人才 -> data.talent_id）；
    少数情况下会保留 {code, data} 外层，这里一并兼容。
    """
    if not result.get("ok"):
        err = result.get("error") or {}
        msg = err.get("message") or json.dumps(err, ensure_ascii=False)[:300]
        raise RuntimeError(f"{step}失败: {msg}")

    data = result.get("data") or {}
    code = data.get("code")
    if code not in (0, None):
        msg = data.get("msg") or data.get("message") or "未知错误"
        raise RuntimeError(f"{step}失败(code={code}): {msg}")

    inner = data
    nested = data.get("data")
    if isinstance(nested, dict) and code is not None:
        inner = nested
    return str(inner.get("id") or inner.get("talent_id") or "")


DATE_RANGE_RE = re.compile(
    r"(20\d{2})[.\-/年]?(\d{1,2})?[.\-/月]?\s*[-–~至]\s*"
    r"(?:(20\d{2})[.\-/年]?(\d{1,2})?|(至今|今|现在))"
)
DEGREE_MAP = {"博士": 8, "硕士": 7, "本科": 6, "大专": 5, "高中": 4, "中专": 3}


def _ms(year: int, month: int = 1) -> str:
    import datetime as dt
    return str(int(dt.datetime(year, max(1, min(12, month)), 1).timestamp() * 1000))


def extract_education(text: str) -> list[dict]:
    """从简历文本保守抽取教育经历（学校 + 学历 + 时间），抽不准就不填"""
    out = []
    for line in text.splitlines():
        m = re.search(r"([\u4e00-\u9fa5A-Za-z]{2,20}(?:大学|学院|学校|University|College))", line)
        if not m:
            continue
        school = m.group(1)
        degree = None
        for kw, lvl in DEGREE_MAP.items():
            if kw in line:
                degree = lvl
                break
        if not degree:
            continue
        item = {"school": school, "degree": degree}
        dm = DATE_RANGE_RE.search(line)
        if dm:
            sy = int(dm.group(1)); sm = int(dm.group(2) or 1)
            if dm.group(3):
                ey = int(dm.group(3)); em = int(dm.group(4) or 1)
            else:
                import datetime as dt
                ey, em = dt.date.today().year, dt.date.today().month
            item["start_time"] = _ms(sy, sm)
            item["end_time"] = _ms(ey, em)
        # 同校去重：优先保留带时间的一条
        exist = next((x for x in out if x["school"] == school), None)
        if exist:
            if "start_time" in item and "start_time" not in exist:
                exist.update(item)
            continue
        out.append(item)
        if len(out) >= 3:
            break
    return out


def extract_careers(text: str) -> list[dict]:
    """从简历文本保守抽取工作经历（时间 + 公司 + 职位）

    兼容两种版式：
      行首时间：2025.07-2026.07 北京哲科软件 AI产品经理
      行尾时间：AI 产品经理 北京哲科软件｜北京银行… 2025.07–2026.07
    """
    company_re = re.compile(
        r"[\u4e00-\u9fa5A-Za-z]{2,16}(?:有限公司|有限责任公司|公司|科技|集团|银行|软件|数据|网络|信息|研究院|事务所|工作室|中心|学院)"
    )
    out = []
    for line in text.splitlines():
        dm = DATE_RANGE_RE.search(line)
        if not dm:
            continue
        # 教育行不算工作经历
        if any(k in line for k in ("大学", "学院", "学校")) and any(
                k in line for k in ("本科", "硕士", "博士", "大专", "高中")):
            continue
        before = line[:dm.start()].strip(" |｜-–~")
        after = line[dm.end():].strip(" |｜-–~")

        company = title = None
        if len(before) >= 4:  # 时间在行尾
            cm = company_re.search(before)
            if cm:
                company = cm.group(0)
                title = before[:cm.start()].strip(" ｜|·-/")
            elif "｜" in before:
                parts = [x.strip() for x in before.split("｜") if x.strip()]
                if len(parts) >= 2:
                    title, company = parts[0], parts[1]
        elif len(after) >= 4:  # 时间在行首
            cm = company_re.search(after)
            if cm:
                company = cm.group(0)
                title = after[:cm.start()].strip(" ｜|·-/")
            elif "｜" in after:
                parts = [x.strip() for x in re.split(r"[|｜·/]", after) if x.strip()]
                if len(parts) >= 2:
                    company, title = parts[0], parts[1]

        if not company or not title:
            continue
        # 去掉水印残留（如 "AI 产品经理1f" 末尾的 1f）
        title = re.sub(r"[0-9A-Za-z]{1,2}$", "", title).strip()
        title = title[:40]
        company = company[:40]
        if len(title) < 2 or len(company) < 2:
            continue
        sy = int(dm.group(1)); sm = int(dm.group(2) or 1)
        if dm.group(3):
            ey = int(dm.group(3)); em = int(dm.group(4) or 1)
        else:
            import datetime as dt
            ey, em = dt.date.today().year, dt.date.today().month
        out.append({"company": company, "title": title,
                    "start_time": _ms(sy, sm), "end_time": _ms(ey, em),
                    "career_type": 2})
        if len(out) >= 4:
            break
    return out


def build_payload(name: str, contact: dict, attachment_id: str,
                  source_id: str | None = None, text: str | None = None,
                  with_experience: bool = True) -> dict:
    """构造 combined_create 请求体（文本存在时可附带教育/工作经历）"""
    payload = {
        "resume_attachment_id": attachment_id,
        "basic_info": {"name": name},
    }
    if source_id:
        payload["resume_source_id"] = source_id
    mobile = clean_mobile(contact.get("mobile") or "")
    email = clean_email(contact.get("email") or "")
    if mobile:
        payload["basic_info"]["mobile"] = mobile
        payload["basic_info"]["mobile_country_code"] = "CN_1"
    if email:
        payload["basic_info"]["email"] = email
    if with_experience and text:
        edu = extract_education(text)
        careers = extract_careers(text)
        if edu:
            payload["education_list"] = edu
        if careers:
            payload["career_list"] = careers
    return payload


def select_candidates(db: Database, args) -> list[dict]:
    """按命令行参数筛选候选人"""
    if args.ids:
        rows = [db.get_candidate(int(i)) for i in args.ids.split(",") if i.strip().isdigit()]
        return [r for r in rows if r]
    if args.names:
        names = [n.strip() for n in args.names.split(",") if n.strip()]
        result = []
        for n in names:
            rows = db.conn.execute(
                "SELECT * FROM candidates WHERE name = ? AND resume_path IS NOT NULL ORDER BY id DESC",
                (n,),
            ).fetchall()
            result.extend(dict(r) for r in rows)
        return result
    return db.get_pending_hire_candidates(
        platform=args.platform, min_score=args.min_score, limit=args.limit,
    )


def print_candidate_list(candidates: list[dict]) -> None:
    if not candidates:
        print("（没有待入库的候选人）")
        return
    print(f"{'ID':>5}  {'姓名':<10} {'匹配度':>5}  {'当前职位':<22} {'简历文件'}")
    print("-" * 90)
    for c in candidates:
        path = Path(c["resume_path"])
        exists = "✓" if path.exists() else "✗缺失"
        print(f"{c['id']:>5}  {c['name']:<10} {c.get('match_score') or '-':>5}  "
              f"{(c.get('title') or '')[:20]:<22} {exists} {path.name}")


def import_one(candidate: dict, dry_run: bool,
               source_id: str | None = None) -> tuple[str, dict]:
    """入库数据库里的单个候选人，返回 (结果类型, 详情)。类型: ok / duplicate / fail"""
    path = Path(candidate["resume_path"])
    if not path.exists():
        return "fail", {"reason": f"简历文件不存在: {path}"}

    if path.suffix.lower() == ".pdf" and classify_resume(path) == "portfolio":
        return "portfolio", {"file": str(path), "reason": "图片为主/作品集，未入库"}

    text = unicodedata.normalize("NFKC", parse_resume(path) or "")
    contact = extract_contact(text)
    fname_name = name_from_filename(path.name)
    name = guess_name(text, candidate.get("name") or fname_name or path.stem.rstrip("_"),
                      trusted_name=fname_name)

    if dry_run:
        return "dry-run", {
            "name": name, "mobile": contact["mobile"], "email": contact["email"],
            "source_id": source_id, "file": str(path), "text_chars": len(text),
        }

    try:
        attachment_id = upload_attachment(str(path))
    except RuntimeError as e:
        return "fail", {"reason": str(e)}

    sha = _file_sha256(path)
    idx = _index_load()
    if sha and sha in (idx.get("hashes") or {}):
        return "duplicate", {"talent_id": (idx["hashes"])[sha], "name": name,
                             "mobile": contact["mobile"], "email": contact["email"],
                             "file": str(path), "note": "同一文件已入库过（跳过）"}

    existing = find_existing_talent(contact)
    if not existing and not contact.get("mobile") and not contact.get("email") and name_ok:
        cand = (idx.get("names") or {}).get(name) or []
        if len(cand) == 1:
            # 仅凭姓名不足以判定同一人：还要学校一致
            schools_new = {str(e.get("school") or "") for e in extract_education(text) if e.get("school")}
            schools_old = _talent_schools(cand[0])
            if schools_new and schools_old and (schools_new & schools_old):
                existing = cand[0]
                print(f"   ↺ 无联系方式，姓名+学校一致，归并到 {existing}")
            else:
                add_review(name, cand[0], str(path), "同名但无联系方式且学校不一致，未自动归并")
    if existing:
        if path.name in talent_attachment_names(existing):
            return "merged", {"talent_id": existing, "name": name,
                              "mobile": contact["mobile"], "email": contact["email"],
                              "note": "人才已存在且简历已挂载，跳过追加"}
        try:
            append_resume(existing, attachment_id, name if (fname_name and name_ok) else "")
        except RuntimeError as e:
            return "fail", {"reason": str(e)}
        return "merged", {"talent_id": existing, "name": name,
                          "mobile": contact["mobile"], "email": contact["email"],
                          "note": "人才已存在，已追加新简历"}

    payload = build_payload(name, contact, attachment_id, source_id, text=text)
    try:
        talent_id = create_talent(payload)
    except RuntimeError as e:
        msg = str(e)
        if any(k in msg for k in ("重复", "已存在", "duplicate")):
            return "duplicate", {"reason": msg}
        return "fail", {"reason": msg}

    return "ok", {"talent_id": talent_id, "name": name,
                  "mobile": contact["mobile"], "email": contact["email"]}


PORTFOLIO_HINTS = ("作品集", "作品", "portfolio", "作品展")


def classify_resume(path: Path) -> str:
    """判断 PDF 是「简历」还是「作品集」：文件名含作品集，或整页几乎无文字（图片为主）"""
    name_lower = path.name.lower()
    if any(k.lower() in name_lower for k in PORTFOLIO_HINTS):
        return "portfolio"
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            pages = len(pdf.pages)
            if pages == 0:
                return "resume"
            sample = pdf.pages[:8]
            chars = sum(len(p.chars) for p in sample)
            imgs = sum(len(p.images) for p in sample)
            if imgs > 0 and chars / len(sample) < 20:
                return "portfolio"
    except Exception:
        pass
    return "resume"


IMPORT_LOCK = DATA_DIR / "import.lock"


def import_file(path: Path, source_id: str | None = None,
                db_name: str = "") -> tuple[str, dict]:
    """入库入口：全局锁保证「查指纹 → 上传 → 记录」不会并发重复"""
    with file_lock(IMPORT_LOCK, timeout=180):
        return _import_file_impl(path, source_id, db_name)


def _import_file_impl(path: Path, source_id: str | None = None,
                      db_name: str = "") -> tuple[str, dict]:
    """入库任意一个简历文件（不依赖数据库），返回 (结果类型, 详情)"""
    if not path.exists():
        return "fail", {"reason": f"文件不存在: {path}"}
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return "fail", {"reason": f"不支持的文件类型: {path.suffix}"}

    if path.suffix.lower() == ".pdf" and classify_resume(path) == "portfolio":
        return "portfolio", {"file": str(path), "reason": "图片为主/作品集，未入库"}

    text = unicodedata.normalize("NFKC", parse_resume(path) or "")
    contact = extract_contact(text)
    fname_name = name_from_filename(path.name)
    name = guess_name(text, db_name or fname_name or path.stem, trusted_name=fname_name)
    name_ok = name_is_valid(name)
    if not name_ok:
        # 闸门：疑似栏目词/称谓/噪声 -> 不写入人才姓名，改用待确认占位名
        tail = (contact.get("mobile") or "")[-4:]
        review_name = f"待确认{tail}" if tail else "待确认"
        name = review_name

    try:
        attachment_id = upload_attachment(str(path))
    except RuntimeError as e:
        return "fail", {"reason": str(e)}

    sha = _file_sha256(path)
    idx = _index_load()
    if sha and sha in (idx.get("hashes") or {}):
        return "duplicate", {"talent_id": (idx["hashes"])[sha], "name": name,
                             "mobile": contact["mobile"], "email": contact["email"],
                             "file": str(path), "note": "同一文件已入库过（跳过）"}

    existing = find_existing_talent(contact)
    if not existing and not contact.get("mobile") and not contact.get("email") and name_ok:
        cand = (idx.get("names") or {}).get(name) or []
        if len(cand) == 1:
            # 仅凭姓名不足以判定同一人：还要学校一致
            schools_new = {str(e.get("school") or "") for e in extract_education(text) if e.get("school")}
            schools_old = _talent_schools(cand[0])
            if schools_new and schools_old and (schools_new & schools_old):
                existing = cand[0]
                print(f"   ↺ 无联系方式，姓名+学校一致，归并到 {existing}")
            else:
                add_review(name, cand[0], str(path), "同名但无联系方式且学校不一致，未自动归并")
    if existing:
        if path.name in talent_attachment_names(existing):
            return "merged", {"talent_id": existing, "name": name,
                              "mobile": contact["mobile"], "email": contact["email"],
                              "file": str(path), "note": "人才已存在且简历已挂载，跳过追加"}
        try:
            append_resume(existing, attachment_id, name if (fname_name and name_ok) else "")
        except RuntimeError as e:
            return "fail", {"reason": str(e)}
        _index_record(sha, name, existing)
        if not name_ok:
            add_review(name, existing, str(path), f"姓名不可信: {name}")
        return "merged", {"talent_id": existing, "name": name,
                          "mobile": contact["mobile"], "email": contact["email"],
                          "file": str(path), "note": "人才已存在，已追加新简历"}

    payload = build_payload(name, contact, attachment_id, source_id, text=text)
    try:
        talent_id = create_talent(payload)
    except RuntimeError as e:
        msg = str(e)
        if any(k in msg for k in ("重复", "已存在", "duplicate")):
            return "duplicate", {"reason": msg}
        return "fail", {"reason": msg}

    _index_record(sha, name, talent_id)
    if not name_ok:
        add_review(name, talent_id, str(path), "姓名不可信（已用占位名）")
    return "ok", {"talent_id": talent_id, "name": name,
                  "mobile": contact["mobile"], "email": contact["email"],
                  "file": str(path)}


def current_user() -> dict:
    """当前 lark-cli 登录用户（name + open_id）"""
    result = lark_json(["auth", "status"], fmt=False)
    u = ((result.get("identities") or {}).get("user")) or {}
    return {"name": u.get("userName") or "", "open_id": u.get("openId") or ""}


def job_manager_ids(job_id: str) -> list[str]:
    """职位的负责人/招聘负责人/协助人/用人经理 open_id 列表

    返回结构：data.job_detail.{recruiter, hiring_manager_list, assistant_list}
    """
    result = lark_json([
        "api", "GET", f"/open-apis/hire/v1/jobs/{job_id}/get_detail", "--as", "bot",
        "--params", json.dumps({"user_id_type": "open_id"}),
    ])
    if not result.get("ok"):
        return []
    data = result.get("data") or {}
    jd = data.get("job_detail") or data.get("job") or data
    ids: list[str] = []

    def add(item):
        if isinstance(item, dict):
            uid = item.get("id") or item.get("user_id") or item.get("open_id")
            if uid:
                ids.append(str(uid))
        elif isinstance(item, (str, int)):
            ids.append(str(item))

    add(jd.get("recruiter"))
    for key in ("hiring_manager_list", "assistant_list", "job_managers",
                "job_recruiters", "job_assistants", "job_interviewers",
                "manager_list", "recruiter_list"):
        for item in (jd.get(key) or []):
            add(item)
    return ids


def fetch_my_jobs(refresh: bool = False) -> list[dict]:
    """当前用户负责的岗位（按负责人/协助人/用人经理匹配），带本地缓存"""
    me = current_user()
    uid = me.get("open_id")
    if not uid:
        return []
    if not refresh and MY_JOBS_CACHE.exists():
        try:
            cached = json.loads(MY_JOBS_CACHE.read_text(encoding="utf-8"))
            if cached.get("open_id") == uid and cached.get("jobs") is not None:
                return cached["jobs"]
        except json.JSONDecodeError:
            pass

    jobs = fetch_jobs()
    mine = []
    for j in jobs:
        try:
            if uid in job_manager_ids(j["id"]):
                mine.append(j)
        except RuntimeError:
            continue
    MY_JOBS_CACHE.write_text(json.dumps(
        {"open_id": uid, "name": me.get("name"), "jobs": mine},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return mine


def fetch_jobs(force: bool = False) -> list[dict]:
    """拉取职位列表（用于岗位选择），带 1 小时磁盘缓存（167 个岗位要 30s）"""
    import time as _t
    if not force and JOBS_CACHE_FILE.exists():
        try:
            cached = json.loads(JOBS_CACHE_FILE.read_text(encoding="utf-8"))
            if _t.time() - float(cached.get("ts", 0)) < 3600 and cached.get("jobs"):
                return cached["jobs"]
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    result = lark_json([
        "api", "GET", JOB_LIST_API, "--as", "bot",
        "--params", json.dumps({"page_size": 20}), "--page-all", "--page-limit", "0",
    ])
    if not result.get("ok"):
        err = result.get("error") or {}
        raise RuntimeError(err.get("message") or "职位列表获取失败")
    data = result.get("data") or {}
    jobs = []
    for it in data.get("items") or []:
        if it.get("active_status") not in (1, None):
            continue
        cities = it.get("city_list") or []
        city = ""
        if cities and isinstance(cities[0], dict):
            name = cities[0].get("name") or {}
            city = name.get("zh_cn", "") if isinstance(name, dict) else str(name)
        jobs.append({
            "id": str(it.get("id")), "title": it.get("title") or "", "city": city,
            "description": (it.get("description") or "")[:400],
            "requirement": (it.get("requirement") or "")[:400],
        })
    try:
        JOBS_CACHE_FILE.write_text(json.dumps({"ts": _t.time(), "jobs": jobs},
                                              ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return jobs


def pick_job_interactive(jobs: list[dict]) -> str | None:
    """交互式选择职位，返回 job_id"""
    if not jobs:
        print("⚠ 没有可选的职位")
        return None
    print("\n可选职位：")
    for i, j in enumerate(jobs, 1):
        print(f"  {i:>2}. {j['title']}  ({j['city']})  id={j['id']}")
    while True:
        raw = input("选择职位编号（回车跳过）: ").strip()
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(jobs):
            return jobs[int(raw) - 1]["id"]
        # 支持直接输入关键词过滤
        hits = [j for j in jobs if raw in j["title"]]
        if len(hits) == 1:
            return hits[0]["id"]
        print("输入有误，请输入编号" + (f"（匹配到 {len(hits)} 个）" if hits else ""))


def match_job(jobs: list[dict], keyword: str) -> str | None:
    """按标题关键词匹配职位"""
    hits = [j for j in jobs if keyword in j["title"]]
    if len(hits) == 1:
        return hits[0]["id"]
    if not hits:
        print(f"⚠ 没有匹配「{keyword}」的职位")
        return None
    print(f"⚠ 「{keyword}」匹配到多个职位，请用 --job-id 指定，或加 --pick-job 手动选：")
    for j in hits:
        print(f"   - {j['title']} ({j['city']}) id={j['id']}")
    return None


JOB_TITLE_STOPWORDS = {
    "上海", "北京", "深圳", "杭州", "广州", "成都", "武汉", "西安", "苏州", "南京",
    "长沙", "重庆", "天津", "厦门", "青岛", "济南", "合肥", "福州", "大连", "宁波",
    "实习", "实习生", "全职", "校招", "社招", "急招", "岗位", "职位", "招聘",
}


def _job_tokens(title: str) -> list[str]:
    """从职位标题提取关键词：整词 + 二元片段（简历里常只出现其中一部分）"""
    cleaned = re.sub(r"[（）()【】\[\]·/、,，]", " ", title)
    tokens = []
    for run in re.findall(r"[\u4e00-\u9fa5]{2,8}", cleaned):
        if run in JOB_TITLE_STOPWORDS:
            continue
        tokens.append(run)
        # 二元片段：品类运营 -> 品类 / 类运 / 运营
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in JOB_TITLE_STOPWORDS:
                tokens.append(bg)
    # 去重保序
    seen, out = set(), []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def score_resume_for_job(text: str, title: str) -> int:
    """按简历文本给职位打分：职位关键词命中次数 + 期望职位加权"""
    score = 0
    for tok in _job_tokens(title):
        n = text.count(tok)
        if n:
            score += min(n, 3) * (2 if len(tok) >= 3 else 1)
    m = re.search(r"期望职位[:：]?\s*([^\n|]{2,30})", text)
    if m:
        for tok in _job_tokens(title):
            if tok in m.group(1):
                score += 3
    return score


def match_best_job(text: str, jobs: list[dict], min_score: int = 4):
    """在重点岗位里为简历挑最匹配的一个，返回 (job_id, title, score) 或 None"""
    best = None
    for j in jobs:
        sc = score_resume_for_job(text, j["title"])
        if sc <= 0:
            continue
        if best is None or sc > best[2]:
            best = (j["id"], j["title"], sc)
    if best and best[2] >= min_score:
        return best
    return None


BOSS_JOB_RE = re.compile(r"【([^】]+)】")


def extract_boss_job(filename: str) -> str | None:
    """从 Boss/脉脉 下载文件名里提取岗位名，如
    【社区产品经理（示例业务）_上海 22-40K】李四 27年应届生.pdf -> 社区产品经理（示例业务）
    """
    m = BOSS_JOB_RE.search(filename)
    if not m:
        return None
    name = m.group(1).split("_")[0].strip()
    return name or None


def load_aliases() -> dict:
    if not JOB_ALIAS_FILE.exists():
        return {}
    try:
        return json.loads(JOB_ALIAS_FILE.read_text(encoding="utf-8")).get("aliases", {})
    except json.JSONDecodeError:
        return {}


def save_alias(boss_job: str, job_id: str, title: str) -> None:
    def _mut(d: dict) -> None:
        d.setdefault("aliases", {})[boss_job] = {
            "job_id": job_id, "title": title,
            "mapped_at": datetime.now().isoformat(timespec="seconds")}
    update_json(JOB_ALIAS_FILE, _mut, default={"aliases": {}})
    print(f"✓ 已建立映射: {boss_job} → {title} (id={job_id})")


def resolve_alias(boss_job: str | None) -> dict | None:
    if not boss_job:
        return None
    return load_aliases().get(boss_job)


def record_job_interest(boss_job: str | None) -> None:
    """累计记录「这个岗位名在简历池里出现了几次」，供报告页展示岗位分布"""
    if not boss_job:
        return
    def _mut(data: dict) -> None:
        entry = data.get(boss_job) or {"count": 0}
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_seen"] = datetime.now().isoformat(timespec="seconds")
        data[boss_job] = entry
    try:
        update_json(JOB_INTEREST_FILE, _mut, default={})
    except (OSError, TimeoutError):
        pass


def show_aliases() -> int:
    aliases = load_aliases()
    if not aliases:
        print("（尚未建立岗位映射；用 --map-job \"Boss岗位名\" \"飞书职位关键词\" 建立）")
    else:
        print(f"岗位映射（{len(aliases)} 条）：")
        for boss, info in aliases.items():
            print(f"   - {boss}  →  {info.get('title')} (id={info.get('job_id')})")
    # 待映射清单
    try:
        interest = json.loads(JOB_INTEREST_FILE.read_text(encoding="utf-8")) if JOB_INTEREST_FILE.exists() else {}
    except json.JSONDecodeError:
        interest = {}
    pending = {k: v for k, v in interest.items() if k not in aliases}
    if pending:
        print("\n待映射（简历池里出现过但还没对应飞书职位）：")
        for k, v in sorted(pending.items(), key=lambda x: -x[1].get("count", 0)):
            print(f"   - {k}  （出现 {v.get('count')} 次）")
    return 0


JOB_FAMILIES = [
    ("产品经理", ("产品经理",)),
    ("算法", ("算法", "AI Infra", "机器学习")),
    ("设计", ("设计",)),
    ("运营", ("运营", "社群")),
    ("研发", ("开发", "工程师", "全栈", "前端", "后端", "服务端", "测试")),
    ("HR", ("HR", "招聘", "人事")),
    ("合规", ("合规", "法务")),
    ("资料库", ("资料库", "资料")),
    ("数据", ("数据",)),
    ("客服", ("客服",)),
]


def add_review(name: str, talent_id: str, file: str, reason: str) -> None:
    """姓名/岗位不可信时进待确认队列（不污染人才库命名）"""
    def _mut(d: dict) -> None:
        items = d.setdefault("items", [])
        if any(i.get("talent_id") == str(talent_id) for i in items):
            return
        items.append({"name": name, "talent_id": str(talent_id), "file": file,
                      "reason": reason, "at": datetime.now().isoformat(timespec="seconds")})
        d["items"] = items[-300:]
    update_json(REVIEW_FILE, _mut, default={"items": []})


def load_review() -> list[dict]:
    if not REVIEW_FILE.exists():
        return []
    try:
        return json.loads(REVIEW_FILE.read_text(encoding="utf-8")).get("items", [])
    except json.JSONDecodeError:
        return []


def add_unassigned(name: str, talent_id: str, file: str, reason: str,
                   suggest: dict | None = None, applied: str | None = None) -> None:
    """记录「入库了但没建投递」的候选人，供 HR 事后补投"""
    def _mut(d: dict) -> None:
        items = d.setdefault("items", [])
        if any(str(i.get("talent_id")) == str(talent_id) and i.get("applied") == applied
               for i in items):
            return
        items.append({
            "name": name, "talent_id": str(talent_id), "file": file,
            "reason": reason, "suggest": suggest or {}, "applied": applied,
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        d["items"] = items[-500:]
    update_json(UNASSIGNED_FILE, _mut, default={"items": []})


def update_unassigned_path(talent_id: str, new_path: str) -> None:
    """简历归档后把待分配条目里的路径更新为新位置"""
    def _mut(d: dict) -> None:
        for item in d.get("items", []):
            if str(item.get("talent_id")) == str(talent_id):
                item["file"] = new_path
    try:
        update_json(UNASSIGNED_FILE, _mut, default={"items": []})
    except (OSError, TimeoutError):
        pass


def load_unassigned() -> list[dict]:
    if not UNASSIGNED_FILE.exists():
        return []
    try:
        return json.loads(UNASSIGNED_FILE.read_text(encoding="utf-8")).get("items", [])
    except json.JSONDecodeError:
        return []


def save_unassigned(items: list[dict]) -> None:
    UNASSIGNED_FILE.write_text(json.dumps({"items": items[-200:]}, ensure_ascii=False, indent=1),
                               encoding="utf-8")


def add_correction(summary: dict, job_id: str, title: str, source: str = "learn") -> None:
    """记录一条「这类简历应该去这个岗位」的校准示例（喂给未来 LLM 判断）"""
    def _mut(d: dict) -> None:
        examples = d.setdefault("examples", [])
        name = (summary or {}).get("name") or ""
        if any((e.get("summary") or {}).get("name") == name and str(e.get("job_id")) == str(job_id)
               for e in examples):
            return
        examples.append({
            "summary": summary, "job_id": str(job_id), "title": title,
            "source": source, "at": datetime.now().isoformat(timespec="seconds"),
        })
        d["examples"] = examples[-100:]
    update_json(CORRECTIONS_FILE, _mut, default={"examples": []})


def load_corrections(limit: int = 3) -> list[dict]:
    if not CORRECTIONS_FILE.exists():
        return []
    try:
        data = json.loads(CORRECTIONS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return (data.get("examples") or [])[-limit:]


TEST_FILE_MARKERS = ("测试", "演示", "压测", "内存测试", "验收", "续跑测试", "坏文件")


def is_test_entry(entry: dict) -> bool:
    """测试用文件产生的记录（不计入统计）"""
    name = str(entry.get("file") or "")
    return any(m in name for m in TEST_FILE_MARKERS)


def job_family(title: str) -> str:
    t = title or ""
    for fam, kws in JOB_FAMILIES:
        if any(kw.lower() in t.lower() for kw in kws):
            return fam
    return "其他"


def _job_title_of(job_id: str) -> str:
    if not job_id:
        return ""
    cache = DATA_DIR / "job_title_cache.json"
    title_map = {}
    if cache.exists():
        try:
            title_map = json.loads(cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            title_map = {}
    if job_id in title_map:
        return title_map[job_id]
    r = lark_json(["api", "GET", f"/open-apis/hire/v1/jobs/{job_id}", "--as", "bot"])
    j = (r.get("data") or {}).get("job") or {}
    title = j.get("title") or ""
    if title:
        title_map[job_id] = title
        cache.write_text(json.dumps(title_map, ensure_ascii=False, indent=1), encoding="utf-8")
    return title


def evaluate_decisions(limit: int = 40) -> dict:
    """对比「我们投的岗位」和「人才最终所在岗位」，产出三档统计"""
    entries = []
    if IMPORT_LOG_FILE.exists():
        for line in IMPORT_LOG_FILE.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    auto_apps = {str(e.get("app_id")) for e in entries if e.get("app_id")}

    rows = []
    seen = set()
    for e in entries:
        tid = e.get("talent_id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        if not e.get("job_id"):
            continue  # 没有预测岗位（未建投递），不参与评估
        pred_title = _job_title_of(str(e.get("job_id") or ""))
        r = lark_json(["api", "GET", "/open-apis/hire/v1/applications", "--as", "bot",
                       "--params", json.dumps({"talent_id": tid, "page_size": 20})])
        actual = []
        for aid in ((r.get("data") or {}).get("items") or []):
            if str(aid) in auto_apps:
                continue
            r2 = lark_json(["api", "GET", f"/open-apis/hire/v1/applications/{aid}", "--as", "bot"])
            jid = ((r2.get("data") or {}).get("application") or {}).get("job_id")
            if jid:
                t = _job_title_of(str(jid))
                if t:
                    actual.append(t)
        if not actual:
            verdict = "无记录"
        elif pred_title and any(a == pred_title for a in actual):
            verdict = "精确"
        elif pred_title and any(job_family(a) == job_family(pred_title) for a in actual):
            verdict = "大类"
        else:
            verdict = "未命中"
        rows.append({"name": e.get("name") or "", "ts": e.get("ts", ""),
                     "pred": pred_title, "actual": actual, "verdict": verdict})

    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    judged = [r for r in rows if r["verdict"] != "无记录"]
    result = {
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "counts": counts, "total": len(rows), "judged": len(judged), "rows": rows,
        "skipped": len(seen) - len(rows),
    }
    DECISION_EVAL_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def show_evaluation() -> int:
    result = evaluate_decisions()
    counts = result["counts"]
    judged = result["judged"] or 1
    print(f"评估样本: {result['judged']} / {result['total']}（无记录 {counts.get('无记录', 0)}）")
    print(f"精确命中: {counts.get('精确', 0)} → {counts.get('精确', 0)/judged*100:.0f}%")
    print(f"大类命中: {counts.get('精确', 0)+counts.get('大类', 0)} → "
          f"{(counts.get('精确', 0)+counts.get('大类', 0))/judged*100:.0f}%")
    print(f"未命中  : {counts.get('未命中', 0)}")
    for r in result["rows"][:20]:
        print(f"  {r['name']:<8} 预测={r['pred'] or '-':<22} 实际={'、'.join(r['actual']) or '-':<28} {r['verdict']}")
    return 0


def load_llm_config() -> dict:
    """LLM 配置（data/llm_config.json）：{enabled, base_url, model, api_key}"""
    if not LLM_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(LLM_CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def llm_enabled() -> bool:
    cfg = load_llm_config()
    return bool(cfg.get("enabled")) or bool(os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))


def list_llm_models(base_url: str, api_key: str) -> list[str]:
    """拉取服务商可用模型列表（OpenAI 兼容 /models）"""
    import urllib.request

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("data") or []
        return [str(x.get("id")) for x in items if x.get("id")]
    except Exception as e:
        print(f"⚠ 拉取模型列表失败: {e}")
        return []


def setup_llm_interactive() -> int:
    """交互式配置 LLM 选岗（默认关闭，按需开启）"""
    import getpass

    print("LLM 语义选岗是可选功能：开启后，入库时用大模型判断该投哪个岗位；")
    print("不开启则走关键词匹配 + 岗位映射（零成本、无需 key）。")
    ans = input("\n启用 LLM 选岗？[y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        cfg = load_llm_config()
        cfg["enabled"] = False
        LLM_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        print("✓ 已关闭 LLM 选岗（纯自动入库 + 关键词匹配）")
        return 0

    base = input("API 地址 [https://api.deepseek.com/v1]: ").strip() or "https://api.deepseek.com/v1"
    key = getpass.getpass("API Key（输入不回显）: ").strip()
    if not key:
        print("✗ 未输入 key")
        return 1

    models = list_llm_models(base, key)
    if models:
        print("\n可用模型：")
        for i, m in enumerate(models, 1):
            print(f"  {i:>2}. {m}")
        raw = input("选择模型编号（或直接输入模型名）: ").strip()
        model = models[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(models) else (raw or models[0])
    else:
        model = input("模型名（如 deepseek-chat）: ").strip() or "deepseek-chat"

    cfg = {"enabled": True, "base_url": base, "model": model, "api_key": key}
    LLM_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ 已保存：{base} / {model}")
    print("  提示：服务会自动读取该配置，无需重启（也可执行 launchctl kickstart 立即生效）")
    return 0


def show_llm_status() -> int:
    cfg = load_llm_config()
    enabled = llm_enabled()
    key = cfg.get("api_key") or os.environ.get("LLM_API_KEY") or ""
    masked = (key[:6] + "***" + key[-4:]) if len(key) > 12 else ("***" if key else "未配置")
    print(f"LLM 选岗: {'已启用' if enabled else '未启用'}")
    print(f"  接口: {cfg.get('base_url') or os.environ.get('LLM_BASE_URL') or 'https://api.deepseek.com/v1'}")
    print(f"  模型: {cfg.get('model') or os.environ.get('LLM_MODEL') or 'deepseek-chat'}")
    print(f"  Key : {masked}")
    if not enabled:
        print("\n开启方式：--setup-llm（交互配置）；关闭：--llm-off")
    return 0


PHONE_RE = re.compile(r"1[3-9]\d{9}")
EMAIL_SANITIZE_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")   # 脱敏用（宽松）
ID_RE = re.compile(r"\d{17}[\dXx]")


def html_escape(v: str) -> str:
    """HTML 转义（姓名/文件名来自外部）"""
    import html as _html
    return _html.escape(str(v), quote=True)


def mask_phone(v: str) -> str:
    if not v:
        return ""
    return re.sub(r"(1[3-9]\d)\d{4}(\d{4})", r"\1****\2", str(v))


def mask_email(v: str) -> str:
    if not v or "@" not in str(v):
        return str(v or "")
    local, domain = str(v).split("@", 1)
    return (local[:1] + "***@" + domain) if local else str(v)


def harden_permissions() -> None:
    """收紧数据目录与日志权限（简历含个人信息）"""
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    logs = DATA_DIR / "logs"
    try:
        os.chmod(logs, 0o700)
    except OSError:
        pass
    import itertools
    for f in itertools.chain(DATA_DIR.glob("*.json"), DATA_DIR.glob("*.jsonl"), logs.glob("*.log")):
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass


ADDRESS_RE = re.compile(r"(?:[\u4e00-\u9fa5]{2,8}(?:省|自治区|市|区|县|镇|乡)){1,4}[\u4e00-\u9fa5A-Za-z0-9]{0,20}(?:路|街|道|巷|号|弄|室)[^\s，。；]{0,10}")


def sanitize_for_llm(text: str, limit: int = 1200) -> str:
    """发给 LLM 前去掉手机号/邮箱/身份证/URL，只保留内容"""
    t = ID_RE.sub("[证件]", text or "")
    t = PHONE_RE.sub("[手机]", t)
    t = EMAIL_SANITIZE_RE.sub("[邮箱]", t)
    t = ADDRESS_RE.sub("[地址]", t)
    t = re.sub(r"https?://\S+", "[链接]", t)
    return t[:limit]


def llm_chat(prompt: str, timeout: int = 60) -> str:
    """调用 LLM（OpenAI 兼容接口，默认 DeepSeek 官方 API）

    环境变量：
      LLM_API_KEY / DEEPSEEK_API_KEY   密钥
      LLM_BASE_URL      默认 https://api.deepseek.com/v1
      LLM_MODEL         默认 deepseek-chat
    """
    import os
    import urllib.request

    cfg = load_llm_config()
    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or cfg.get("api_key", "")
    )
    if not api_key:
        return ""
    base = (os.environ.get("LLM_BASE_URL") or cfg.get("base_url")
            or "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL") or cfg.get("model") or "deepseek-chat"

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"⚠ LLM 调用失败: {e}")
        return ""


def _extract_json_object(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def resume_summary(name: str, text: str) -> dict:
    """把简历整理成结构化摘要（给 LLM 的信噪比更高）"""
    years = ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*年(?:以上)?(?:工作)?经验", text)
    if m:
        years = m.group(1)
    expected = ""
    m = re.search(r"期望职位[:：]?\s*([^\n|]{2,30})", text)
    if m:
        expected = m.group(1).strip()
    edu = extract_education(text)[:1]
    careers = extract_careers(text)[:2]
    return {
        "name": name,
        "years": years,
        "expected": expected,
        "education": [f"{e.get('school','')}/{e.get('degree','')}" for e in edu],
        "careers": [f"{c.get('company','')}·{c.get('title','')}" for c in careers],
    }


def _profile_of(user_id: str) -> dict:
    if not JOB_PROFILE_FILE.exists():
        return {}
    try:
        data = json.loads(JOB_PROFILE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data.get(user_id) or {}


def record_job_choice(job_id: str, title: str, weight: int = 1) -> None:
    """记录「这个人被投到了哪个岗位」，用于学习用户常投岗位（weight=2 表示人工纠正信号）"""
    from datetime import datetime
    me = current_user()
    uid = me.get("open_id") or "default"
    def _mut(data: dict) -> None:
        prof = data.setdefault(uid, {})
        entry = prof.get(job_id) or {"title": title, "count": 0}
        entry["count"] = int(entry.get("count", 0)) + weight
        entry["title"] = title or entry.get("title", "")
        entry["last"] = datetime.now().isoformat(timespec="seconds")
        prof[job_id] = entry
    update_json(JOB_PROFILE_FILE, _mut, default={})


def profile_jobs(limit: int = 8) -> list[dict]:
    """用户历史常投岗位（按次数排序）"""
    me = current_user()
    prof = _profile_of(me.get("open_id") or "default")
    items = sorted(prof.items(), key=lambda kv: -int(kv[1].get("count", 0)))[:limit]
    return [{"id": jid, "title": info.get("title", "")} for jid, info in items]


def llm_pick_job(text: str, jobs: list[dict], name: str = "",
                 applied_job: str | None = None, years: str = "") -> dict | None:
    """用 LLM 在候选岗位里挑最匹配的一个（JD + 结构化摘要 + 历史偏好）"""
    summary = resume_summary(name, text) if name else {}
    job_lines = []
    for j in jobs:
        extra = ""
        if j.get("requirement"):
            extra += f" 要求：{j['requirement'][:120]}"
        if j.get("description"):
            extra += f" 描述：{j['description'][:100]}"
        job_lines.append(f"- {j['title']} (id={j['id']}){extra}")

    corr = load_corrections(3)
    corr_hint = ""
    if corr:
        lines = []
        for c in corr:
            sm = c.get("summary") or {}
            desc = " ".join(str(x) for x in (sm.get("expected"), sm.get("careers"), sm.get("years")) if x)
            lines.append(f"- 类似候选人（{desc[:50]}）→ 应投「{c.get('title')}」")
        corr_hint = "历史校准（HR 认可的正确结果，优先参考）：\n" + "\n".join(lines) + "\n"

    hist = [] if applied_job else profile_jobs(limit=5)
    hist_hint = ""
    if hist:
        hist_hint = "该用户历史上常投的岗位（优先考虑）：" + "、".join(h["title"] for h in hist) + "\n"

    hints = []
    if applied_job:
        hints.append(f"候选人在招聘平台实际投递的岗位是「{applied_job}」，请优先考虑名称/职责相近的岗位")
    if years:
        hints.append(f"候选人有 {years} 年工作经验，不要推荐实习/校招岗位")
    hint_text = ("\n" + "；".join(hints) + "\n") if hints else ""

    prompt = (
        "你是招聘助手。请根据候选人简历，从候选职位中选出最合适的一个。\n"
        f"候选人摘要：{json.dumps(summary, ensure_ascii=False)}\n"
        f"{hint_text}"
        f"简历片段（已脱敏）：\n{sanitize_for_llm(text)}\n\n"
        f"{corr_hint}{hist_hint}"
        f"候选职位：\n" + "\n".join(job_lines) + "\n\n"
        "只返回 JSON：{\"id\": \"职位id\", \"reason\": \"一句话理由\", \"confidence\": 0到1}。"
        "如果都不匹配，返回 {\"id\": \"\", \"reason\": \"...\", \"confidence\": 0}。不要其他内容。"
    )
    if os.environ.get("LLM_DEBUG"):
        print("\n----- 发送给 LLM 的完整提示词 -----")
        print(prompt)
        print("----- 提示词结束 -----\n")

    reply = llm_chat(prompt)
    if os.environ.get("LLM_DEBUG"):
        print("----- LLM 原始回答 -----")
        print(reply)
        print("----- 回答结束 -----\n")
    data = _extract_json_object(reply)
    if not data:
        return None
    jid = str(data.get("id") or "")
    if not jid:
        return None
    title = next((j["title"] for j in jobs if j["id"] == jid), "")
    if not title:
        return None  # 模型幻觉：返回了候选池外的岗位
    import math
    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if not math.isfinite(conf):
        conf = 0.0
    conf = min(1.0, max(0.0, conf))
    return {"id": jid, "title": title, "reason": data.get("reason", ""), "confidence": conf}


def suggest_alias_llm(boss_job: str, jobs: list[dict]) -> list[dict]:
    """用 LLM 给 Boss 岗位名推荐飞书职位（只建议，不保存）"""
    job_lines = "\n".join(f"- {j['title']} (id={j['id']})" for j in jobs)
    prompt = (
        f"招聘平台上有个岗位叫「{boss_job}」，企业飞书招聘系统里有以下职位：\n{job_lines}\n\n"
        "请判断该平台岗位最可能对应哪 1-3 个飞书职位。"
        "只返回 JSON 数组，元素为 {\"title\": ..., \"id\": ..., \"reason\": ...}，不要其他内容。"
    )
    reply = llm_chat(prompt)
    m = re.search(r"\[.*\]", reply, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return []


CAMPUS_HINTS = ("实习", "校招", "应届", "intern", "Intern")
_ALL_JOBS_CACHE: dict = {"ts": 0, "jobs": []}


def all_jobs_cached(ttl: int = 600) -> list[dict]:
    """在招岗位列表缓存（10 分钟）"""
    import time as _t
    if _ALL_JOBS_CACHE["jobs"] and _t.time() - _ALL_JOBS_CACHE["ts"] < ttl:
        return _ALL_JOBS_CACHE["jobs"]
    try:
        jobs = fetch_jobs()
    except RuntimeError:
        return _ALL_JOBS_CACHE["jobs"]
    _ALL_JOBS_CACHE["jobs"] = jobs
    _ALL_JOBS_CACHE["ts"] = _t.time()
    return jobs


def experience_years(text: str) -> float:
    """从简历文本估算工作年限"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*年(?:以上)?(?:工作)?经验", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    for m in re.finditer(r"(\d{4})[.\-/年](\d{1,2})?\s*[-–~至]\s*(?:(\d{4})[.\-/年](\d{1,2})?|至今)", text):
        try:
            start = int(m.group(1))
            end = int(m.group(3)) if m.group(3) else 2026
            if 1990 < start <= end:
                return float(max(0, end - start))
        except (ValueError, TypeError):
            continue
    return 0.0


def jobs_matching_name(applied_job: str | None, all_jobs: list[dict]) -> list[dict]:
    """按「候选人实际投递的岗位名」在飞书职位里找名称相近的（token 重叠）"""
    if not applied_job:
        return []
    target = _job_tokens(applied_job)
    scored = []
    for j in all_jobs:
        overlap = sum(1 for t in target if t and t in j["title"])
        if overlap >= 2:
            scored.append((overlap, j))
    scored.sort(key=lambda x: -x[0])
    return [j for _, j in scored[:3]]


def decide_job(text: str, name: str, applied_job: str | None = None,
               explicit_job: str | None = None, llm_match: bool = True,
               min_conf: float | None = None) -> dict:
    """统一的岗位决策（所有入口共用）

    返回 {job_id, title, via, confidence, reason, suggest}
    via: explicit / alias / rule / llm / llm_low / llm_none / keyword / active / none
    """
    if min_conf is None:
        min_conf = float(os.environ.get("LLM_MIN_CONF", "0.65") or 0.65)

    if explicit_job:
        return {"job_id": explicit_job, "title": _job_title_of(explicit_job),
                "via": "explicit", "confidence": 1.0, "reason": "显式指定"}

    alias = resolve_alias(applied_job)
    if alias and alias.get("job_id"):
        return {"job_id": alias.get("job_id"), "title": alias.get("title", ""),
                "via": "alias", "confidence": 1.0, "reason": f"映射自「{applied_job}」"}

    years = experience_years(text or "")
    extra = jobs_matching_name(applied_job, all_jobs_cached())
    pool = filter_by_experience(extra if extra else candidate_jobs(), years)

    rule = rule_pick_job(applied_job, pool)
    if rule:
        return {"job_id": rule["id"], "title": rule["title"], "via": "rule",
                "confidence": 0.95, "reason": f"投递岗位直配「{applied_job}」"}

    if llm_match and llm_enabled() and pool:
        picked = llm_pick_job(text or "", pool, name, applied_job=applied_job,
                              years=str(years) if years else "")
        if picked:
            if picked.get("confidence", 0) < min_conf:
                return {"job_id": None, "title": picked["title"], "via": "llm_low",
                        "confidence": picked.get("confidence", 0),
                        "reason": picked.get("reason", ""), "suggest": picked}
            return {"job_id": picked["id"], "title": picked["title"], "via": "llm",
                    "confidence": picked.get("confidence", 0), "reason": picked.get("reason", "")}
        return {"job_id": None, "title": "", "via": "llm_none", "confidence": 0.0,
                "reason": "LLM 认为没有合适岗位"}

    best = match_best_job(text or "", pool)
    if best:
        jid, title, score = best
        conf = min(0.9, 0.5 + score / 20)
        # 与 llm 路径统一：低于门槛不建投递，转待分配
        if conf < min_conf:
            return {"job_id": None, "title": title, "via": "keyword",
                    "confidence": conf, "reason": f"关键词匹配 {score} 分（低于门槛 {min_conf}）",
                    "suggest": {"id": jid, "title": title, "confidence": conf}}
        return {"job_id": jid, "title": title, "via": "keyword",
                "confidence": conf, "reason": f"关键词匹配 {score} 分"}

    if not load_focus_jobs():
        active = load_active_job()
        if active:
            conf = 0.5
            title = _job_title_of(active)
            if conf < min_conf:
                return {"job_id": None, "title": title, "via": "active",
                        "confidence": conf, "reason": f"兜底当前岗位（低于门槛 {min_conf}）",
                        "suggest": {"id": active, "title": title, "confidence": conf}}
            return {"job_id": active, "title": title, "via": "active",
                    "confidence": conf, "reason": "兜底当前岗位"}
    return {"job_id": None, "title": "", "via": "none", "confidence": 0.0,
            "reason": "没有可用候选岗位"}


def rule_pick_job(applied_job: str | None, jobs: list[dict]) -> dict | None:
    """投递岗位名与飞书职位名有明确对应时，直接规则命中（不劳烦 LLM）"""
    if not applied_job or not jobs:
        return None
    target = set(_job_tokens(applied_job))
    scored = []
    for j in jobs:
        overlap = len(target & set(_job_tokens(j["title"])))
        scored.append((overlap, j))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return None
    best = scored[0]
    if best[0] < 3:          # 至少 3 个词块重叠，单候选也不豁免
        return None
    if len(scored) > 1 and best[0] <= scored[1][0]:
        return None
    return best[1]


def filter_by_experience(jobs: list[dict], years: float) -> list[dict]:
    """有工作经验的人不投实习/校招岗位"""
    if years >= 1:
        return [j for j in jobs if not any(h.lower() in j["title"].lower() for h in CAMPUS_HINTS)]
    return jobs


def candidate_jobs(refresh: bool = False) -> list[dict]:
    """候选岗位池：我负责的岗位 → 重点岗位 → 历史常投岗位（去重）"""
    my = fetch_my_jobs(refresh=refresh)
    merged = {j["id"]: j for j in my}
    for j in load_focus_jobs():
        merged.setdefault(j["id"], j)
    for j in profile_jobs(limit=8):
        if j.get("title"):
            merged.setdefault(j["id"], j)
    return list(merged.values())


def learn_corrections(limit: int = 40) -> int:
    """学习人工纠正：对比我们建的投递和人才最终所在岗位，把差异记为偏好信号"""
    entries = []
    if IMPORT_LOG_FILE.exists():
        for line in IMPORT_LOG_FILE.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    fixed = 0
    seen = set()
    seen_data = read_json(LEARN_SEEN_FILE, default={"pairs": {}})
    learned_pairs = seen_data.setdefault("pairs", {})
    title_map = {}
    try:
        title_map = {j["id"]: j["title"] for j in fetch_jobs()}
    except RuntimeError:
        title_map = {}
    for e in entries:
        tid = e.get("talent_id")
        chosen = str(e.get("job_id") or "")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        result = lark_json(["api", "GET", "/open-apis/hire/v1/applications", "--as", "bot",
                            "--params", json.dumps({"talent_id": tid, "page_size": 20})])
        ids = ((result.get("data") or {}).get("items")) or []
        jobs = set()
        for aid in ids:
            r2 = lark_json(["api", "GET", f"/open-apis/hire/v1/applications/{aid}", "--as", "bot"])
            job = ((r2.get("data") or {}).get("application") or {}).get("job_id")
            if job:
                jobs.add(str(job))
        for jid in sorted(jobs - {chosen}):
            key = f"{tid}|{jid}"
            if key in learned_pairs:
                continue
            record_job_choice(jid, title_map.get(jid, ""), 2)
            learned_pairs[key] = datetime.now().isoformat(timespec="seconds")
            fixed += 1
            print(f"  发现纠正：{e.get('name')} → job {jid}")
    # 待分配清单：HR 手动补投的 → 记为校准示例
    remaining = []
    for item in load_unassigned():
        tid = str(item.get("talent_id") or "")
        if not tid:
            continue
        result = lark_json(["api", "GET", "/open-apis/hire/v1/applications", "--as", "bot",
                            "--params", json.dumps({"talent_id": tid, "page_size": 20})])
        ids = ((result.get("data") or {}).get("items")) or []
        assigned = []
        for aid in ids:
            r2 = lark_json(["api", "GET", f"/open-apis/hire/v1/applications/{aid}", "--as", "bot"])
            job = ((r2.get("data") or {}).get("application") or {}).get("job_id")
            if job:
                assigned.append(str(job))
        if not assigned:
            remaining.append(item)
            continue
        title = _job_title_of(assigned[0])
        summary = {"name": item.get("name", "")}
        fp = Path(item.get("file") or "")
        if fp.exists():
            try:
                text = unicodedata.normalize("NFKC", parse_resume(fp) or "")
                summary = resume_summary(item.get("name", ""), text)
            except Exception:
                pass
        add_correction(summary, assigned[0], title, source="hr")
        print(f"  ✓ 校准示例：{item.get('name')} → {title}")
    if len(remaining) != len(load_unassigned()):
        save_unassigned(remaining)
        print(f"  （待分配清单还有 {len(remaining)} 人）")

    try:
        update_json(LEARN_SEEN_FILE, lambda d: d.update({"pairs": learned_pairs}), default={"pairs": {}})
    except (OSError, TimeoutError):
        pass
    print(f"✓ 学习完成：{len(seen)} 人，新增 {fixed} 条纠正信号")
    return 0


def explain_matching(path_str: str) -> int:
    """展示一次完整判断过程：解析 → 候选池 → 提示词 → LLM 回答 → 结论"""
    path = Path(os.path.expanduser(path_str))
    if not path.exists():
        print(f"✗ 文件不存在: {path}")
        return 1
    text = unicodedata.normalize("NFKC", parse_resume(path) or "")
    applied = extract_boss_job(path.name)
    years = experience_years(text)
    name = name_from_filename(path.name) or guess_name(text, path.stem)

    print("① 简历解析")
    print(f"   姓名: {name}")
    print(f"   平台投递岗位: {applied or '（文件名里没有）'}")
    print(f"   估算经验: {years} 年")
    print(f"   联系方式: {extract_contact(text)}")

    extra = jobs_matching_name(applied, all_jobs_cached())
    base = candidate_jobs()
    pool = filter_by_experience(extra if extra else base, years)
    print("\n② 候选岗位（已按经验过滤）")
    for j in pool:
        mark = " ← 与投递岗位名称匹配" if j in extra else ""
        print(f"   - {j['title']}{mark}")

    rule = rule_pick_job(applied, pool)
    if rule:
        print("\n③ 规则命中（投递岗位名与飞书职位名明确对应，不需要 LLM）")
        print(f"   → {rule['title']} (id={rule['id']})")
        print("\n④ 结论")
        print(f"   {json.dumps({'id': rule['id'], 'title': rule['title'], 'via': 'rule'}, ensure_ascii=False)}")
        return 0

    print("\n③ LLM 判断")
    os.environ["LLM_DEBUG"] = "1"
    picked = llm_pick_job(text, pool, name, applied_job=applied,
                          years=str(years) if years else "")
    print("④ 结论")
    print(f"   {json.dumps(picked, ensure_ascii=False) if picked else '无匹配'}")
    return 0


def auto_map_jobs(apply: bool = True) -> int:
    """给未映射的平台岗位名自动推荐/建立映射（LLM 推荐 top1）"""
    aliases = load_aliases()
    try:
        interest = json.loads(JOB_INTEREST_FILE.read_text(encoding="utf-8")) if JOB_INTEREST_FILE.exists() else {}
    except json.JSONDecodeError:
        interest = {}
    pending = [k for k in interest if k not in aliases and not k.startswith(("测试", "演示", "演示"))]
    if not pending:
        print("没有待映射的平台岗位名")
        return 0

    jobs = fetch_jobs()
    applied = 0
    pending_maps: list[dict] = []
    for boss_job in pending:
        try:
            sugg = suggest_alias_llm(boss_job, jobs)
        except Exception as e:
            print(f"⚠ {boss_job}: LLM 推荐失败 {e}")
            continue
        if not sugg:
            print(f"· {boss_job}: 无推荐")
            continue
        top = sugg[0]
        title = top.get("title") or ""
        jid = str(top.get("id") or "")
        if not jid or not title:
            continue
        def _norm(t: str) -> str:
            return re.sub(r"[\s·（）()【】\[\]·\-_/]", "", t or "").lower()

        exact = _norm(boss_job) == _norm(title)
        line = f"· {boss_job}  →  {title}（{top.get('reason', '')[:40]}）"
        if not exact:
            pending_maps.append({"boss_job": boss_job, "title": title, "job_id": jid,
                                 "reason": top.get("reason", "")})
            print(line + "  ⏳ 待确认（名称不完全一致）")
            continue
        if apply:
            save_alias(boss_job, jid, title)
            applied += 1
            print(line + "  ✅ 已映射")
        else:
            print(line)
    if apply:
        if pending_maps:
            MAP_SUGGESTIONS_FILE.write_text(
                json.dumps({"items": pending_maps}, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"\n⏳ {len(pending_maps)} 条需要确认：报告页「待确认映射」或 --map-job 手动确认")
        print(f"✓ 自动建立 {applied} 条映射（仅名称完全一致的）")
    return 0


def show_my_jobs() -> int:
    me = current_user()
    jobs = fetch_my_jobs(refresh=True)
    if not jobs:
        print(f"（{me.get('name') or '当前用户'} 没有匹配到负责的岗位——可能缺 hire:job.composite_info:readonly 权限）")
        return 0
    print(f"{me.get('name')} 负责的岗位（{len(jobs)} 个）：")
    for j in jobs:
        print(f"   - {j['title']} (id={j['id']})")
    return 0


def load_focus_jobs() -> list[dict]:
    """读取用户设置的重点岗位列表"""
    if not FOCUS_JOBS_FILE.exists():
        return []
    try:
        data = json.loads(FOCUS_JOBS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [j for j in (data.get("jobs") or []) if j.get("id")]


def save_focus_jobs(jobs: list[dict]) -> None:
    FOCUS_JOBS_FILE.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    print(f"✓ 已保存 {len(jobs)} 个重点岗位：")
    for j in jobs:
        print(f"   - {j['title']} (id={j['id']})")


def show_focus_jobs() -> int:
    jobs = load_focus_jobs()
    if not jobs:
        print("（尚未设置重点岗位；用 --init-jobs 交互设置，或 --focus-jobs \"关键词1,关键词2\"）")
        return 0
    print(f"重点岗位（{len(jobs)} 个）：")
    for j in jobs:
        print(f"   - {j['title']} (id={j['id']})")
    return 0


def init_focus_jobs_interactive() -> int:
    """交互式选择重点岗位（安装后引导用）"""
    try:
        jobs = fetch_jobs()
    except RuntimeError as e:
        print(f"✗ 无法获取职位列表: {e}")
        return 1
    if not jobs:
        print("⚠ 没有可选的职位")
        return 1
    print("你在招的职位（输入编号，可多选，逗号分隔；直接回车跳过）：")
    for i, j in enumerate(jobs, 1):
        print(f"  {i:>3}. {j['title']}  ({j['city']})")
    raw = input("\n重点岗位编号: ").strip()
    if not raw:
        print("已跳过（可随时运行 --init-jobs 再设置）")
        return 0
    picked = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if not part.isdigit():
            continue
        idx = int(part) - 1
        if 0 <= idx < len(jobs):
            picked.append(jobs[idx])
    if not picked:
        print("⚠ 没有有效编号，未保存")
        return 1
    save_focus_jobs(picked)
    return 0


def resolve_job(args) -> str | None:
    """按 --job-id / --job / --pick-job 解析目标职位"""
    if getattr(args, "job_id", None):
        return args.job_id
    keyword = getattr(args, "job", None)
    pick = getattr(args, "pick_job", False)
    if not keyword and not pick:
        return load_active_job()
    try:
        jobs = fetch_jobs()
    except RuntimeError as e:
        print(f"⚠ 无法获取职位列表: {e}")
        return None
    if keyword:
        return match_job(jobs, keyword)
    return pick_job_interactive(jobs)


def create_application(talent_id: str, job_id: str,
                       source_id: str | None = None) -> str:
    """为人才创建投递（把简历投到指定职位）"""
    payload = {"talent_id": talent_id, "job_id": job_id, "delivery_type": 1}
    if source_id:
        payload["resume_source_id"] = source_id
    result = lark_json([
        "api", "POST", APPLICATION_API, "--as", "bot",
        "--data", "-",
    ], stdin=json.dumps(payload, ensure_ascii=False))
    return _unwrap(result, "创建投递")


def save_active_job(job_id: str, title: str = "") -> None:
    """保存「当前在招岗位」，监听模式会自动把新简历投到这个岗位"""
    from datetime import datetime
    ACTIVE_JOB_FILE.write_text(json.dumps({
        "job_id": job_id,
        "title": title,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ 已设置当前岗位: {title or job_id} (job_id={job_id})")


def load_active_job() -> str | None:
    """读取「当前在招岗位」ID，未配置返回 None"""
    if not ACTIVE_JOB_FILE.exists():
        return None
    try:
        data = json.loads(ACTIVE_JOB_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return str(data.get("job_id") or "") or None


def show_active_job() -> int:
    if not ACTIVE_JOB_FILE.exists():
        print("（尚未设置当前岗位；用 --set-job \"关键词\" 设置）")
        return 0
    data = json.loads(ACTIVE_JOB_FILE.read_text(encoding="utf-8"))
    print(f"当前岗位: {data.get('title') or '(无标题)'} (job_id={data.get('job_id')})")
    print(f"设置时间: {data.get('updated_at')}")
    return 0


def has_any_application(talent_id: str) -> bool:
    """该人才在飞书招聘里是否已有任何投递（避免把"已存在投递"误记成待分配）"""
    result = lark_json(["api", "GET", "/open-apis/hire/v1/applications", "--as", "bot",
                        "--params", json.dumps({"talent_id": talent_id, "page_size": 5})])
    return bool(((result.get("data") or {}).get("items")) or [])


def try_create_application(talent_id: str, job_id: str, source_id: str | None) -> str | None:
    """创建投递并打印结果（重复投递等错误不阻塞主流程），成功时返回 application_id"""
    try:
        app_id = create_application(talent_id, job_id, source_id)
        print(f"   📌 已创建投递: application_id={app_id}")
        return app_id
    except RuntimeError as e:
        msg = str(e)
        if any(k in msg for k in ("重复投递", "1002206", "same application", "application exist")):
            print("   📌 该职位下已有活跃投递，跳过")
        else:
            print(f"   ⚠ 创建投递失败: {msg}")
        return None


def log_import(name: str, mobile: str | None, email: str | None,
               source_id: str | None, talent_id: str, file: str,
               job_id: str | None = None, app_id: str | None = None,
               source_name: str | None = None, channel: str = "watch") -> None:
    """把每次入库写进审计日志（JSONL），供 --report 汇总查看"""
    from datetime import datetime
    mask = (os.environ.get("RESUME_LOG_MASK") or "").lower() in ("1", "true", "yes")
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "name": name, "mobile": mask_phone(mobile) if mask else mobile,
        "email": mask_email(email) if mask else email,
        "source_id": source_id, "source_name": source_name, "talent_id": talent_id,
        "file": Path(file).name, "job_id": job_id, "app_id": app_id,
        "channel": channel,
    }
    try:
        import fcntl
        with open(IMPORT_LOG_FILE, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        print(f"⚠ 写入审计日志失败: {e}")
        return
    _rotate_log_if_needed()


def _rotate_log_if_needed(days: int = 90) -> None:
    """超过 90 天的审计记录自动清理（每次写入时抽查，文件超过 2MB 才整理）"""
    try:
        if not IMPORT_LOG_FILE.exists() or IMPORT_LOG_FILE.stat().st_size < 2 * 1024 * 1024:
            return
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        kept = []
        for line in IMPORT_LOG_FILE.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("ts", "") >= cutoff:
                    kept.append(line)
            except json.JSONDecodeError:
                continue
        atomic_write_bytes(IMPORT_LOG_FILE, ("\n".join(kept) + "\n").encode("utf-8"))
    except OSError:
        pass


def _map_suggestions_html() -> str:
    """报告页：待确认的岗位映射建议"""
    if not MAP_SUGGESTIONS_FILE.exists():
        return ""
    try:
        items = json.loads(MAP_SUGGESTIONS_FILE.read_text(encoding="utf-8")).get("items", [])
    except json.JSONDecodeError:
        return ""
    if not items:
        return ""
    rows = "".join(
        f"<tr><td>{it.get('boss_job')}</td><td>{it.get('title')}</td>"
        f"<td>{it.get('reason','')[:40]}</td></tr>" for it in items[:20])
    return f"""
<h3 style="margin-top:28px">待确认映射（{len(items)} 条）</h3>
<div class="meta">平台岗位名与飞书职位名不完全一致，需人工确认：<code>--map-job "平台名" "飞书职位关键词"</code></div>
<table><thead><tr><th>平台岗位</th><th>建议飞书职位</th><th>理由</th></tr></thead><tbody>{rows}</tbody></table>
"""


def _review_html() -> str:
    """报告页：待确认（姓名/岗位不可信）"""
    items = load_review()
    if not items:
        return ""
    rows = "".join(
        f"<tr><td>{it.get('name')}</td><td>{it.get('reason','')[:40]}</td>"
        f"<td>{Path(it.get('file','')).name[:36]}</td></tr>" for it in items[-20:])
    return f"""
<h3 style="margin-top:28px">待确认（{len(items)} 条）</h3>
<div class="meta">姓名或岗位不可信，未写入人才库命名；请在飞书里手动修正</div>
<table><thead><tr><th>占位名</th><th>原因</th><th>文件</th></tr></thead><tbody>{rows}</tbody></table>
"""


def _unassigned_html() -> str:
    """报告页的「待分配」区块：入库但没建投递的候选人"""
    items = load_unassigned()
    if not items:
        return ""
    rows = []
    for it in items[-20:]:
        tid = it.get("talent_id", "")
        url = safe_talent_url(str(tid)) or "#"
        sug = (it.get("suggest") or {})
        sug_text = sug.get("title") or "-"
        rows.append(
            f"<tr><td><a href='{url}' target='_blank'>{it.get('name') or '-'}</a></td>"
            f"<td>{it.get('reason', '')[:30]}</td><td>{sug_text}</td>"
            f"<td>{it.get('at', '')[:10]}</td></tr>")
    return f"""
<h3 style="margin-top:28px">待分配（{len(items)} 人）</h3>
<div class="meta">这些人已入库但没建投递；你在飞书里补投后，下次 --learn 会自动学习成校准示例</div>
<table>
<thead><tr><th>姓名</th><th>原因</th><th>LLM 建议</th><th>日期</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
"""


def _decision_stats_html() -> str:
    """报告页的「匹配有效性」区块（三档统计）"""
    if not DECISION_EVAL_FILE.exists():
        return ('<h3 style="margin-top:28px">匹配有效性</h3>'
                '<div class="meta">还没有评估数据，运行 <code>--evaluate</code> 生成</div>')
    try:
        d = json.loads(DECISION_EVAL_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    counts = d.get("counts") or {}
    judged = d.get("judged") or 1
    exact = counts.get("精确", 0)
    family = counts.get("精确", 0) + counts.get("大类", 0)
    miss = counts.get("未命中", 0)
    rows = []
    for r in (d.get("rows") or [])[:20]:
        color = {"精确": "#2f8f4e", "大类": "#b8860b", "未命中": "#c0392b"}.get(r["verdict"], "#888")
        rows.append(
            f"<tr><td>{r['name']}</td><td>{r['pred'] or '-'}</td>"
            f"<td>{'、'.join(r['actual']) or '-'}</td>"
            f"<td style='color:{color}'>{r['verdict']}</td></tr>")
    return f"""
<h3 style="margin-top:28px">匹配有效性（{d.get('updated_at','')[:10]}）</h3>
<div class="meta">样本 {d.get('judged',0)} / {d.get('total',0)}；
精确 {exact}（{exact/judged*100:.0f}%）；大类 {family}（{family/judged*100:.0f}%）；未命中 {miss}</div>
<table>
<thead><tr><th>姓名</th><th>预测岗位</th><th>实际岗位</th><th>判定</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=4>暂无</td></tr>'}</tbody>
</table>
"""


def _job_stats_html() -> str:
    """报告页的「岗位分布 / 待映射」区块"""
    try:
        interest = json.loads(JOB_INTEREST_FILE.read_text(encoding="utf-8")) if JOB_INTEREST_FILE.exists() else {}
    except json.JSONDecodeError:
        interest = {}
    aliases = load_aliases()

    rows = []
    for name, info in sorted(interest.items(), key=lambda x: -int(x[1].get("count", 0))):
        mapped = aliases.get(name)
        status = (f"<span style='color:#2f8f4e'>已映射 → {mapped['title']}</span>"
                  if mapped else "<span style='color:#c0392b'>待映射</span>")
        rows.append(f"<tr><td>{name}</td><td>{info.get('count', 0)}</td>"
                    f"<td>{info.get('last_seen', '')[:16].replace('T', ' ')}</td><td>{status}</td></tr>")
    body = ''.join(rows) or '<tr><td colspan=4>暂无数据</td></tr>'
    return f"""
<h3 style="margin-top:28px">岗位分布（来自简历文件名）</h3>
<div class="meta">用于判断你最近实际在忙哪些岗位；「待映射」的岗位可执行 --map-job 建立映射</div>
<table>
<thead><tr><th>岗位名（Boss/猎聘）</th><th>出现次数</th><th>最近出现</th><th>映射状态</th></tr></thead>
<tbody>{body}</tbody>
</table>
"""


def digest(days: int = 1) -> int:
    """每日摘要：统计最近 N 天（不含今天）的入库情况，发通知 + 打印"""
    import datetime as dt

    entries = []
    if IMPORT_LOG_FILE.exists():
        for line in IMPORT_LOG_FILE.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    picked = [e for e in entries
              if start <= dt.date.fromisoformat(e["ts"][:10]) < today]
    if not picked:
        msg = f"最近 {days} 天没有入库记录"
    else:
        names = "、".join(e.get("name", "") for e in picked[:10])
        extra = "" if len(picked) <= 10 else f" 等 {len(picked)} 人"
        msg = f"最近 {days} 天入库 {len(picked)} 人：{names}{extra}"
    print(msg)
    notify("简历入库摘要", msg)
    return 0


def generate_report(days: int = 0) -> str:
    """生成导入记录 HTML 报告（按天），返回文件路径

    days=0 表示只看今天；days>0 表示最近 N 天。
    """
    import datetime as dt

    entries = []
    if IMPORT_LOG_FILE.exists():
        for line in IMPORT_LOG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    today = dt.date.today()
    if days > 0:
        start = today - dt.timedelta(days=days - 1)
        entries = [e for e in entries if dt.date.fromisoformat(e["ts"][:10]) >= start]
        label = f"最近 {days} 天"
    else:
        entries = [e for e in entries if e["ts"][:10] == today.isoformat()]
        label = "今天"

    entries = [e for e in entries if not is_test_entry(e)]
    entries.sort(key=lambda e: e["ts"], reverse=True)

    rows = []
    for e in entries:
        url = safe_talent_url(e["talent_id"]) or "#"
        rows.append(
            f"<tr><td>{html_escape(str(e['ts'][11:16]))}</td><td>{html_escape(str(e['name']))}</td>"
            f"<td>{mask_phone(e.get('mobile')) or '-'}</td>"
            f"<td>{mask_email(e.get('email')) or '-'}</td>"
            f"<td>{e.get('source_name') or e.get('source_id') or '-'}</td>"
            f"<td><a href='{url}' target='_blank'>打人才页</a></td>"
            f"<td>{html_escape(str(e.get('file') or ''))}</td></tr>"
        )

    html = f"""<!doctype html>
<html lang="zh"><meta charset="utf-8">
<title>飞书招聘入库记录 · {label}</title>
<style>
body {{ font-family: -apple-system, 'PingFang SC', sans-serif; margin: 32px; color:#222; }}
h2 {{ margin-bottom: 4px; }}
.meta {{ color:#888; font-size: 13px; margin-bottom: 18px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border-bottom: 1px solid #eee; padding: 8px 10px; text-align: left; }}
th {{ background:#fafafa; position: sticky; top: 0; }}
a {{ color:#2f6fdd; text-decoration:none; }}
tr:hover {{ background:#f7faff; }}
</style>
<h2>飞书招聘入库记录</h2>
<div class="meta">{label} · 共 {len(entries)} 条 · 生成于 {dt.datetime.now():%Y-%m-%d %H:%M}</div>
<table>
<thead><tr><th>时间</th><th>姓名</th><th>手机</th><th>邮箱</th><th>来源</th><th>链接</th><th>简历文件</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=7>暂无记录</td></tr>'}</tbody>
</table>
{_job_stats_html()}
{_decision_stats_html()}
{_unassigned_html()}
{_map_suggestions_html()}
{_review_html()}
</html>"""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"imports_{label.replace(' ', '_')}.html"
    out.write_text(html, encoding="utf-8")
    print(f"📄 报告已生成: {out}（{len(entries)} 条）")
    subprocess.run(["open", str(out)], check=False)
    return str(out)


def _frontmost_app() -> str:
    """当前前台应用名"""
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out
    except Exception:
        return ""


def _activate_app(name: str) -> None:
    if not name:
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to set frontmost of process "{name}" to true'],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _prune_talent_tabs(max_keep: int = 6) -> None:
    """只保留最近的 N 个人才页标签（按窗口内顺序，关掉最旧的）"""
    if max_keep <= 0:
        return
    script = f'''
tell application "Google Chrome"
    set keep to {max_keep}
    repeat with w in windows
        set idxList to {{}}
        repeat with i from 1 to (count of tabs of w)
            try
                set u to URL of tab i of w
                if (u contains "/hire/talent/") or (u contains "hire%2Ftalent%2F") then set end of idxList to i
            end try
        end repeat
        set n to count of idxList
        if n > keep then
            set cut to n - keep
            repeat with k from cut to 1 by -1
                try
                    close tab (item k of idxList) of w
                end try
            end repeat
        end if
    end repeat
end tell
'''
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
    except Exception:
        pass


def safe_talent_url(talent_id: str, tmpl: str | None = None) -> str:
    """只输出 https://*.feishu.cn 的人才链接，其他一律不输出。"""
    from urllib.parse import urlparse
    base = tmpl or TALENT_URL_TMPL or ""
    if not base:
        return ""
    url = base.replace("{talent_id}", urllib.parse.quote(str(talent_id)))
    try:
        p = urlparse(url)
        if p.scheme == "https" and p.hostname and p.hostname.endswith(".feishu.cn"):
            return url
    except ValueError:
        pass
    return ""


def open_talent(talent_id: str, tmpl: str = TALENT_URL_TMPL,
                background: bool = False) -> str:
    """在默认浏览器打开人才详情页，返回打开的 URL

    background=True：尽量不抢焦点。macOS 的 open -g 在部分版本仍会把浏览器拉前，
    因此这里额外记录打开前后的前台应用，被抢了就切回去。
    """
    url = tmpl.replace("{talent_id}", urllib.parse.quote(str(talent_id)))
    if not background:
        subprocess.run(["open", url], check=False)
        return url

    prev = _frontmost_app()
    subprocess.run(["open", "-g", url], check=False)
    time.sleep(0.6)
    cur = _frontmost_app()
    if prev and cur and cur != prev and cur.lower().startswith("google chrome"):
        _activate_app(prev)
    try:
        _prune_talent_tabs(int(os.environ.get("RESUME_TABS_MAX", "6")))
    except Exception:
        pass
    return url


def notify(title: str, message: str, sound: bool = False) -> bool:
    """macOS 通知 + 可选提示音

    去重与限流（防止同类提醒反复打扰）：
    - 相同内容 10 分钟内只提醒一次
    - 每分钟最多 5 条
    """
    import hashlib, time as _t
    key = hashlib.md5(f"{title}|{message}".encode()).hexdigest()
    now = _t.time()

    def _mut(d: dict) -> None:
        d.setdefault("sent", {})
        d.setdefault("recent", [])
    data = read_json(NOTIFY_STATE, default={"sent": {}, "recent": []})
    sent = data.get("sent", {})
    recent = [t for t in data.get("recent", []) if now - float(t) < 60]

    last = float(sent.get(key, 0))
    if now - last < 600:
        return False
    if len(recent) >= 5:
        return False

    def _save(d: dict) -> None:
        d.setdefault("sent", {})[key] = now
        d.setdefault("recent", []).append(now)
        d["sent"] = {k: v for k, v in list(d["sent"].items())[-500:]}
        d["recent"] = [t for t in d["recent"] if now - float(t) < 3600][-200:]
    try:
        update_json(NOTIFY_STATE, _save, default={"sent": {}, "recent": []})
    except (OSError, TimeoutError):
        pass

    safe = message.replace('"', "'").replace("\\", "")
    try:
        subprocess.run(["osascript",
                        "-e", "on run argv",
                        "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                        "-e", "end run", "--", safe[:300], str(title)[:60]],
                       check=False, capture_output=True, timeout=5)
    except Exception:
        pass
    if sound:
        try:
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"],
                           check=False, capture_output=True, timeout=5)
        except Exception:
            pass
    return True


def acquire_watch_lock() -> object | None:
    """监听模式的单实例锁：避免手动启动和后台服务同时跑、抢同一批文件"""
    import fcntl

    lock_path = DATA_DIR / "hire_watch.lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.write(str(os.getpid()))
    f.flush()
    return f  # 保持打开，进程退出时自动释放


def _file_sig(path: Path) -> str:
    st = path.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


RESUME_NAME_HINT = re.compile(r"(【.*】|简历|工作\d+年)")

# 按文件名里的平台关键词推断简历来源（Boss 下载件通常不含平台名，默认值兜底）
SOURCE_NAME_HINTS = [
    ("脉脉", "脉脉"),
    ("猎聘", "猎聘"),
    ("前程", "前程无忧"),
    ("智联", "智联招聘"),
    ("领英", "领英"),
    ("拉勾", "拉勾"),
]


def _looks_like_resume(path: Path) -> bool:
    return bool(RESUME_NAME_HINT.search(path.name))


def infer_source_name(filename: str, fallback: str) -> str:
    """根据文件名推断平台来源，推断不出就用默认来源"""
    for keyword, source_name in SOURCE_NAME_HINTS:
        if keyword in filename:
            return source_name
    return fallback


def _decision_note(decision: dict) -> str:
    """把决策结果转成一行可读说明"""
    via = decision.get("via")
    title = decision.get("title") or ""
    conf = decision.get("confidence") or 0
    if via == "alias":
        return f"  🔗 映射岗位: {title}"
    if via == "rule":
        return f"  📌 按投递岗位直配: {title}"
    if via == "llm":
        return f"  🤖 LLM 选岗: {title} (置信度 {conf:.2f}；{decision.get('reason', '')})"
    if via == "llm_low":
        return f"  🤔 LLM 低置信度({conf:.2f})，入库但不建投递（候选：{title}）"
    if via == "llm_none":
        return "  🤔 LLM 认为没有合适岗位，本次不建投递"
    if via == "keyword":
        return f"  🎯 匹配岗位: {title} ({decision.get('reason', '')})"
    if via == "active":
        return f"  ⚙ 兜底当前岗位: {title}"
    if via == "explicit":
        return f"  📎 指定岗位: {title}"
    return "  ⚠ 未匹配到岗位，本次不建投递"


def _current_rss_mb() -> float:
    """当前进程常驻内存（MB）"""
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) / 1024 if out else 0.0
    except Exception:
        return 0.0


def touch_heartbeat(dirs: list[Path] | None = None) -> None:
    """更新心跳时间戳（长任务处理期间也要打点，避免被判为停止）"""
    try:
        atomic_write_json(HEARTBEAT_FILE, {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "dirs": [str(d) for d in (dirs or [])],
        })
    except OSError:
        pass


def _normalize_entry(raw) -> dict:
    """兼容旧格式（纯签名字符串）与新格式（带阶段）"""
    if isinstance(raw, str):
        return {"sig": raw, "stage": "archived"}
    return dict(raw or {})


def _save_watch_entry(key: str, entry: dict, state: dict | None = None) -> None:
    """写盘，并同步更新内存中的 state（避免同进程下一轮扫描重复处理）。"""
    def _mut(d: dict) -> None:
        d[key] = entry
    update_json(WATCH_STATE_FILE, _mut, default={})
    if state is not None:
        state[key] = entry


def _mark_failed(f: Path, d: Path, sig: str, err: Exception,
                state: dict | None = None) -> None:
    """单个文件处理失败：记录阶段并移动到失败区，避免拖垮整个监听"""
    print(f"   ✗ 处理失败: {err}")
    try:
        _save_watch_entry(str(f), {
            "sig": sig, "stage": "failed", "error": str(err)[:200],
            "updated": datetime.now().isoformat(timespec="seconds"),
        }, state=state)
    except Exception:
        pass
    failed_dir = DATA_DIR / "failed"
    try:
        failed_dir.mkdir(parents=True, exist_ok=True)
        if f.exists():
            shutil.move(str(f), str(failed_dir / f.name))
            print(f"   📁 已移入失败区: {failed_dir}")
    except OSError:
        pass


def _process_watch_file(f: Path, d: Path, sig: str, trusted: bool,
                        source_id: str | None, default_source_name: str | None,
                        source_cache: dict, job_id: str | None, llm_match: bool,
                        open_browser: bool, tmpl: str, background_open: bool,
                        move_done: bool, state: dict) -> None:
    """处理单个新文件：可断点续跑（已入库的不重复入库）"""
    print(f"📄 新简历: {f.name}")
    touch_heartbeat([d])
    entry = _normalize_entry(state.get(str(f)))
    resume_from = entry.get("stage") if entry.get("talent_id") else None

    src = source_id
    src_name = default_source_name
    if default_source_name:
        guess = infer_source_name(f.name, default_source_name)
        if guess != default_source_name:
            if guess not in source_cache:
                try:
                    source_cache[guess] = resolve_source_id(guess)
                except RuntimeError:
                    source_cache[guess] = None
            src = source_cache[guess] or source_id
            src_name = guess

    if resume_from in ("imported", "matched", "applied"):
        status, detail = "merged", {
            "talent_id": entry.get("talent_id"), "name": entry.get("name") or "",
            "mobile": entry.get("mobile"), "email": entry.get("email"),
            "note": "断点续跑（跳过重复入库）",
        }
        print(f"   ↻ 续跑未完成步骤（阶段 {resume_from}）")
    else:
        status, detail = import_file(f, src)
        safe_detail = dict(detail)
        if safe_detail.get("mobile"):
            safe_detail["mobile"] = mask_phone(safe_detail["mobile"])
        if safe_detail.get("email"):
            safe_detail["email"] = mask_email(safe_detail["email"])
        print(f"   -> {status}: {json.dumps(safe_detail, ensure_ascii=False)}")
        if status == "portfolio":
            _save_watch_entry(str(f), {"sig": sig, "stage": "portfolio",
                                       "note": "图片为主/作品集，未入库",
                                       "updated": datetime.now().isoformat(timespec="seconds")},
                                   state=state)
            pf_dir = d / "作品集"
            pf_dir.mkdir(exist_ok=True)
            try:
                shutil.move(str(f), str(pf_dir / f.name))
                print(f"   📁 作品集，已移入 {pf_dir}")
            except OSError:
                pass
            notify("已归入作品集", f.name[:40], sound=False)
            return
        if status not in ("ok", "duplicate", "merged"):
            return
        touch_heartbeat([d])
        _save_watch_entry(str(f), {
            "sig": sig, "stage": "imported",
            "talent_id": detail.get("talent_id"), "name": detail.get("name"),
            "mobile": detail.get("mobile"), "email": detail.get("email"),
            "updated": datetime.now().isoformat(timespec="seconds"),
        }, state=state)

    boss_job = extract_boss_job(f.name)
    record_job_interest(boss_job)
    try:
        ftext = parse_resume(f) or ""
    except Exception:
        ftext = ""
    decision = decide_job(ftext, detail.get("name") or "", applied_job=boss_job,
                          explicit_job=job_id, llm_match=llm_match)
    effective_job = decision.get("job_id")
    picked = decision if decision.get("via", "").startswith("llm") else None
    match_note = _decision_note(decision)
    if match_note:
        print(match_note)

    _save_watch_entry(str(f), {
        "sig": sig, "stage": "matched", "talent_id": detail.get("talent_id"),
        "name": detail.get("name"), "mobile": detail.get("mobile"),
        "email": detail.get("email"), "job_id": effective_job,
        "updated": datetime.now().isoformat(timespec="seconds"),
    }, state=state)

    touch_heartbeat([d])
    app_id = None
    if effective_job and detail.get("talent_id"):
        app_id = try_create_application(str(detail["talent_id"]), effective_job, src)
        if app_id:  # 只有真正建成功才记偏好，避免把失败当成"常投"
            record_job_choice(effective_job, _job_title_of(effective_job), 1)

    if app_id:
        _save_watch_entry(str(f), {
            "sig": sig, "stage": "applied", "talent_id": detail.get("talent_id"),
            "name": detail.get("name"), "mobile": detail.get("mobile"),
            "email": detail.get("email"), "job_id": effective_job, "app_id": app_id,
            "updated": datetime.now().isoformat(timespec="seconds"),
        }, state=state)

    if not app_id and detail.get("talent_id"):
        try:
            if not has_any_application(str(detail["talent_id"])):
                add_unassigned(detail.get("name") or "", str(detail["talent_id"]),
                               str(f), match_note.strip() or "未匹配到岗位", picked,
                               applied=boss_job)
        except RuntimeError:
            pass

    log_import(detail.get("name") or "", detail.get("mobile"),
               detail.get("email"), src, str(detail.get("talent_id")),
               str(f), job_id=effective_job, app_id=app_id, source_name=src_name)

    if move_done and trusted:
        done_dir = d / "已入库"
        done_dir.mkdir(exist_ok=True)
        try:
            shutil.move(str(f), str(done_dir / f.name))
            print(f"   📁 已移动到 {done_dir}")
            if detail.get("talent_id"):
                update_unassigned_path(str(detail["talent_id"]), str(done_dir / f.name))
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"   ⚠ 移动失败: {e}")

    _save_watch_entry(str(f), {
        "sig": sig, "stage": "archived", "talent_id": detail.get("talent_id"),
        "name": detail.get("name"), "mobile": detail.get("mobile"),
        "email": detail.get("email"), "job_id": effective_job, "app_id": app_id,
        "updated": datetime.now().isoformat(timespec="seconds"),
    }, state=state)

    if open_browser and detail.get("talent_id"):
        url = open_talent(str(detail["talent_id"]), tmpl, background=background_open)
        print(f"   🌐 已打开: {url}")
    if detail.get("talent_id"):
        job_desc = match_note.replace("  ", "").strip() or "未匹配岗位"
        notify("简历已入库", f"{detail.get('name', '')} · {job_desc[:40]}", sound=True)


def watch_folder(dir_paths: list[Path], source_id: str | None, open_browser: bool,
                 tmpl: str = TALENT_URL_TMPL, move_done: bool = True,
                 job_id: str | None = None, import_existing: bool = False,
                 default_source_name: str | None = "BOSS直聘",
                 background_open: bool = False, llm_match: bool = False) -> int:
    """监听文件夹（可多个）：发现新简历自动入库，支持断点续跑"""
    lock = acquire_watch_lock()
    if lock is None:
        print("✗ 已有监听进程在运行（可能是后台服务），本次不重复启动。")
        print("  如需重启：launchctl kickstart -k gui/$(id -u)/com.example.resume-import")
        return 1
    for d in dir_paths:
        if not d.is_dir():
            print(f"✗ 目录不存在: {d}")
            return 1

    state = read_json(WATCH_STATE_FILE, default={})

    if not state.get("__seeded__"):
        count = 0
        if not import_existing:
            for d in dir_paths:
                for f in d.iterdir():
                    if f.is_file() and f.suffix.lower() in SUPPORTED_SUFFIXES:
                        try:
                            state[str(f)] = _file_sig(f)
                            count += 1
                        except OSError:
                            pass
        state["__seeded__"] = True
        update_json(WATCH_STATE_FILE, lambda d: d.update(state), default={})
        if count:
            print(f"ℹ 已把 {count} 个历史文件记为已处理（不会重复导入；如需导入历史文件加 --import-existing）")

    print(f"👀 监听中: {', '.join(str(d) for d in dir_paths)}")
    print(f"   来源: {'默认' if source_id is None else source_id} | "
          f"浏览器{('后台打开' if background_open else '前台打开') if open_browser else '不打开'} | Ctrl+C 停止\n")

    strict_dirs = {(Path.home() / "Downloads").resolve()}
    source_cache: dict[str, str | None] = {}
    handled = 0
    max_files = int(os.environ.get("RESUME_MAX_FILES_PER_PROCESS", "25"))
    max_rss = float(os.environ.get("RESUME_MAX_RSS_MB", "300"))
    try:
        while True:
            for d in dir_paths:
                trusted = d.resolve() not in strict_dirs
                for f in sorted(d.iterdir()):
                    if f.is_dir() or f.name.startswith(".") or f.suffix.lower() not in SUPPORTED_SUFFIXES:
                        continue
                    if not trusted and not _looks_like_resume(f):
                        continue
                    try:
                        sig = _file_sig(f)
                    except OSError:
                        continue
                    entry = _normalize_entry(state.get(str(f)))
                    if entry.get("sig") == sig and entry.get("stage") == "archived":
                        continue
                    if entry.get("stage") == "failed" and entry.get("sig") == sig:
                        continue  # 失败文件不再反复重试
                    time.sleep(1.5)
                    try:
                        if _file_sig(f) != sig:
                            continue
                    except OSError:
                        continue
                    try:
                        _process_watch_file(f, d, sig, trusted, source_id, default_source_name,
                                            source_cache, job_id, llm_match, open_browser,
                                            tmpl, background_open, move_done, state)
                        handled += 1
                    except Exception as e:
                        _mark_failed(f, d, sig, e, state=state)
                        handled += 1
                    gc.collect()
                    if handled >= max_files or _current_rss_mb() > max_rss:
                        print(f"♻ 内存回收：已处理 {handled} 份 / RSS {_current_rss_mb():.0f}MB，重启监听进程释放内存")
                        return 0
            try:
                atomic_write_json(HEARTBEAT_FILE, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "pid": os.getpid(),
                    "dirs": [str(d) for d in dir_paths],
                })
            except OSError:
                pass
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止监听。")
        return 0


def main() -> int:
    # 保证重定向到文件/管道时日志也能实时输出（监听模式很重要）
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    harden_permissions()
    parser = argparse.ArgumentParser(
        description="把 resume-hunter 已下载的简历一键导入飞书招聘人才库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--check", action="store_true", help="检查应用权限，缺权限时打印开通指引")
    parser.add_argument("--report", action="store_true",
                        help="生成入库记录报告（HTML，自动在浏览器打开）")
    parser.add_argument("--map-job", nargs=2, metavar=("BOSS岗位名", "飞书职位关键词"),
                        help="建立岗位映射，例如 --map-job \"社区产品经理（示例业务）\" \"社区产品经理\"")
    parser.add_argument("--show-alias", action="store_true", help="查看岗位映射与待映射清单")
    parser.add_argument("--suggest-alias", metavar="BOSS岗位名",
                        help="用 LLM 给某个 Boss 岗位名推荐飞书职位（只建议不保存）")
    parser.add_argument("--init-jobs", action="store_true",
                        help="交互式设置重点岗位（安装后引导用）")
    parser.add_argument("--focus-jobs", help="非交互设置重点岗位，逗号分隔关键词")
    parser.add_argument("--show-focus", action="store_true", help="查看重点岗位")
    parser.add_argument("--explain", metavar="简历文件",
                        help="展示某份简历的完整匹配判断过程（含 LLM 提示词与回答）")
    parser.add_argument("--evaluate", action="store_true",
                        help="评估匹配有效性（精确/大类/未命中三档）并缓存结果")
    parser.add_argument("--learn", action="store_true",
                        help="学习人工纠正（对比投递记录，更新岗位偏好画像）")
    parser.add_argument("--auto-map", action="store_true",
                        help="给未映射的平台岗位名自动推荐并建立映射（LLM top1）")
    parser.add_argument("--auto-map-dry", action="store_true",
                        help="只推荐映射，不保存")
    parser.add_argument("--show-my-jobs", action="store_true",
                        help="查看「我负责的岗位」（按职位负责人匹配，需 hire:job.composite_info 权限）")
    parser.add_argument("--setup-llm", action="store_true",
                        help="交互式配置 LLM 选岗（可选功能，默认关闭）")
    parser.add_argument("--llm-off", action="store_true", help="关闭 LLM 选岗")
    parser.add_argument("--llm-status", action="store_true", help="查看 LLM 配置")
    parser.add_argument("--llm-match", action="store_true",
                        help="用 LLM 在重点岗位里选最匹配的（需 ANTHROPIC_API_KEY，比关键词准）")
    parser.add_argument("--set-job", help="设置当前在招岗位（按标题关键词），监听时自动建投递")
    parser.add_argument("--clear-job", action="store_true", help="清除当前岗位设置")
    parser.add_argument("--show-job", action="store_true", help="查看当前岗位设置")
    parser.add_argument("--days", type=int, default=0,
                        help="配合 --report：最近 N 天（默认只统计今天）")
    parser.add_argument("--list", action="store_true", help="列出待入库候选人")
    parser.add_argument("--dry-run", action="store_true", help="只预览解析结果，不调用写接口")
    parser.add_argument("--yes", action="store_true", help="确认执行真实入库（写操作）")
    parser.add_argument("--min-score", type=float, help="只处理匹配度 >= N 的候选人")
    parser.add_argument("--names", help="指定姓名，逗号分隔（如 张三,李四）")
    parser.add_argument("--ids", help="指定数据库 ID，逗号分隔（如 1,2,3）")
    parser.add_argument("--platform", help="平台过滤：boss / maimai")
    parser.add_argument("--limit", type=int, help="最多处理 N 条")
    parser.add_argument("--source-name", default="BOSS直聘",
                        help="简历来源名称（默认 BOSS直聘，按名称自动解析 ID）")
    parser.add_argument("--source-id", help="简历来源 ID（优先于 --source-name）")
    parser.add_argument("--no-source", action="store_true", help="不设置简历来源")
    parser.add_argument("--file", help="导入单个简历文件（支持 PDF/DOC/DOCX）")
    parser.add_argument("--dir", help="批量导入一个目录下的所有简历（配合 --dry-run 先预览）")
    parser.add_argument("--digest", action="store_true",
                        help="生成最近 N 天入库摘要（默认 1 天，配合 --days）")
    parser.add_argument("--watch", nargs="?", const=str(DEFAULT_WATCH_DIR),
                        help=f"监听文件夹（逗号分隔可指定多个），新简历自动入库（默认 {DEFAULT_WATCH_DIR}）")
    parser.add_argument("--import-existing", action="store_true",
                        help="监听模式首次启动时，把目录里已有的简历也导入（默认跳过历史文件）")
    parser.add_argument("--open", action="store_true",
                        help="入库成功后用默认浏览器打开人才详情页")
    parser.add_argument("--open-mode", choices=["foreground", "background"],
                        default="foreground",
                        help="打开方式：foreground 切到前台；background 后台打开不抢焦点")
    parser.add_argument("--talent-url", default=TALENT_URL_TMPL,
                        help="人才详情页 URL 模板，{talent_id} 会被替换")
    parser.add_argument("--no-move", action="store_true",
                        help="监听模式下导入成功后不移动文件到「已入库」子目录")
    parser.add_argument("--job", help="入库后把简历投到匹配的职位（按标题关键词）")
    parser.add_argument("--job-id", help="指定职位 ID（优先于 --job）")
    parser.add_argument("--pick-job", action="store_true", help="入库后交互式选择职位并创建投递")
    args = parser.parse_args()

    if args.check:
        result = check_scopes()
        for e in result.get("errors") or []:
            print(f"⚠ {e}")
        print("权限状态（四态：待申请 / 待管理员审批 / 待用户授权 / 已生效）：")
        for it in result.get("items") or []:
            mark = "✓" if it.get("state") == SCOPE_STATE_ACTIVE else "·"
            print(f"  {mark} [{it.get('state')}] {it.get('scope')} — {it.get('label')}"
                  f"（{'应用级' if it.get('kind') == 'bot' else '用户级'}）")
        print()
        print(f"申请说明可直接粘贴：{result.get('application_reason') or APPLICATION_REASON}")
        missing = result.get("missing") or []
        if result.get("ok") or not missing:
            print("✓ 权限齐全，可以入库（招聘 + 邮件）。")
            return 0
        print("✗ 尚未就绪的权限：")
        for s in missing:
            print(f"  - {s}")
        print()
        print("请一次性申请齐全（避免多轮）：")
        print(f"  1. 打开飞书开发者后台权限页: {CONSOLE_URL}")
        print("  2. 开通招聘（应用级）：hire:talent / hire:attachment / hire:job:readonly / hire:application")
        print("  3. 开通邮件（用户级）：mail:user_mailbox.message:readonly /")
        print("     mail:user_mailbox.message（详情含发件人主题） /")
        print("     mail:user_mailbox.message.attachment:readonly")
        print(f"  4. 申请说明填写：{APPLICATION_REASON}")
        print("  5. 提交后：待管理员审批 → 通过后同事执行 lark-cli auth login（待用户授权）→ 已生效")
        print("  6. 重新检查: .venv/bin/python scripts/hire_import.py --check")
        print()
        print("可选：hire:people_cli（people-cli）、hire:job.composite_info:readonly（我负责的岗位）")
        print("  一键申请(招聘部分): https://open.feishu.cn/app/<APP_ID>/auth"
              "?scopes=hire%3Ajob%3Areadonly%2Chire%3Aapplication%2Chire%3Apeople_cli"
              "%2Chire%3Atalent%2Chire%3Aattachment")
        return 1

    if args.report:
        generate_report(days=max(0, args.days))
        return 0
    if args.llm_status:
        return show_llm_status()
    if args.llm_off:
        cfg = load_llm_config()
        cfg["enabled"] = False
        LLM_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        print("✓ 已关闭 LLM 选岗")
        return 0
    if args.setup_llm:
        return setup_llm_interactive()

    if args.digest:
        return digest(days=max(1, args.days or 1))
    if args.show_alias:
        return show_aliases()
    if args.map_job:
        boss_name, kw = args.map_job
        try:
            jobs = fetch_jobs()
        except RuntimeError as e:
            print(f"✗ 无法获取职位列表: {e}")
            return 1
        jid = match_job(jobs, kw)
        if not jid:
            return 1
        title = next((j["title"] for j in jobs if j["id"] == jid), "")
        save_alias(boss_name.strip(), jid, title)
        return 0
    if args.suggest_alias:
        try:
            jobs = fetch_jobs()
        except RuntimeError as e:
            print(f"✗ 无法获取职位列表: {e}")
            return 1
        results = suggest_alias_llm(args.suggest_alias, jobs)
        if not results:
            print("（没有建议）")
            return 1
        print(f"「{args.suggest_alias}」的候选映射：")
        for r in results:
            print(f"   - {r.get('title')} (id={r.get('id')})  {r.get('reason','')}")
        print("\n确认后执行: --map-job \"%s\" \"<飞书职位关键词>\"" % args.suggest_alias)
        return 0
    if args.evaluate:
        return show_evaluation()
    if args.explain:
        return explain_matching(args.explain)
    if args.learn:
        return learn_corrections()
    if args.auto_map:
        return auto_map_jobs(apply=True)
    if args.auto_map_dry:
        return auto_map_jobs(apply=False)
    if args.show_my_jobs:
        return show_my_jobs()
    if args.show_focus:
        return show_focus_jobs()
    if args.init_jobs:
        return init_focus_jobs_interactive()
    if args.focus_jobs:
        try:
            jobs = fetch_jobs()
        except RuntimeError as e:
            print(f"✗ 无法获取职位列表: {e}")
            return 1
        picked = []
        for kw in args.focus_jobs.replace("，", ",").split(","):
            kw = kw.strip()
            if not kw:
                continue
            jid = match_job(jobs, kw)
            if jid:
                picked.append({"id": jid, "title": next(j["title"] for j in jobs if j["id"] == jid)})
        if not picked:
            print("⚠ 没有匹配到任何职位")
            return 1
        save_focus_jobs(picked)
        return 0
    if args.show_job:
        return show_active_job()
    if args.clear_job:
        ACTIVE_JOB_FILE.unlink(missing_ok=True)
        print("✓ 已清除当前岗位设置")
        return 0
    if args.set_job:
        try:
            jobs = fetch_jobs()
        except RuntimeError as e:
            print(f"✗ 无法获取职位列表: {e}")
            return 1
        jid = match_job(jobs, args.set_job)
        if not jid:
            return 1
        title = next((j["title"] for j in jobs if j["id"] == jid), "")
        save_active_job(jid, title)
        return 0

    # 解析简历来源 ID（如 BOSS直聘 -> 18），单文件/监听/批量模式都要用
    source_id = None
    if not args.no_source:
        if args.source_id:
            source_id = args.source_id
        else:
            try:
                source_id = resolve_source_id(args.source_name)
            except RuntimeError as e:
                print(f"⚠ 解析简历来源「{args.source_name}」失败，将不带来源入库: {e}")
            if source_id is None:
                print(f"⚠ 未找到简历来源「{args.source_name}」，将不带来源入库（可用 --source-id 指定）")

    # 目标职位（用于创建投递）：--job-id / --job / --pick-job
    job_id = resolve_job(args)

    # 模式 B：目录批量导入
    if args.dir:
        d = Path(os.path.expanduser(args.dir))
        if not d.is_dir():
            print(f"✗ 目录不存在: {d}")
            return 1
        files = sorted(f for f in d.iterdir()
                       if f.is_file() and not f.name.startswith(".")
                       and f.suffix.lower() in SUPPORTED_SUFFIXES)
        if not files:
            print(f"（目录里没有简历文件: {d}）")
            return 0
        print(f"目录下共 {len(files)} 份简历：{d}")
        if args.dry_run:
            for f in files:
                text = unicodedata.normalize("NFKC", parse_resume(f) or "")
                contact = extract_contact(text)
                fname_name = name_from_filename(f.name)
                nm = guess_name(text, fname_name or f.stem, trusted_name=fname_name)
                print(f"  - {f.name[:50]:<52} {nm:<8} "
                      f"{(contact['mobile'] or '-'):<13} {(contact['email'] or '-')}")
            print("\n（干跑模式，未调用任何写接口；确认后去掉 --dry-run 执行）")
            return 0
        ok = merged = fail = 0
        for i, f in enumerate(files, 1):
            status, detail = import_file(f, source_id)
            print(f"[{i}/{len(files)}] {f.name[:40]:<42} -> {status}: {detail.get('note', '')}")
            if status == "ok":
                ok += 1
            elif status == "merged":
                merged += 1
            else:
                fail += 1
                notify("简历入库失败", f"{f.name}: {detail.get('reason', '')[:60]}")
            if status in ("ok", "merged"):
                done_dir = d / "已入库"
                done_dir.mkdir(exist_ok=True)
                try:
                    shutil.move(str(f), str(done_dir / f.name))
                except OSError:
                    pass
                boss_job = extract_boss_job(f.name)
                record_job_interest(boss_job)
                log_import(detail.get("name") or "", detail.get("mobile"),
                           detail.get("email"), source_id, str(detail["talent_id"]),
                           str(f), source_name=None if args.no_source else args.source_name)
        print(f"\n汇总: 新建 {ok} / 合并 {merged} / 失败 {fail}")
        return 0 if fail == 0 else 1

    # 模式 B：单文件导入（Boss/猎聘等任意渠道下载的简历）
    if args.file:
        path = Path(os.path.expanduser(args.file))
        if args.dry_run:
            text = parse_resume(path) or ""
            contact = extract_contact(text)
            print(json.dumps({
                "name": guess_name(text, path.stem),
                "mobile": contact["mobile"], "email": contact["email"],
                "source_id": source_id, "file": str(path),
            }, ensure_ascii=False))
            return 0
        src = source_id
        src_name = None if args.no_source else args.source_name
        if not args.no_source:
            guess = infer_source_name(path.name, args.source_name)
            if guess != args.source_name:
                try:
                    src = resolve_source_id(guess) or source_id
                    src_name = guess
                except RuntimeError:
                    pass
        status, detail = import_file(path, src)
        print(f"{status}: {json.dumps(detail, ensure_ascii=False)}")
        if status == "fail":
            notify("简历入库失败", f"{path.name}: {detail.get('reason', '')[:80]}")
        if status in ("ok", "duplicate", "merged") and detail.get("talent_id"):
            boss_job = extract_boss_job(path.name)
            record_job_interest(boss_job)
            effective_job = job_id
            if not effective_job:
                alias = resolve_alias(boss_job)
                if alias:
                    effective_job = alias.get("job_id")
                    print(f"🔗 映射岗位: {alias.get('title')}")
            if not effective_job:
                focus = candidate_jobs()
                if focus:
                    try:
                        ftext = parse_resume(path) or ""
                    except Exception:
                        ftext = ""
                    best = match_best_job(ftext, focus)
                    if best:
                        effective_job, mtitle, mscore = best
                        print(f"🎯 匹配岗位: {mtitle} (分数 {mscore})")
                    else:
                        print("⚠ 未匹配到重点岗位，本次不建投递")
            if not effective_job and not load_focus_jobs():
                effective_job = load_active_job()
            app_id = None
            if effective_job:
                app_id = try_create_application(str(detail["talent_id"]), effective_job, src)
            log_import(detail.get("name") or "", detail.get("mobile"), detail.get("email"),
                       src, str(detail["talent_id"]), str(path), job_id=job_id, app_id=app_id,
                       source_name=src_name)
            if args.open:
                url = open_talent(str(detail["talent_id"]), args.talent_url,
                                  background=(args.open_mode == "background"))
                print(f"🌐 已打开: {url}")
        return 0 if status in ("ok", "duplicate", "merged") else 1

    # 模式 C：监听文件夹，新简历自动入库
    if args.watch is not None and args.dry_run:
        print("✗ --watch 不支持 --dry-run（监听模式会真实入库）；预览请用 --dir --dry-run")
        return 2

    if args.watch is not None:
        dirs = [Path(os.path.expanduser(d.strip())) for d in args.watch.split(",") if d.strip()]
        return watch_folder(dirs, source_id, args.open, args.talent_url,
                            move_done=not args.no_move, job_id=job_id,
                            import_existing=args.import_existing,
                            default_source_name=None if args.no_source else args.source_name,
                            background_open=(args.open_mode == "background"),
                            llm_match=args.llm_match)

    db = Database()
    try:
        candidates = select_candidates(db, args)

        if args.list or (not args.dry_run and not args.yes):
            print(f"待入库候选人（{len(candidates)} 人）：")
            print_candidate_list(candidates)
            if not args.list:
                print()
                print("预览模式：未执行任何写操作。")
                print("确认无误后执行: .venv/bin/python scripts/hire_import.py --yes [同样的过滤参数]")
                print("（先用 --dry-run 查看每个人的解析结果；--check 检查权限）")
            return 0

        # 真实执行或干跑（source_id 已在前面统一解析）
        results = []
        for idx, c in enumerate(candidates, 1):
            status, detail = import_one(c, dry_run=args.dry_run, source_id=source_id)
            print(f"[{idx}/{len(candidates)}] {c['name']:<10} -> {status}: {json.dumps(detail, ensure_ascii=False)}")
            if status in ("ok", "merged") and not args.dry_run:
                db.mark_hire_imported(c["id"], detail["talent_id"])
                app_id = None
                if job_id:
                    app_id = try_create_application(str(detail["talent_id"]), job_id, source_id)
                log_import(detail.get("name") or "", detail.get("mobile"), detail.get("email"),
                           source_id, str(detail["talent_id"]),
                           c.get("resume_path") or "", job_id=job_id, app_id=app_id,
                           source_name=None if args.no_source else args.source_name)
            results.append((c, status, detail))

        # 汇总
        counts = {}
        for _, s, _ in results:
            counts[s] = counts.get(s, 0) + 1
        print()
        print("汇总:", ", ".join(f"{k}×{v}" for k, v in counts.items()))
        if args.dry_run:
            print("（干跑模式，未调用任何写接口）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
