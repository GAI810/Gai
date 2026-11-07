#!/bin/bash
# 每周维护：刷新「我负责的岗位」缓存 + 学习人工纠正
set -u
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$PROJECT_DIR/.venv/bin/python"
LOG="$PROJECT_DIR/data/logs/profile.log"
mkdir -p "$(dirname "$LOG")"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 开始 ====="
  "$PY" "$PROJECT_DIR/scripts/hire_import.py" --show-my-jobs
  "$PY" "$PROJECT_DIR/scripts/hire_import.py" --auto-map
  "$PY" "$PROJECT_DIR/scripts/hire_import.py" --learn
  "$PY" "$PROJECT_DIR/scripts/hire_import.py" --evaluate
  bash "$PROJECT_DIR/scripts/smoke_test.sh"
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 结束 ====="
} >> "$LOG" 2>&1
