---
name: resume-hunter
description: >
  招聘简历自动化工具。主线功能是「简历一键入库飞书招聘人才库」：把 Boss直聘/猎聘等平台
  下载的简历文件自动上传到飞书招聘人才库，可选创建投递、并在浏览器打开人才详情页。
  当用户提到"简历入库"、"新简历入库"、"导入飞书招聘"、"一键入库"、"简历获取"、
  "搜索候选人"、"打招呼"、"巡检回复"时使用。
  Boss/脉脉 的搜索、打招呼、巡检为二期能力，按需手动运行。
author: user
category: recruitment
tags: [recruitment, feishu-hire, resume, boss-zhipin, maimai]
compatibility: [python3, playwright]
---

# Resume Hunter

## 项目定位（重要）

**V1 主线：简历一键入库。**

把「在招聘平台挑简历 → 打开飞书招聘 → 新简历入库 → 上传 → 填来源 → 提交」压缩成一条命令。

- 输入：Boss直聘、猎聘、前程无忧、智联、拉勾、领英等平台下载的简历文件（PDF/DOC/DOCX）
- 动作：解析姓名/手机/邮箱 → 上传附件 → 创建人才 →（可选）创建投递 →（可选）浏览器打开人才页
- 不依赖平台登录态：简历文件到手即可入库，无风控风险

**二期（暂缓，按需手动）**：Boss/脉脉 的批量搜索、打招呼、巡检回复。
这些能力依赖浏览器登录态、有风控风险，不默认启用、不装定时任务。

## 环境

```bash
cd ~/.claude/skills/resume-hunter
.venv/bin/python scripts/hire_import.py --check   # 检查飞书权限（招聘+邮件，四态）
```

说明：本机 `uv run` 受全局 uv 配置问题影响，统一用项目 venv 的 python。

## 主线：飞书招聘一键入库

### 三种模式

```bash
# 单文件（Boss/猎聘等下载的简历），入库后打开人才页
.venv/bin/python scripts/hire_import.py --file "~/Downloads/xxx.pdf" --open

# 监听文件夹：新简历出现即自动入库（默认 ~/Downloads/招聘简历/待整理下载）
.venv/bin/python scripts/hire_import.py --watch --open

# 岗位映射（Boss/猎聘岗位名 → 飞书职位，解决名称对不上）
.venv/bin/python scripts/hire_import.py --show-alias                 # 映射表 + 待映射清单
.venv/bin/python scripts/hire_import.py --map-job "社区产品经理（示例业务）" "社区产品经理"
.venv/bin/python scripts/hire_import.py --suggest-alias "ai产品经理"  # LLM 建议（需 LLM_API_KEY）

# 重点岗位（安装后引导设置）：入库时按简历内容自动匹配岗位并建投递
.venv/bin/python scripts/hire_import.py --init-jobs          # 交互式多选
.venv/bin/python scripts/hire_import.py --focus-jobs "品类运营（拼豆）,产品设计工程师"
.venv/bin/python scripts/hire_import.py --show-focus

# 岗位绑定：设置当前在招岗位，监听时自动建投递
.venv/bin/python scripts/hire_import.py --set-job "品类运营（拼豆）"
.venv/bin/python scripts/hire_import.py --show-job
.venv/bin/python scripts/hire_import.py --clear-job

# 目录批量导入（先 --dry-run 预览，确认后去掉）
.venv/bin/python scripts/hire_import.py --dir "~/Downloads/招聘简历/待整理下载" --dry-run
.venv/bin/python scripts/hire_import.py --dir "~/Downloads/招聘简历/待整理下载"

# 每日摘要（默认统计昨天；--days 3 看最近三天）
.venv/bin/python scripts/hire_import.py --digest
.venv/bin/python scripts/hire_import.py --digest --days 3

# 查看入库记录（HTML 报告，含人才链接）
.venv/bin/python scripts/hire_import.py --report          # 今天
.venv/bin/python scripts/hire_import.py --report --days 7 # 最近 7 天

# 批量：处理数据库里已分析、未入库的候选人
.venv/bin/python scripts/hire_import.py --list
.venv/bin/python scripts/hire_import.py --dry-run --min-score 7
.venv/bin/python scripts/hire_import.py --yes --min-score 8 --limit 5
```

### 来源与岗位

- 来源默认 BOSS直聘（ID 18）；猎聘 4、前程无忧 3、智联 2、领英 6、拉勾 5
  - 用 `--source-name 猎聘` / `--source-id` 指定，`--no-source` 不填
- 选岗位建投递：`--job "关键词"`（唯一命中自动选中）、`--job-id <id>`、`--pick-job`（交互选择）
- 人才详情页 URL 优先读 `data/config.json` 的 `talent_url` / `tenant_domain`，
  可用 `--talent-url` 或环境变量 `HIRE_TALENT_URL` 覆盖；example.feishu.cn 仅作未配置时的占位兜底

### 依赖的飞书权限（一次列全）

- 应用级：`hire:talent`、`hire:attachment`、`hire:job:readonly`、`hire:application`
- 用户级（邮箱通道）：`mail:user_mailbox.message:readonly`、
  `mail:user_mailbox.message`（详情含发件人/主题）、
  `mail:user_mailbox.message.attachment:readonly`
- 申请说明预填：`招聘提效工具-简历自动入库`
- `--check` 按四态显示：待申请 / 待管理员审批 / 待用户授权 / 已生效

### 两条入库通道

- 本地：下载到监听目录才会入库
- 邮箱：Boss 绑定飞书邮箱后由 `resume-mail` 扫描（默认 `--limit 20`，可调）
- **仅在 Boss 会话内预览附件 → 不会入库**（不做客户端 hook）

### 实现要点

- API 链路：`POST /open-apis/hire/v1/attachments`（上传）→ `POST /open-apis/hire/v1/talents/combined_create`（建人才）→ `POST /open-apis/hire/v1/applications`（建投递）
- PDF 解析（pdfplumber）提取姓名/手机/邮箱；能还原脱敏名（张女士→张三）、双印水印（李李四四→李四）、中英混合名（李四Alex Lee→李四）
- 飞书按手机号/邮箱自动去重：重复导入返回已有 talent_id，不重复建人
- 监听模式导入后把文件移到「已入库」子目录；批量模式写入 `hire_talent_id` / `hire_imported_at`

## 二期能力（按需手动，不默认启用）

> **风控硬规则（2026-11 起）**
> - 二期参数须用户确认；Agent **首次只能** `--scan-only` / `--dry-run`
> - `daily_limit` / `daily_hard_cap` 默认 ≤50；CLI `--limit` 与硬顶取 min；突破建议需 `--i-know-the-risk`
> - Boss 非 scan-only：必须 `--tokens N(N>0)` 或成功解析余额；`--tokens 0` 退出；不明余额拒绝
> - 脉脉默认 `enabled: false` + `verified: false`，未验证禁止外发
> - search/check 共用日配额 outbound + 平台文件锁；check 默认 `--platform boss`、`--limit 20`
> - 裸 `--no-limit` 已移除（需确认 + 硬顶）；无头禁止非 scan-only 外发

### 登录平台（只有用浏览器自动化时才需要）

```bash
.venv/bin/python scripts/login.py --platform boss
.venv/bin/python scripts/login.py --platform maimai
```

### 搜索并打招呼（有风控风险，小批量使用）

```bash
# Agent / 首次：只扫描
.venv/bin/python scripts/search.py --platform boss --keywords "产品经理,B端" --job "产品经理" --limit 20 --scan-only

# 用户确认后小批量外发（须 tokens）
.venv/bin/python scripts/search.py --platform boss --keywords "产品经理,B端" --job "产品经理" --limit 20 --tokens 20 --threshold 6
```

### 巡检回复 + 下载简历（手动跑一次，不要定时）

```bash
# 先 dry-run
.venv/bin/python scripts/check.py --platform boss --days 7 --dry-run --limit 20
# 确认后再外发
.venv/bin/python scripts/check.py --platform boss --days 7 --limit 20
```

### 简历分析（可选）

```bash
.venv/bin/python scripts/analyze.py
```

## 配置与数据

- 配置：`data/config.yaml`（平台、行为节奏、每日上限、JD）
- 数据库：`data/db.sqlite`（候选人、简历路径、入库标记）
- 简历：`data/resumes/<日期>/<平台>/*.pdf`
- 归档：`archive/`（已停用的旧脚本，仅留追溯）

## 简历解析能力

- 姓名：文件名优先（Boss「【岗位】姓名 经验.pdf」）→ 数据库姓名 → PDF 页眉/联系方式邻域
  （处理脱敏名、双印水印、康熙部首字符 NFKC 归一化）
- 手机/邮箱：只信任完整、合理的匹配（水印拆行的邮箱宁可不填）
- 教育/工作经历：保守抽取（学校+学历+时间、公司+职位+时间），抽不准就不填
- 平台来源：文件名关键词（脉脉/猎聘/前程/智联/领英/拉勾），默认 BOSS直聘

## 岗位匹配规则（优先级从高到低）

1. 命令行显式指定（`--job` / `--job-id`）；
2. **岗位映射**：从简历文件名提取 Boss/猎聘 岗位名（【岗位_城市 薪资】），查 `job_alias.json`；
   命中就直接投到对应飞书职位（解决两边岗位名对不上的问题）；
3. 候选池 = 「我负责的岗位」（职位负责人匹配）→ 重点岗位 → 历史常投岗位；
   加 `--llm-match` 且配置了 LLM key（默认 DeepSeek 官方 API）时，LLM 在候选池里选，
   prompt 含岗位 JD、候选人结构化摘要、历史偏好；返回置信度，
   低于 `LLM_MIN_CONF`（默认 0.65）只入库不建投递。无 key 自动回退关键词匹配。
   **keyword / active 兜底同样受该门槛约束**（active 置信度 0.5，默认会被拦下进待分配）。
4. 「当前岗位」（active_job.json）仅在没有配置重点岗位时兜底；
5. 有重点岗位但没匹配上：**不建投递**，只提示（避免投错）。

岗位统计（`job_interest.json`）会记录每个 Boss 岗位名出现的次数，报告页展示「岗位分布」和「待映射」清单。

## 分发给同事（安装）

所有路径基于 `$HOME`，不写死用户目录。同事拿到项目目录后执行：

```bash
bash scripts/setup_colleague.sh
```

脚本会：检查 lark-cli / venv / 招聘+邮件权限（四态）→ 写租户域名 → 建监听目录
→ 生成双击启动器 → 安装 launchd（import/mail/status/menubar）→ 结束跑 healthcheck。

分发包注意：**不要打包 `data/` 和 `archive/`**（前者含候选人隐私、登录态、日志；后者是停用的旧脚本）。
同事装好后 `data/` 会重新生成。LLM 相关功能需要 `LLM_API_KEY`（可选 `LLM_BASE_URL`、`LLM_MODEL`）。

前提（同事机器上）：
- 项目目录（含 `.venv`）与 `lark-cli`，且 `lark-cli` 已完成 `auth login`
- `lark-cli` 绑定的是你自己的应用（公开快照不写真实 `cli_<APP_ID>`），权限是应用级、全租户共用
- 如租户域名不同：设 `HIRE_TALENT_URL=https://<租户>.feishu.cn/hire/talent/{talent_id}`

## 注意

- 入库链路不依赖浏览器，是最稳的主线
- 浏览器自动化依赖登录态，Boss 会话会过期；巡检/打招呼失败先检查登录
- 风控敏感操作（批量打招呼、巡检）先小批量试跑，观察平台反馈

## 重复导入的行为

- 先按手机号/邮箱查已有人才；命中则不新建，改为**给老记录追加新简历**（更新人才接口）
- 同一份简历（文件名相同）已挂载过的，跳过追加，避免附件重复堆积
- 追加后若配置了映射/重点岗位，仍会尝试建投递；重复投递会自动跳过

## 邮箱通道（零操作）

Boss 会把简历自动发到飞书邮箱。`scripts/mail_import.py` 每 5 分钟扫描收件箱：
匹配发件域名 → 下载 PDF/DOCX 附件 → 复用主流程入库 → 落岗（映射/LLM）→ 建投递。

```bash
.venv/bin/python scripts/mail_import.py --dry-run   # 预览
launchctl kickstart -k gui/$(id -u)/com.example.resume-mail   # 立即跑一次
```

## 统一决策与待分配

所有入口共用 `decide_job()`：显式指定 → 映射 → 投递岗位直配 → LLM（≥0.65）→ 关键词 → 兜底。
不确定的进「待分配」（`data/unassigned.json`，报告页可见）；HR 补投后 `--learn` 会存成校准示例。
