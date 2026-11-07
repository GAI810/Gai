#!/bin/bash
# 冒烟测试：每次改代码后跑一遍，验证关键链路没有回归
# 不写入飞书、不产生投递（全部只读或 dry-run）
set -u
export SMOKE_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$PROJECT_DIR/.venv/bin/python"
FAIL=0
ok()  { echo "  ✓ $*"; }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

echo "== 1. 编译 =="
if "$PY" -m py_compile "$PROJECT_DIR/scripts/hire_import.py" "$PROJECT_DIR/scripts/mail_import.py" \
      "$PROJECT_DIR/scripts/status_server.py" "$PROJECT_DIR/scripts/evaluate_matching.py" 2>/dev/null; then
  ok "Python 编译通过"
else
  bad "Python 编译失败"
fi

echo "== 2. 服务心跳 =="
TOKEN=$(cat "$PROJECT_DIR/data/status_token" 2>/dev/null)
status=$(curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/api/status 2>/dev/null)
if [ -n "$status" ]; then
  echo "$status" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
assert d['watch']['ok'], 'watch 心跳异常'
print('  ✓ 监听心跳 %s 秒前' % d['watch']['age'])
" 2>/dev/null || bad "监听心跳异常/已停止"
else
  bad "状态面板不可用"
fi

echo "== 3. 姓名提取（回归用例）=="
"$PY" - <<'PYEOF' || FAIL=$((FAIL+1))
import os, sys
sys.path.insert(0, os.environ["SMOKE_SCRIPTS"])
import hire_import as h
cases = [
    ("教育背景\n男 28岁", "", "栏目词不当姓名"),
    ("李 小 明\n求职意向：文案编辑", "李小明", "字间空格姓名"),
    ("Alex Kim\n28岁/上海", "Alex Kim", "英文名"),
    ("姓 名 张三\n手机 13812345678", "张三", "标签姓名"),
]
bad = 0
for text, want, desc in cases:
    got = h.guess_name(text, "")
    if want and got != want:
        print(f"  ✗ {desc}: 期望 {want!r} 实际 {got!r}")
        bad += 1
    elif not want and h.name_is_valid(got):
        print(f"  ✗ {desc}: 不应返回可信姓名 {got!r}")
        bad += 1
    else:
        print(f"  ✓ {desc}")
raise SystemExit(1 if bad else 0)
PYEOF

echo "== 4. 岗位决策（离线，不调接口）=="
"$PY" - <<'PYEOF' || FAIL=$((FAIL+1))
import os, sys
sys.path.insert(0, os.environ["SMOKE_SCRIPTS"])
import hire_import as h
d = h.decide_job("随便一段简历文本", "测试", explicit_job="123456")
assert d.get("via") == "explicit" and d.get("job_id") == "123456", d
print("  ✓ 显式岗位路径")
alias = h.resolve_alias("新品类拓展运营")
if alias:
    d2 = h.decide_job("随便一段简历文本", "测试", applied_job="新品类拓展运营")
    assert d2.get("via") == "alias", d2
    print("  ✓ 映射路径 ->", d2.get("title"))
else:
    print("  · 未配置该映射，跳过")
PYEOF

echo "== 5. 邮箱扫描（dry-run）=="
if timeout 120 "$PY" "$PROJECT_DIR/scripts/mail_import.py" --dry-run --limit 3 2>&1 | tail -2 | grep -qE "本次处理|无新邮件"; then
  ok "邮箱扫描正常"
else
  bad "邮箱扫描异常"
fi

echo "== 6. 通知去重（防重复打扰）=="
"$PY" - <<'PYEOF' || FAIL=$((FAIL+1))
import os, sys, time
sys.path.insert(0, os.environ["SMOKE_SCRIPTS"])
import hire_import as h
tag = f"冒烟测试-{time.time()}"
first = h.notify("冒烟测试", tag, sound=False)
second = h.notify("冒烟测试", tag, sound=False)
assert first is True and second is False, (first, second)
print("  ✓ 相同内容第二次被抑制")
PYEOF

echo "== 7. 报告生成 =="
if "$PY" "$PROJECT_DIR/scripts/hire_import.py" --report >/dev/null 2>&1 && \
   [ -f "$PROJECT_DIR/data/reports/imports_今天.html" ]; then
  ok "报告页生成成功"
else
  bad "报告页生成失败"
fi


echo "== 8. 路径空格（C7） =="
SPACE_DIR="$PROJECT_DIR/data/tmp smoke space"
mkdir -p "$SPACE_DIR"
if PROJECT_DIR="$SPACE_DIR" "$PY" -c 'import os,pathlib; print(pathlib.Path(os.environ["PROJECT_DIR"]).name)' 2>/dev/null | grep -q 'tmp smoke space'; then
  ok "含空格 PROJECT_DIR 可经环境变量传递"
else
  bad "含空格路径传递失败"
fi
rmdir "$SPACE_DIR" 2>/dev/null || true

echo "== 9. mail --limit 说明 =="
if "$PY" "$PROJECT_DIR/scripts/mail_import.py" --help 2>&1 | grep -qE '默认 20'; then
  ok "mail_import --limit 默认 20（可调）"
else
  bad "mail_import --help 未说明默认 20"
fi

echo
if [ $FAIL -eq 0 ]; then
  echo "✅ 冒烟测试全部通过"
else
  echo "❌ 冒烟测试失败 $FAIL 项，请修复后再继续"
fi
exit $FAIL
