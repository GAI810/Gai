#!/bin/bash
# 构建「离线版客户端」：安装包内置在 App 里，不需要服务器
# 用法: bash scripts/build_client_offline.sh [版本号]
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="${1:?用法: build_client_offline.sh v1.0.4（版本号必填）}"
OUT="$PROJECT_DIR/dist"

bash "$PROJECT_DIR/scripts/build_release.sh" "$VER" "offline"

APP="$OUT/简历一键入库.app"
rm -rf "$APP"
cp -R "$PROJECT_DIR/client/ResumeHunter.app" "$APP"
mkdir -p "$APP/Contents/Resources/offline"
cp "$PROJECT_DIR/dist/www/latest.json" "$APP/Contents/Resources/offline/"
PKG=$(PROJECT_DIR="$PROJECT_DIR" /usr/bin/python3 - <<'PY'
import json, os, pathlib
p = pathlib.Path(os.environ["PROJECT_DIR"]) / "dist" / "www" / "latest.json"
print(json.loads(p.read_text(encoding="utf-8"))["url"].rsplit("/", 1)[-1])
PY
)
cp "$PROJECT_DIR/dist/www/$PKG" "$APP/Contents/Resources/offline/"
# 额外把 ResumeStatus.app 放进 Resources，安装时也可从这里取
if [ -d "$PROJECT_DIR/dist/ResumeStatus.app" ]; then
  cp -R "$PROJECT_DIR/dist/ResumeStatus.app" "$APP/Contents/Resources/"
fi
printf 'offline' > "$APP/Contents/Resources/base_url.txt"
chmod +x "$APP/Contents/MacOS/launcher"
xattr -cr "$APP" 2>/dev/null || true

rm -f "$OUT/简历一键入库-离线版.zip"
(cd "$OUT" && zip -rq "简历一键入库-离线版.zip" "简历一键入库.app")
echo "✓ 离线版已生成"
echo "  $OUT/简历一键入库.app"
echo "  $OUT/简历一键入库-离线版.zip"
ls -lh "$OUT/简历一键入库-离线版.zip" | awk '{print "  大小: "$5}'
