#!/bin/bash
# 配置 LLM 选岗（默认 DeepSeek 官方 API，OpenAI 兼容）
# 写入 data/llm_config.json（600 权限），不写 launchd 环境变量（launchctl print 会泄露）
# 用法: bash scripts/set_llm_key.sh <api_key> [base_url] [model]
set -e
KEY="${1:?用法: set_llm_key.sh <api_key> [base_url] [model]}"
BASE="${2:-https://api.deepseek.com/v1}"
MODEL="${3:-deepseek-chat}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

/usr/bin/python3 - "$PROJECT_DIR/data/llm_config.json" "$KEY" "$BASE" "$MODEL" <<'PY'
import json, os, sys
path, key, base, model = sys.argv[1:5]
cfg = {"enabled": True, "base_url": base, "model": model, "api_key": key}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=1)
os.chmod(path, 0o600)
print(f"✓ 已写入 {path}（600 权限）")
PY

launchctl kickstart -k "gui/$(id -u)/com.example.resume-import" 2>/dev/null || true
echo "✓ 服务已重启，LLM 配置生效"
