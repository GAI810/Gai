#!/bin/bash
# 构建悬浮窗 / 菜单栏状态 App（读取 http://127.0.0.1:8787/api/status）
# HUD.swift → 主可执行文件 ResumeStatus（悬浮窗）
# MenuBar.swift → 同包内次要可执行文件 MenuBar（菜单栏图标；二者各自有入口，不能链成单 binary）
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$PROJECT_DIR/dist/ResumeStatus.app"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "✗ 本机无 swiftc，无法编译 ResumeStatus.app（请在 macOS 上执行本脚本）"
  exit 1
fi

swiftc -O "$PROJECT_DIR/client/ResumeStatus/HUD.swift" \
  -o "$OUT/Contents/MacOS/ResumeStatus" -framework AppKit
swiftc -O "$PROJECT_DIR/client/ResumeStatus/MenuBar.swift" \
  -o "$OUT/Contents/MacOS/MenuBar" -framework AppKit

cat > "$OUT/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>com.example.resumestatus</string>
    <key>CFBundleName</key><string>简历入库</string>
    <key>CFBundleExecutable</key><string>ResumeStatus</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSUIElement</key><true/>
    <key>NSPanel</key><true/>
</dict>
</plist>
PLIST
xattr -cr "$OUT" 2>/dev/null || true
echo "✓ 菜单栏/悬浮窗 App: $OUT"
echo "  - Contents/MacOS/ResumeStatus  (HUD.swift)"
echo "  - Contents/MacOS/MenuBar       (MenuBar.swift)"
