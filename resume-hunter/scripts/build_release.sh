#!/bin/bash
# 构建发布包：zip + latest.json（放到公司服务器的静态目录即可）
# 用法: bash scripts/build_release.sh v1.0.0 [https://your-server/resume-hunter]
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:?用法: build_release.sh v1.0.0 [base_url] [app_id:app_secret]}"
BASE_URL="${2:-http://YOUR-SERVER/resume-hunter}"
APP_CRED="${3:-}"
OUT="$PROJECT_DIR/dist/www"
mkdir -p "$OUT"
cd "$PROJECT_DIR"

# 若尚未构建悬浮窗，尝试构建（macOS）；失败则明确报错（C4）
if [ ! -d "$PROJECT_DIR/dist/ResumeStatus.app" ]; then
  if command -v swiftc >/dev/null 2>&1; then
    bash "$PROJECT_DIR/scripts/build_menubar.sh"
  else
    echo "✗ 缺少 dist/ResumeStatus.app，且本机无 swiftc 无法现场编译。"
    echo "  请先在 macOS 上运行 bash scripts/build_menubar.sh 再打包。"
    exit 1
  fi
fi
[ -f "$PROJECT_DIR/dist/ResumeStatus.app/Contents/MacOS/ResumeStatus" ] \
  || { echo "✗ ResumeStatus.app 不完整"; exit 1; }
[ -f "$PROJECT_DIR/dist/ResumeStatus.app/Contents/MacOS/MenuBar" ] \
  || echo "⚠ ResumeStatus.app 缺少 MenuBar 可执行文件（建议重跑 build_menubar.sh）"

ZIP="resume-hunter-$VERSION.zip"
rm -f "$OUT/$ZIP"
# 排除 client 源码，但把编译产物 ResumeStatus.app 打进包根目录 dist-app/
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
# 复制仓库（排除大目录）
rsync -a --exclude 'data' --exclude '.venv' --exclude 'archive' \
  --exclude 'client' --exclude 'dist' --exclude 'deploy' \
  --exclude '*.plist' --exclude '__pycache__' --exclude '*.log' \
  --exclude '.git' \
  "$PROJECT_DIR/" "$STAGE/" 2>/dev/null || {
  # fallback without rsync
  mkdir -p "$STAGE"
  cp -R "$PROJECT_DIR/scripts" "$STAGE/"
  cp "$PROJECT_DIR"/*.md "$STAGE/" 2>/dev/null || true
  cp "$PROJECT_DIR/pyproject.toml" "$STAGE/" 2>/dev/null || true
  cp "$PROJECT_DIR/.python-version" "$STAGE/" 2>/dev/null || true
  cp "$PROJECT_DIR/.gitignore" "$STAGE/" 2>/dev/null || true
}
mkdir -p "$STAGE/dist"
cp -R "$PROJECT_DIR/dist/ResumeStatus.app" "$STAGE/dist/"
(cd "$STAGE" && zip -rq "$OUT/$ZIP" .)
SHA=$(shasum -a 256 "$OUT/$ZIP" | awk '{print $1}')
cat > "$OUT/latest.json" <<JSON
{
  "version": "$VERSION",
  "url": "$BASE_URL/$ZIP",
  "notes": "简历一键入库 $VERSION",
  "sha256": "$SHA"
}
JSON
# 内置应用配置（客户端安装时自动写入 lark-cli 配置，同事无需手填）
if [ -n "$APP_CRED" ]; then
  APP_ID="${APP_CRED%%:*}"
  APP_SECRET="${APP_CRED#*:}"
  mkdir -p "$PROJECT_DIR/deploy"
  printf '{"app_id":"%s","app_secret":"%s"}\n' "$APP_ID" "$APP_SECRET" > "$PROJECT_DIR/deploy/app_config.json"
  echo "✓ 已内置应用配置（${APP_ID}）"
fi
echo "✓ 发布包已生成（含 dist/ResumeStatus.app）"
echo "  $OUT/$ZIP"
echo "  $OUT/latest.json"
ls -lh "$OUT" | tail -3
