#!/bin/bash
# 部署发布包到公司服务器（静态托管即可，不需要任何服务端程序）
# 用法: bash scripts/deploy_release.sh <user@host> <远程目录> [端口]
# 示例: bash scripts/deploy_release.sh deploy@10.0.0.5 /var/www/resume-hunter
set -e
TARGET="${1:?用法: deploy_release.sh <user@host> <远程目录> [端口]}"
REMOTE_DIR="${2:?缺远程目录，如 /var/www/resume-hunter}"
PORT="${3:-22}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$PROJECT_DIR/dist/www"

[ -f "$SRC/latest.json" ] || { echo "✗ 请先运行 build_release.sh"; exit 1; }

echo "→ 上传到 $TARGET:$REMOTE_DIR ..."
ssh -p "$PORT" "$TARGET" "mkdir -p '$REMOTE_DIR'" 
rsync -avz -e "ssh -p $PORT" "$SRC/" "$TARGET:$REMOTE_DIR/"
echo "✓ 已上传。请确认服务器已用 nginx/静态服务把该目录暴露为:"
echo "    <你的地址>/resume-hunter/latest.json"
