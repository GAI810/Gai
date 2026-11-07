# 简历一键入库 · 安装与使用说明（供 AI 助手执行）

> **怎么用这份文件**：把本文件**整个**发给你的 AI 助手（Claude Code / Codex / 任意本地 Agent），并说一句：
> 「按这份文档帮我装好，然后教我使用。」
>
> AI 会按下面步骤执行、逐步验证，失败会自动排障；最后会把使用方法讲给你听。

---

## 一、给 AI 的硬性规则（必须遵守）

1. **按顺序执行**，每一步验证通过才进入下一步；不要跳过验证。
2. **不要向用户索取密码**；app_secret 只在用户主动提供时使用，**不要打印明文**、不要写进日志。
3. **不要删除** `data/` 目录（里面有入库记录、映射、学习数据）。
4. **不要手改** `data/hire_watch_state.json`。
5. 安装/修改完成后，**必须运行体检脚本**并把结果告诉用户。
6. 如果某步失败，**先查「九、排障」**再重试；连续两次失败就停下，把报错原文告诉用户。
7. 面向用户的说明用**中文**，命令用代码块给出。
8. 向用户说明两条入库通道：① 本地下载到监听目录；② Boss 绑定飞书邮箱后自动扫信。
   **明确告知：仅在 Boss 会话内预览附件不会入库**（不做 Boss hook）。
9. 权限申请说明预填：**招聘提效工具-简历自动入库**；`--check` 看四态。
10. 邮件扫描 `--limit` 代码默认 20（可用 `RESUME_MAIL_LIMIT` 调整）；不要把测试报告里的 60 当成代码默认。

---

## 二、步骤 0：环境预检

```bash
sw_vers -productVersion            # 需要 macOS 12+
uname -m                            # arm64 或 x86_64 都行

command -v node && node -v          # 需要 Node.js >= 18
command -v npm && npm -v
command -v python3 && python3 -V    # 需要 >= 3.10（没有也能装，客户端会用 uv）
command -v lark-cli && lark-cli --version
```

判定：

| 情况 | 动作 |
|---|---|
| 不是 macOS | 停止，告知用户"目前只支持 macOS" |
| 没有 node/npm | `brew install node`（没有 brew 就先装 Homebrew） |
| 没有 lark-cli | 见步骤 1 |
| 有 lark-cli 但未登录 | `lark-cli auth login`（见步骤 1） |
| 不能访问 open.feishu.cn | 停止，告知用户需要放通 |

---

## 三、步骤 1：安装并登录 lark-cli

```bash
npm i -g @larksuite/cli
lark-cli --version

# 登录（会打开浏览器，让用户完成授权）
lark-cli auth login

# 验证登录态
lark-cli auth status
```

期望：`auth status` 里 `identities.user.status` 为 `ready`，并有 `userName` / `openId`。

> ⚠️ 登录用的是**用户自己的飞书账号**，邮箱通道和"我负责的岗位"都依赖它。

---

## 四、步骤 2：配置飞书应用凭证

两种方式，**优先方式 A**。

### 方式 A：安装包已内置（推荐，用户零配置）

如果安装包里带了 `deploy/app_config.json`，安装时会自动写入 lark-cli 配置，跳过本步骤。验证：

```bash
lark-cli config show | grep appId
```

### 方式 B：手工配置（需要用户提供 app_id / app_secret）

```bash
printf '%s' "<APP_SECRET>" | lark-cli config init --app-id "<APP_ID>" --app-secret-stdin
lark-cli config show
```

> 没凭证就让用户找管理员要（应用需已开通招聘相关权限）。

---

## 五、步骤 3：安装工具本体

### 方式 A（推荐）：双击客户端 App

1. 解压 `简历一键入库-离线版.zip`（或联网版）
2. **右键 → 打开**「简历一键入库.app」（首次会被 Gatekeeper 拦，右键打开即可）
3. 等待 3–5 分钟（自动建环境、装依赖、装后台服务）

安装目录：

```
~/Library/Application Support/ResumeHunter/app      # 程序
~/Library/Application Support/ResumeHunter/logs     # 安装日志
```

验证：

```bash
APP="$HOME/Library/Application Support/ResumeHunter/app"
cat "$APP/VERSION"
tail -20 "$HOME/Library/Application Support/ResumeHunter/logs/launcher.log"
```

### 方式 B：命令行安装（没有 App 时）

```bash
mkdir -p ~/简历一键入库 && cd ~/简历一键入库
unzip -q <发布包路径>/resume-hunter-*.zip -d .
bash scripts/bootstrap.sh
```

之后项目目录就是当前目录。下文用 `PROJ` 表示：

```bash
PROJ="$HOME/Library/Application Support/ResumeHunter/app"   # App 安装
# 或
PROJ="$HOME/简历一键入库"                                   # 命令行安装
```

---

## 六、步骤 4：体检（必做）

```bash
cd "$PROJ" && bash scripts/healthcheck.sh
```

期望末尾：`✅ 体检通过，可以正常使用`

| 检查项 | 通过标准 |
|---|---|
| lark-cli | 已安装且已登录 |
| Python 环境 | `.venv/bin/python` 存在 |
| 飞书权限 | `--check` 返回「权限齐全」（招聘+邮件，四态均为已生效） |
| 后台服务 | resume-import 运行中；mail/digest/profile 已加载 |
| 状态面板 | `http://127.0.0.1:8787` 可访问，右下角悬浮窗显示"运行中" |
| 数据 | 能读今日入库数 |

有 ✗：先 `bash scripts/setup_colleague.sh` 重新生成服务配置，再体检一次。

---

## 七、步骤 5：初始化配置（按需，约 10 分钟）

### 5.1 设置重点岗位（可选但推荐）

```bash
cd "$PROJ"
.venv/bin/python scripts/hire_import.py --init-jobs
```

### 5.2 建立岗位映射（强烈推荐）

Boss 岗位名和飞书职位名经常对不上，映射能显著提升准确率：

```bash
.venv/bin/python scripts/hire_import.py --auto-map      # 自动推荐（同名自动应用）
.venv/bin/python scripts/hire_import.py --show-alias    # 查看映射 + 待映射
.venv/bin/python scripts/hire_import.py --map-job "Boss岗位名" "飞书职位关键词"
```

### 5.3 配置 LLM 语义选岗（可选）

不开也能用（关键词 + 映射）。要开：

```bash
.venv/bin/python scripts/hire_import.py --setup-llm
.venv/bin/python scripts/hire_import.py --llm-status    # 验证
```

### 5.4 确认邮箱通道

- Boss 后台 → 设置 → 确认「简历发送到邮箱」已开启；
- 收件邮箱 = 用户自己的飞书邮箱。

```bash
.venv/bin/python scripts/mail_import.py --dry-run   # 只看会处理哪些，不写入
```

---

## 八、步骤 6：教用户使用（AI 必须复述以下内容）

### 每天

> **什么都不用做。**
> 在 Boss 上正常沟通、收简历，工具会自动把简历入库到飞书招聘，并自己判断投哪个岗位。
> 成功时：Chrome 后台多一个人才页标签 + 一声提示音（不抢当前窗口）。

### 每周（约 5 分钟）

> 1. 打开报告页：双击「查看入库记录.command」，或执行
>    `.venv/bin/python scripts/hire_import.py --report`
> 2. 看「**待分配**」区块：工具拿不准、只入库没投递的候选人
> 3. 在飞书招聘里把他们投到正确岗位
> 4. 每周一 09:30 自动学习；也可手动 `.venv/bin/python scripts/hire_import.py --learn`

### 常用命令

```bash
cd "$PROJ"

.venv/bin/python scripts/hire_import.py --report          # 报告页
.venv/bin/python scripts/hire_import.py --show-alias      # 岗位映射
.venv/bin/python scripts/hire_import.py --show-my-jobs    # 我负责的岗位
.venv/bin/python scripts/hire_import.py --show-unassigned # 待分配
.venv/bin/python scripts/hire_import.py --evaluate        # 匹配有效性
.venv/bin/python scripts/hire_import.py --explain "<简历>" # 单份判断过程
.venv/bin/python scripts/hire_import.py --check           # 权限检查
bash scripts/healthcheck.sh                                # 体检
```

### 出问题

| 现象 | 用户该做什么 |
|---|---|
| 下载简历没反应 | 让 AI 跑 `healthcheck.sh` + 看 `data/logs/hire_watch.log` |
| 邮箱没收到简历 | 检查 Boss「简历发送到邮箱」；`mail_import.py --dry-run` |
| 岗位投错 | 告诉 AI 正确对应关系，让 AI 执行 `--map-job` |
| 待分配太多 | 检查重点岗位 / `--show-my-jobs` 权限 |

---


## 八补、完整权限清单（一轮申请）

申请说明填：`招聘提效工具-简历自动入库`

| 权限 | 级别 | 用途 |
|---|---|---|
| `hire:talent` / `hire:attachment` / `hire:job:readonly` / `hire:application` | 应用 | 人才/附件/职位/投递 |
| `mail:user_mailbox.message:readonly` | 用户 | 邮件列表 |
| `mail:user_mailbox.message` | 用户 | 详情（发件人、主题） |
| `mail:user_mailbox.message.attachment:readonly` | 用户 | 附件下载 |

四态：待申请 → 待管理员审批 → 待用户授权（`lark-cli auth login`）→ 已生效。

两条通道：本地下载监听；Boss 绑定飞书邮箱。仅会话内预览不入库。邮件默认扫描最近 20 封（`--limit` / `RESUME_MAIL_LIMIT`）。

## 九、排障（给 AI）

| 报错 | 原因 | 修法 |
|---|---|---|
| `lark-cli 未安装` | 没装 | `npm i -g @larksuite/cli` |
| `lark-cli 未登录` | 没登录/过期 | `lark-cli auth login` |
| `权限不足` / `app_scope_not_applied` | 缺 scope | 开放平台补权限（`--check` 给链接） |
| `resume-import 未运行` | 服务没起来 | `launchctl kickstart -k gui/$(id -u)/com.example.resume-import` |
| 服务里找不到 lark-cli | Node 升级导致路径变化 | 重跑 `bash scripts/setup_colleague.sh` |
| 安装失败 | 看 `~/Library/Application Support/ResumeHunter/logs/install.log` | 按日志修，再双击 App |
| 文件解析失败 | 坏 PDF | 自动进 `data/failed/`，不影响其他文件 |
| 邮件处理报错 | 附件/权限 | 看 `data/logs/mail_import.log` |

重启服务：

```bash
for s in import mail; do launchctl kickstart -k "gui/$(id -u)/com.example.resume-$s"; done
```

---

## 十、更新与卸载

### 更新

- **联网版**：双击 App 自动检测升级（校验哈希，失败保留旧版）；
- **离线版**：重新发新包，双击覆盖安装（数据保留）。

### 卸载

```bash
for s in import mail digest profile static; do
  launchctl bootout "gui/$(id -u)/com.example.resume-$s" 2>/dev/null
done
rm -f ~/Library/LaunchAgents/com.example.resume-*.plist
rm -rf "$HOME/Library/Application Support/ResumeHunter"   # 确认后再执行
```

---

## 十一、安全与隐私（要向用户说明）

- 简历只上传到**你们公司的飞书招聘**，不经过第三方；
- 开启 LLM 时，发给模型的是**脱敏文本**（去掉手机号、邮箱、身份证、链接）；
- 日志与报告页中的联系方式已打码；数据目录权限 700；
- 工具不会覆盖飞书里已有人才的姓名；
- 所有投递都能在飞书里撤销/调整。
