#!/bin/bash
# 一键体检：系统更新 / 重启后跑一遍，确认所有组件正常
# digest / profile / static 从必检降为可选（仓内无安装脚本，不凭空实现）
set -u
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PROJECT_DIR
PY="$PROJECT_DIR/.venv/bin/python"
FAIL=0
ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*"; FAIL=$((FAIL+1)); }
warn() { echo "  · $*（可选，未安装不算失败）"; }

echo "== 1. 基础环境 =="
command -v lark-cli >/dev/null 2>&1 && ok "lark-cli: $(lark-cli --version 2>/dev/null)" || bad "lark-cli 未安装（npm i -g @larksuite/cli）"
lark-cli auth status >/dev/null 2>&1 && ok "lark-cli 已登录" || bad "lark-cli 未登录（lark-cli auth login）"
[ -x "$PY" ] && ok "Python 环境: $("$PY" -V 2>&1)" || bad "Python 虚拟环境缺失（重跑 scripts/bootstrap.sh）"

echo "== 2. 飞书权限 =="
if "$PY" "$PROJECT_DIR/scripts/hire_import.py" --check >/dev/null 2>&1; then
  ok "招聘/邮件接口权限齐全"
else
  bad "权限不足（运行 --check 查看详情；申请说明可填：招聘提效工具-简历自动入库）"
fi

echo "== 3. 后台服务（必检） =="
for label in resume-import resume-mail resume-status resume-menubar; do
  line=$(launchctl list 2>/dev/null | grep "com.example.$label" || true)
  if [ -n "$line" ]; then
    code=$(echo "$line" | awk '{print $2}')
    pid=$(echo "$line" | awk '{print $1}')
    if [ "$label" = "resume-import" ] || [ "$label" = "resume-status" ] || [ "$label" = "resume-menubar" ]; then
      [ "$pid" != "-" ] && ok "$label 运行中 (pid $pid)" || bad "$label 未运行"
    else
      ok "$label 已加载 (上次退出码 $code)"
    fi
  else
    bad "$label 未安装"
  fi
done

echo "== 3b. 可选服务（不阻断） =="
for label in resume-digest resume-profile resume-static; do
  line=$(launchctl list 2>/dev/null | grep "com.example.$label" || true)
  if [ -n "$line" ]; then
    ok "$label 已加载（可选）"
  else
    warn "$label 未安装"
  fi
done

echo "== 4. 状态面板 =="
TOKEN=""
if [ -f "$PROJECT_DIR/data/status_token" ]; then
  TOKEN=$(cat "$PROJECT_DIR/data/status_token")
fi
if curl -fsS --max-time 3 ${TOKEN:+-H "Authorization: Bearer $TOKEN"} http://127.0.0.1:8787/api/status >/dev/null 2>&1; then
  ok "状态面板可访问: http://127.0.0.1:8787"
else
  bad "状态面板未响应（launchctl kickstart gui/$(id -u)/com.example.resume-status）"
fi
pgrep -f "ResumeStatus" >/dev/null 2>&1 && ok "桌面悬浮窗运行中" || bad "桌面悬浮窗未运行"

echo "== 5. 数据状态 =="
# 路径经环境变量传入，避免含空格目录被截断（C7）
TODAY=$(date +%Y-%m-%d)
export TODAY
CNT=$("$PY" - <<'PY' 2>/dev/null
import json, os, pathlib
root = pathlib.Path(os.environ["PROJECT_DIR"]) / "data" / "hire_import_log.jsonl"
today = os.environ["TODAY"]
n = 0
if root.exists():
    for line in root.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            if e.get("ts", "").startswith(today) and not any(
                m in str(e.get("file") or "")
                for m in ("测试", "演示", "压测", "内存测试", "验收", "续跑测试")
            ):
                n += 1
        except Exception:
            pass
print(n)
PY
)
ok "今日已入库: ${CNT:-0} 份"
"$PY" - <<'PY' 2>/dev/null
import json, os, pathlib
data = pathlib.Path(os.environ["PROJECT_DIR"]) / "data"
for f, label in (("unassigned.json", "待分配"), ("corrections.json", "校准示例")):
    p = data / f
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        print(f"  · {label}: {len(d.get('items') or d.get('examples') or [])}")
PY

echo
if [ "$FAIL" -eq 0 ]; then
  echo "✅ 体检通过，可以正常使用"
else
  echo "⚠️ 有 $FAIL 项异常，按上面提示修复（多数情况重跑 scripts/setup_colleague.sh 即可）"
fi
exit "$FAIL"
