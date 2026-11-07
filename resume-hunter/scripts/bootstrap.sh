#!/bin/bash
# 一键环境引导：确保 Python ≥3.10 + 虚拟环境 + 依赖，然后交给 setup_colleague.sh
#
# 用法（同事机器上）：
#   bash scripts/bootstrap.sh
#
# 适合让 AI 按步骤执行：每一步都有明确的成功/失败输出。

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1
VENV="$PROJECT_DIR/.venv"

echo "== 1/4 检查 Python =="
PYBIN=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1; then
        v=$("$c" -c 'import sys; print("%d%02d" % sys.version_info[:2])' 2>/dev/null || echo 0)
        if [ "$v" -ge 310 ] 2>/dev/null; then
            PYBIN=$(command -v "$c")
            echo "✓ 使用 $c ($($c --version 2>&1)) → $PYBIN"
            break
        fi
    fi
done

if [ -z "$PYBIN" ]; then
    echo "未找到 Python ≥3.10，尝试用 uv 安装…"
    if command -v uv >/dev/null 2>&1; then
        uv python install 3.12 || { echo "✗ uv 安装 Python 失败"; exit 1; }
        PYBIN=$(uv python find 3.12 2>/dev/null)
        [ -n "$PYBIN" ] || { echo "✗ 找不到 uv 安装的 Python"; exit 1; }
        echo "✓ 使用 uv Python: $PYBIN"
    else
        echo "✗ 需要 Python ≥3.10 或 uv"
        echo "  可选：brew install python@3.12"
        echo "  或安装 uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
fi

echo
echo "== 2/4 创建虚拟环境 =="
if [ -x "$VENV/bin/python" ]; then
    echo "✓ 已存在: $VENV"
else
    "$PYBIN" -m venv "$VENV" || { echo "✗ venv 创建失败（可能需要先安装 python3-venv）"; exit 1; }
    echo "✓ 已创建: $VENV"
fi

echo
echo "== 3/4 安装依赖 =="
if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$VENV/bin/python" pdfplumber pyyaml python-docx >/dev/null 2>&1 || \
        "$VENV/bin/python" -m pip install -q pdfplumber pyyaml python-docx
else
    "$VENV/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1
    "$VENV/bin/python" -m pip install -q pdfplumber pyyaml python-docx || { echo "✗ 依赖安装失败"; exit 1; }
fi
"$VENV/bin/python" -c "import pdfplumber, yaml" || { echo "✗ 依赖校验失败"; exit 1; }
echo "✓ pdfplumber / pyyaml 就绪"

echo
echo "== 应用配置（如安装包内置）=="
if [ -f "$PROJECT_DIR/deploy/app_config.json" ] && command -v lark-cli >/dev/null 2>&1; then
    if ! lark-cli config show 2>/dev/null | grep -q "appId"; then
        export RESUME_APP_CONFIG="$PROJECT_DIR/deploy/app_config.json"
        APP_ID=$(/usr/bin/python3 -c 'import json,os;print(json.load(open(os.environ["RESUME_APP_CONFIG"]))["app_id"])')
        APP_SECRET=$(/usr/bin/python3 -c 'import json,os;print(json.load(open(os.environ["RESUME_APP_CONFIG"]))["app_secret"])')
        printf '%s' "$APP_SECRET" | lark-cli config init --app-id "$APP_ID" --app-secret-stdin >/dev/null 2>&1 \
            && echo "✓ 已自动配置 lark-cli 应用（${APP_ID}）" \
            || echo "⚠ 自动配置失败，请手动执行 lark-cli config init"
        unset APP_SECRET
    else
        echo "✓ lark-cli 已配置，跳过"
    fi
fi

if [ "${BOOTSTRAP_ONLY:-0}" = "1" ]; then
    echo "✓ 环境就绪（BOOTSTRAP_ONLY=1，跳过服务安装）"
    exit 0
fi
echo "== 4/4 交给安装脚本 =="
exec bash "$PROJECT_DIR/scripts/setup_colleague.sh"
