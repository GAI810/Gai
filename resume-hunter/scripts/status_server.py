"""本地状态面板：实时显示自动化是否在跑（http://127.0.0.1:8787）

- /            网页面板（5 秒自动刷新）
- /api/status  JSON（菜单栏图标用）
"""

import html
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

DATA = PROJECT_DIR / "data"
HEARTBEAT = DATA / "heartbeat.json"
MAIL_HEARTBEAT = DATA / "mail_heartbeat.json"
IMPORT_LOG = DATA / "hire_import_log.jsonl"
UNASSIGNED = DATA / "unassigned.json"
REVIEW = DATA / "needs_review.json"
ALIAS = DATA / "job_alias.json"
FAILED_DIR = DATA / "failed"
LLM_CONFIG = DATA / "llm_config.json"
PORT = int(os.environ.get("STATUS_PORT", "8787"))
TOKEN_FILE = DATA / "status_token"


def _load_token() -> str:
    """生成/读取本地鉴权 token（0600），HUD 与本地脚本用"""
    try:
        if TOKEN_FILE.exists():
            return TOKEN_FILE.read_text(encoding="utf-8").strip()
        import secrets
        tok = secrets.token_hex(16)
        TOKEN_FILE.write_text(tok, encoding="utf-8")
        os.chmod(TOKEN_FILE, 0o600)
        return tok
    except OSError:
        return ""


TOKEN = _load_token()


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def age_seconds(ts: str) -> float | None:
    try:
        return (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        return None


TEST_MARKERS = ("测试", "演示", "压测", "内存测试", "验收", "续跑测试", "坏文件")


def is_test(e: dict) -> bool:
    return any(m in str(e.get("file") or "") for m in TEST_MARKERS)


def today_entries() -> list[dict]:
    today = datetime.now().date().isoformat()
    out = []
    if not IMPORT_LOG.exists():
        return out
    for line in IMPORT_LOG.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(e.get("ts", ""))[:10] == today and not is_test(e):
            out.append(e)
    return out


def launchd_state(label: str) -> str:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if label in line:
                pid = line.split()[0]
                return "running" if pid != "-" else "scheduled"
    except Exception:
        pass
    return "unknown"


def collect() -> dict:
    now = datetime.now()
    hb = read_json(HEARTBEAT)
    mhb = read_json(MAIL_HEARTBEAT)
    entries = today_entries()
    last = entries[-1] if entries else {}

    hb_age = age_seconds(hb.get("ts", "")) if hb else None
    mail_age = age_seconds(mhb.get("ts", "")) if mhb else None
    watch_ok = hb_age is not None and hb_age < 60
    mail_ok = mail_age is not None and mail_age < 600

    unassigned = read_json(UNASSIGNED, {"items": []}).get("items", [])
    review = read_json(REVIEW, {"items": []}).get("items", [])
    aliases = read_json(ALIAS, {"aliases": {}}).get("aliases", {})
    llm = read_json(LLM_CONFIG)
    failed = len(list(FAILED_DIR.glob("*"))) if FAILED_DIR.exists() else 0

    return {
        "now": now.isoformat(timespec="seconds"),
        "watch": {"ok": watch_ok, "pid": hb.get("pid"), "age": round(hb_age) if hb_age else None,
                  "dirs": hb.get("dirs", [])},
        "mail": {"ok": mail_ok, "age": round(mail_age) if mail_age else None,
                 "scanned": mhb.get("scanned"), "processed": mhb.get("processed")},
        "today": len(entries),
        "last": {"name": last.get("name"), "ts": last.get("ts"), "job_id": last.get("job_id"),
                 "app_id": last.get("app_id")},
        "recent": [{"name": e.get("name"), "ts": e.get("ts"), "app": bool(e.get("app_id"))}
                   for e in entries[-6:]][::-1],
        "unassigned": len(unassigned),
        "review": len(review),
        "aliases": len(aliases),
        "failed": failed,
        "llm": bool(llm.get("enabled")),
        "import_service": launchd_state("com.example.resume-import"),
        "mail_service": launchd_state("com.example.resume-mail"),
    }


def render_html(s: dict) -> str:
    ok = s["watch"]["ok"]
    color = "#2f8f4e" if ok else "#c0392b"
    status_text = "运行中" if ok else "⚠ 监听服务可能已停止"
    watch_age = f'{s["watch"]["age"]} 秒前' if s["watch"]["age"] is not None else "无心跳"
    mail_age = f'{s["mail"]["age"]} 秒前' if s["mail"]["age"] is not None else "无记录"
    recent = "".join(
        f"<tr><td>{html.escape(str(r['ts'][11:16]))}</td><td>{html.escape(str(r['name'] or '-'))}</td>"
        f"<td>{'已投递' if r['app'] else '仅入库'}</td></tr>" for r in s["recent"]) or \
        "<tr><td colspan=3>今天还没有入库记录</td></tr>"

    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>简历入库 · 运行状态</title>
<style>
body{{font-family:-apple-system,PingFang SC,sans-serif;margin:0;padding:32px;background:#f6f7f9;color:#222}}
.card{{background:#fff;border-radius:12px;padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:16px}}
.dot{{display:inline-block;width:14px;height:14px;border-radius:50%;background:{color};margin-right:8px;vertical-align:middle}}
h1{{font-size:20px;margin:0 0 6px}} .muted{{color:#888;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:12px}}
.kpi{{background:#fafbfc;border-radius:8px;padding:12px 14px}}
.kpi b{{display:block;font-size:22px;margin-bottom:2px}}
table{{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee}}
th{{color:#888;font-weight:500}}
a{{color:#0b7bd4;text-decoration:none}}
</style>
<div class="card">
  <h1><span class="dot"></span>{status_text}</h1>
  <div class="muted">刷新于 {s['now']} · 每 5 秒自动刷新</div>
  <div class="grid">
    <div class="kpi"><b>{s['today']}</b>今日入库</div>
    <div class="kpi"><b>{s['unassigned']}</b>待分配</div>
    <div class="kpi"><b>{s['aliases']}</b>岗位映射</div>
    <div class="kpi"><b>{s['failed']}</b>失败文件</div>
  </div>
</div>
<div class="card">
  <div><b>监听服务</b> · 心跳 {watch_age} · PID {s['watch']['pid'] or '-'} · launchd: {s['import_service']}</div>
  <div style="margin-top:6px"><b>邮箱扫描</b> · 上次运行 {mail_age} · launchd: {s['mail_service']} · 扫描 {s['mail']['scanned'] or 0} 封</div>
  <div style="margin-top:6px"><b>LLM 选岗</b> · {'已启用' if s['llm'] else '未启用（规则匹配）'}</div>
</div>
<div class="card">
  <b>最近入库</b>
  <table><thead><tr><th>时间</th><th>姓名</th><th>状态</th></tr></thead><tbody>{recent}</tbody></table>
  <div class="muted" style="margin-top:10px">完整记录：<a href="file://{PROJECT_DIR}/data/reports/imports_今天.html" target="_blank">打开报告页</a></div>
</div>
<meta http-equiv="refresh" content="5">
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send_response(403); self.end_headers(); return
        if self.path not in ("/", "/api/status") and not self.path.startswith("/api/status?"):
            self.send_response(404); self.end_headers(); return
        if self.path.startswith("/api/status"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            supplied = q.get("token", [""])[0]
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Bearer "):
                supplied = auth[7:]
            if not TOKEN or supplied != TOKEN:
                self.send_response(401); self.end_headers(); return
            body = json.dumps(collect(), ensure_ascii=False).encode()
            ctype = "application/json; charset=utf-8"
        else:
            body = render_html(collect()).encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
