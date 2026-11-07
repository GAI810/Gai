#!/bin/bash
# 构建客户端 App（把服务器地址写进 App）
# 用法: bash scripts/build_client.sh https://your-server/resume-hunter [版本号]
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${1:?用法: build_client.sh <base_url> [version]}"
VER="${2:-1.0.0}"
OUT="$PROJECT_DIR/dist"
rm -rf "$OUT/简历一键入库.app"
mkdir -p "$OUT"
cp -R "$PROJECT_DIR/client/ResumeHunter.app" "$OUT/简历一键入库.app"
LAUNCHER="$OUT/简历一键入库.app/Contents/MacOS/launcher"
mkdir -p "$OUT/简历一键入库.app/Contents/Resources"
printf '%s' "$BASE_URL" > "$OUT/简历一键入库.app/Contents/Resources/base_url.txt"
sed -i '' "s|<key>CFBundleVersion</key><string>[^<]*</string>|<key>CFBundleVersion</key><string>$VER</string>|" \
    "$OUT/简历一键入库.app/Contents/Info.plist"
sed -i '' "s|<key>CFBundleShortVersionString</key><string>[^<]*</string>|<key>CFBundleShortVersionString</key><string>$VER</string>|" \
    "$OUT/简历一键入库.app/Contents/Info.plist"
chmod +x "$LAUNCHER"
# 去掉隔离属性，避免 Gatekeeper 拦截（内部分发）
xattr -cr "$OUT/简历一键入库.app" 2>/dev/null || true
# 打包成 zip 便于分发
rm -f "$OUT/简历一键入库.zip"
(cd "$OUT" && zip -rq "简历一键入库.zip" "简历一键入库.app")
echo "✓ 客户端已构建"
echo "  $OUT/简历一键入库.app"
echo "  $OUT/简历一键入库.zip"
ls -lh "$OUT/简历一键入库.zip" | awk '{print "  大小: "$5}'
