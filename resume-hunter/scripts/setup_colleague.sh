#!/bin/bash
# 飞书招聘「简历一键入库」安装脚本（macOS）
#
# 用法：
#   bash scripts/setup_colleague.sh
#
# 做的事：
#   1. 检查依赖（lark-cli、venv、飞书招聘+邮件权限）
#   2. 创建监听目录 ~/Downloads/招聘简历/待整理下载
#   3. 写入租户人才详情域名（data/config.json）
#   4. 生成双击启动器 + 入库记录查看入口
#   5. 安装 launchd：resume-import / resume-mail / resume-status / resume-menubar
#   6. 安装结束跑 healthcheck
#
# 适合分发给同事：所有路径基于 $HOME，不写死具体用户目录。

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$PROJECT_DIR/.venv/bin/python"
WATCH_DIR="$HOME/Downloads/招聘简历/待整理下载"
LAUNCH_DIR="$HOME/Downloads/招聘简历"
AGENTS_DIR="$HOME/Library/LaunchAgents"
SUPPORT_DIR="$HOME/Library/Application Support/ResumeHunter"
STATUS_APP_SRC=""
STATUS_APP_DST="$SUPPORT_DIR/ResumeStatus.app"

echo "== 飞书招聘一键入库 · 安装 =="
echo "项目目录: $PROJECT_DIR"

# 1. 依赖检查
if [ ! -x "$PY" ]; then
    echo "未找到 Python 环境，先运行环境引导（自动装 Python/依赖）…"
    exec bash "$PROJECT_DIR/scripts/bootstrap.sh"
fi
if ! command -v lark-cli >/dev/null 2>&1; then
    echo "✗ 未找到 lark-cli"
    echo "  请先安装（联系管理员获取安装方式），并执行: lark-cli auth login"
    echo "  安装后重新运行: bash scripts/bootstrap.sh"
    exit 1
fi

# 2. 权限检查（招聘 + 邮件，四态）
echo
echo "-- 检查飞书招聘 / 邮件接口权限 --"
if ! "$PY" "$PROJECT_DIR/scripts/hire_import.py" --check; then
    echo
    echo "✗ 权限未就绪，请按上面的提示到开发者后台开通后重新运行本脚本。"
    echo "  申请说明可填：招聘提效工具-简历自动入库"
    exit 1
fi

# 3. 监听目录
mkdir -p "$WATCH_DIR"
mkdir -p "$PROJECT_DIR/data/logs"
mkdir -p "$AGENTS_DIR"
mkdir -p "$SUPPORT_DIR"

# 3.5 租户人才详情域名（C6）
CONFIG_JSON="$PROJECT_DIR/data/config.json"
export CFG="$CONFIG_JSON"
echo
echo "== 配置人才详情页租户域名 =="
EXISTING_TENANT=""
if [ -f "$CONFIG_JSON" ]; then
    EXISTING_TENANT=$("$PY" -c 'import json,os,pathlib; p=pathlib.Path(os.environ["CFG"]);
d=json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
print(d.get("tenant_domain") or "")' 2>/dev/null) || EXISTING_TENANT=""
fi
DEFAULT_TENANT="${EXISTING_TENANT:-}"
if [ -z "$DEFAULT_TENANT" ]; then
    # 尝试从 lark-cli 已有配置猜测（失败则空）
    DEFAULT_TENANT=$("$PY" - <<'PY' 2>/dev/null || true
import json, os, pathlib, re, subprocess
# 不写死密钥；只读本机已有配置提示用户
print("")
PY
)
fi
if [ -z "$DEFAULT_TENANT" ]; then
    DEFAULT_TENANT="example.feishu.cn"
    echo "⚠ 未检测到本租户域名。公开快照的兜底 $DEFAULT_TENANT 不是真实租户，请改成自己的 xxx.feishu.cn"
fi
if [ -t 0 ]; then
    read -r -p "租户域名（如 company.feishu.cn）[$DEFAULT_TENANT]: " TENANT_IN
    TENANT_DOMAIN="${TENANT_IN:-$DEFAULT_TENANT}"
else
    TENANT_DOMAIN="${RESUME_TENANT_DOMAIN:-$DEFAULT_TENANT}"
    echo "非交互安装，使用租户域名: ${TENANT_DOMAIN}（可用 RESUME_TENANT_DOMAIN 覆盖）"
fi
TENANT_DOMAIN="${TENANT_DOMAIN#https://}"
TENANT_DOMAIN="${TENANT_DOMAIN#http://}"
TENANT_DOMAIN="${TENANT_DOMAIN%%/*}"
export TENANT_DOMAIN
"$PY" - <<'PY'
import json, os, pathlib
p = pathlib.Path(os.environ["CFG"])
p.parent.mkdir(parents=True, exist_ok=True)
d = {}
if p.exists():
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        d = {}
tenant = os.environ["TENANT_DOMAIN"].strip()
d["tenant_domain"] = tenant
d["talent_url"] = f"https://{tenant}/hire/talent/{{talent_id}}"
p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(p, 0o600)
print(f"✓ 已写入 {p}: talent_url={d['talent_url']}")
PY

# 4. 双击启动器（路径一律加引号，兼容 Application Support 空格）
cat > "$LAUNCH_DIR/一键入库服务.command" <<EOF
#!/bin/bash
cd "$PROJECT_DIR" || exit 1
echo "简历一键入库服务已启动，关闭此窗口即停止。"
exec "$PY" "$PROJECT_DIR/scripts/hire_import.py" --watch "$WATCH_DIR,$HOME/Downloads" --open --open-mode background
EOF
chmod +x "$LAUNCH_DIR/一键入库服务.command"

cat > "$LAUNCH_DIR/查看入库记录.command" <<EOF
#!/bin/bash
cd "$PROJECT_DIR" || exit 1
"$PY" "$PROJECT_DIR/scripts/hire_import.py" --report "\$@"
EOF
chmod +x "$LAUNCH_DIR/查看入库记录.command"

# 找 ResumeStatus.app（发布产物或已安装副本）
for cand in \
    "$PROJECT_DIR/dist/ResumeStatus.app" \
    "$PROJECT_DIR/client/dist/ResumeStatus.app" \
    "$SUPPORT_DIR/ResumeStatus.app" \
    "$PROJECT_DIR/../ResumeStatus.app"; do
    if [ -d "$cand" ] && { [ -x "$cand/Contents/MacOS/ResumeStatus" ] || [ -f "$cand/Contents/MacOS/ResumeStatus" ]; }; then
        STATUS_APP_SRC="$cand"
        break
    fi
done

install_plist() {
    local label="$1"
    local plist="$AGENTS_DIR/$label.plist"
    local body="$2"
    printf '%s\n' "$body" > "$plist"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    if launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null; then
        echo "✓ 已启动 $label"
    else
        # bootstrap 在已加载时可能失败，尝试 kickstart
        if launchctl kickstart -k "gui/$(id -u)/$label" 2>/dev/null; then
            echo "✓ 已拉起 $label"
        else
            echo "⚠ $label 加载失败（可稍后手动 launchctl bootstrap）"
        fi
    fi
}

# 5. launchd 后台服务
if [ "${RESUME_NO_SERVICE:-0}" = "1" ]; then
    echo "（RESUME_NO_SERVICE=1，跳过 launchd 安装）"
else
    LARK_BIN_DIR="$(dirname "$(command -v lark-cli)")"
    PATH_VAL="$LARK_BIN_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    # 5a resume-import（本地监听）
    install_plist "com.example.resume-import" "$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.example.resume-import</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$PROJECT_DIR/scripts/hire_import.py</string>
        <string>--watch</string>
        <string>$WATCH_DIR,$HOME/Downloads</string>
        <string>--open</string>
        <string>--open-mode</string>
        <string>background</string>
        <string>--llm-match</string>
    </array>
    <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$PROJECT_DIR/data/logs/hire_watch.log</string>
    <key>StandardErrorPath</key><string>$PROJECT_DIR/data/logs/hire_watch.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$PATH_VAL</string>
    </dict>
</dict>
</plist>
EOF
)"

    # 5b resume-mail（每 5 分钟；--limit 默认 20，可用环境变量覆盖）
    MAIL_LIMIT="${RESUME_MAIL_LIMIT:-20}"
    install_plist "com.example.resume-mail" "$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.example.resume-mail</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$PROJECT_DIR/scripts/mail_import.py</string>
        <string>--limit</string>
        <string>$MAIL_LIMIT</string>
    </array>
    <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>StartInterval</key><integer>300</integer>
    <key>StandardOutPath</key><string>$PROJECT_DIR/data/logs/mail_import.log</string>
    <key>StandardErrorPath</key><string>$PROJECT_DIR/data/logs/mail_import.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$PATH_VAL</string>
    </dict>
</dict>
</plist>
EOF
)"

    # 5c resume-status（:8787）
    install_plist "com.example.resume-status" "$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.example.resume-status</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$PROJECT_DIR/scripts/status_server.py</string>
    </array>
    <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$PROJECT_DIR/data/logs/status_server.log</string>
    <key>StandardErrorPath</key><string>$PROJECT_DIR/data/logs/status_server.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$PATH_VAL</string>
        <key>STATUS_PORT</key>
        <string>8787</string>
    </dict>
</dict>
</plist>
EOF
)"

    # 5d 悬浮窗 ResumeStatus / menubar
    if [ -z "$STATUS_APP_SRC" ]; then
        echo "✗ 未找到 ResumeStatus.app（应在 dist/ResumeStatus.app）。"
        echo "  请先在 macOS 上运行: bash scripts/build_menubar.sh"
        echo "  安装包必须包含编译产物；缺失则终止安装（不等到体检）。"
        exit 1
    fi
    rm -rf "$STATUS_APP_DST"
    mkdir -p "$SUPPORT_DIR"
    cp -R "$STATUS_APP_SRC" "$STATUS_APP_DST"
    xattr -cr "$STATUS_APP_DST" 2>/dev/null || true
    STATUS_BIN="$STATUS_APP_DST/Contents/MacOS/ResumeStatus"
    if [ ! -e "$STATUS_BIN" ]; then
        echo "✗ ResumeStatus.app 不完整：$STATUS_BIN 不存在"
        exit 1
    fi
    install_plist "com.example.resume-menubar" "$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.example.resume-menubar</string>
    <key>ProgramArguments</key>
    <array>
        <string>$STATUS_BIN</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$PROJECT_DIR/data/logs/menubar.log</string>
    <key>StandardErrorPath</key><string>$PROJECT_DIR/data/logs/menubar.log</string>
</dict>
</plist>
EOF
)"
fi

echo
# 6. 引导设置重点岗位（可跳过）
echo "== 设置重点岗位（用于按简历内容自动匹配投递岗位）=="
if [ -t 0 ]; then
    read -r -p "现在设置重点岗位吗？[Y/n] " ans
else
    ans="n"
    echo "非交互安装，跳过重点岗位（稍后可 --init-jobs）"
fi
if [ "${ans:-Y}" != "n" ] && [ "${ans:-Y}" != "N" ]; then
    "$PY" "$PROJECT_DIR/scripts/hire_import.py" --init-jobs
else
    echo "已跳过，稍后可运行: $PY scripts/hire_import.py --init-jobs"
fi

echo
# 7. 可选：LLM 语义选岗
echo "== 可选：LLM 语义选岗（用大模型判断该投哪个岗位）=="
echo "   不开启也能用：走关键词匹配 + 岗位映射，零成本"
if [ -t 0 ]; then
    read -r -p "现在配置 LLM 选岗吗？[y/N] " ans2
else
    ans2="N"
fi
if [ "${ans2:-N}" = "y" ] || [ "${ans2:-N}" = "Y" ]; then
    "$PY" "$PROJECT_DIR/scripts/hire_import.py" --setup-llm
else
    echo "已跳过（之后可运行: $PY scripts/hire_import.py --setup-llm）"
fi

echo
echo "== 安装完成 =="
echo "监听目录 : $WATCH_DIR"
echo "邮箱扫描 : 每 5 分钟（--limit 默认 20，可用 RESUME_MAIL_LIMIT 调整）"
echo "状态面板 : http://127.0.0.1:8787"
echo "日常用法 :"
echo "  · 本地通道：在 Boss/猎聘点「下载简历」保存到监听目录 → 自动入库"
echo "  · 邮箱通道：Boss 绑定飞书邮箱后，候选人简历进邮箱 → 自动入库"
echo "  · 仅在 Boss 会话内预览附件、未下载也未转邮箱 → 不会入库"
echo "查看记录 : 双击「$LAUNCH_DIR/查看入库记录.command」"
echo "设置岗位 : $PY scripts/hire_import.py --set-job \"岗位关键词\""

# 8. 安装结束体检（C3）
if [ "${RESUME_NO_SERVICE:-0}" != "1" ] && [ "${RESUME_SKIP_HEALTHCHECK:-0}" != "1" ]; then
    echo
    echo "== 安装结束体检 =="
    bash "$PROJECT_DIR/scripts/healthcheck.sh" || {
        echo "⚠ 体检未全部通过，请按上方提示排查后重跑 healthcheck。"
        exit 1
    }
fi
