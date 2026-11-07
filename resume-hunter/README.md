# 简历一键入库（resume-hunter）

> 作品集里的产品叙事叫「[天下无鸽入库助手](../README.md#天下无鸽入库助手)」。这是脱敏后的公开快照：没有本地 `data/`，没有真实租户，没有密钥。配置模板只有 [`config.example.yaml`](config.example.yaml)。密钥、租户域名和候选人数据只放在本机，不要提交。

> 把招聘平台来的简历，自动变成飞书招聘里的「人才 + 投递」，HR 不用再手点。

---

## 一、它解决什么问题

**以前**：下载简历 → 打开飞书招聘 → 点「新简历入库」→ 上传 → 选岗位 → 建投递（每份 1–2 分钟，还容易漏）。

**现在**：

- Boss 上的简历会**自动发到你的飞书邮箱**，工具每 5 分钟取一次，自动入库；
- 手动下载到指定文件夹的简历，也会被自动捡走；
- 工具自己判断该投哪个岗位（映射优先，LLM 兜底），不确定的**不硬投**，进入「待分配」清单；
- 入库后**后台打开人才页**（不抢焦点）+ 一声提示音；
- 你每周花 5 分钟看待分配、补投递，工具会自动学习你的选择。

---

## 二、两条入口

### 入口 1：邮箱（主通道，零操作）

前置：在 Boss **绑定飞书邮箱**作为简历接收邮箱。

```
候选人在 Boss 发简历
   → BOSS 自动发邮件到你的飞书邮箱
   → 工具每 5 分钟扫描收件箱（默认最近 20 封，--limit / RESUME_MAIL_LIMIT 可调）
   → 下载 PDF/DOC/DOCX 附件
   → 解析 → 入库（已有则合并）→ 选岗 → 建投递（不确定则待分配，不硬投）
```

### 入口 2：本地文件夹监听（兜底通道）

```
手动下载简历到 ~/Downloads/招聘简历/待整理下载（或 ~/Downloads）
   → 3 秒内检测到 → 同上流程 → 处理完移入「已入库」
```

> `~/Downloads` 根目录只处理文件名像简历的文件，避免误导入其他 PDF。
>
> **仅在 Boss 会话内预览附件、未下载也未转邮箱 → 不会入库**（不做 Boss 客户端 hook）。

---

## 三、岗位决策流程（核心）

```
候选池
 ├─ ① 映射表：Boss 岗位名 → 飞书职位（人工确认，最准）
 ├─ ② 投递岗位直配：名称词块高度重合（≥3 块且唯一最优）
 ├─ ③ LLM：给模型看候选人摘要 + 候选岗位 JD，返回岗位 + 置信度
 ├─ ④ 关键词匹配（LLM 不可用时）
 └─ ⑤ 兜底当前岗位（仅在没有重点岗位时）
        ↓
置信度 ≥ 0.65 → 建投递
置信度 < 0.65 或模型说"没有合适岗位" → 只入库，进「待分配」
```

**发送给 LLM 的内容**：候选人结构化摘要 + 最近经历 + 岗位 JD + 历史校准示例（已去掉手机/邮箱/身份证/链接）。

**候选池来源**（按优先级）：

1. 你负责的岗位（按职位负责人/协助人/用人经理匹配）
2. 重点岗位（`--init-jobs` 配置）
3. 历史常投岗位（自动学习）

---

## 四、数据与文件

| 路径 | 内容 |
|---|---|
| `data/hire_import_log.jsonl` | 审计日志（谁、什么时间、进了哪个岗位、application_id） |
| `data/hire_watch_state.json` | 监听状态机（imported → matched → applied → archived / failed） |
| `data/job_alias.json` | 平台岗位名 → 飞书职位映射 |
| `data/map_suggestions.json` | 待人工确认的映射建议 |
| `data/unassigned.json` | 待分配清单（入库但没建投递） |
| `data/corrections.json` | 校准示例（喂给 LLM 的 few-shot） |
| `data/llm_config.json` | LLM 配置（key 在本机，600 权限） |
| `data/mail_state.json` | 邮箱已处理记录（附件级） |
| `data/jobs_cache.json` | 在招岗位缓存（1 小时） |
| `data/reports/imports_今天.html` | 报告页 |
| `data/failed/` | 解析失败的文件（隔离，不再重试） |
| `data/logs/` | 运行日志（脱敏） |

---

## 五、命令速查

```bash
cd ~/.claude/skills/resume-hunter
PY=.venv/bin/python

# —— 日常 ——
$PY scripts/hire_import.py --report            # 生成并打开报告页
$PY scripts/hire_import.py --show-unassigned   # 看待分配（也可在报告页看）
$PY scripts/mail_import.py --dry-run           # 预览邮箱会处理哪些简历

# —— 配置 ——
$PY scripts/hire_import.py --init-jobs         # 设置重点岗位
$PY scripts/hire_import.py --map-job "Boss岗位名" "飞书职位关键词"   # 建立映射
$PY scripts/hire_import.py --auto-map          # LLM 推荐映射（同名自动，其余待确认）
$PY scripts/hire_import.py --show-alias        # 查看映射 + 待映射清单
$PY scripts/hire_import.py --show-my-jobs      # 查看"我负责的岗位"
$PY scripts/hire_import.py --setup-llm         # 配置 LLM（可选）
$PY scripts/hire_import.py --llm-status        # 查看 LLM 配置
$PY scripts/hire_import.py --llm-off           # 关闭 LLM（回到纯规则）

# —— 学习与评估 ——
$PY scripts/hire_import.py --learn             # 学习人工纠正（每周自动跑）
$PY scripts/hire_import.py --evaluate          # 更新"匹配有效性"三档统计
$PY scripts/hire_import.py --explain "<简历>"   # 看某份简历的完整判断过程

# —— 排障 ——
$PY scripts/hire_import.py --check             # 检查飞书权限
tail -50 data/logs/hire_watch.log              # 监听日志
tail -20 data/logs/mail_import.log             # 邮箱日志
launchctl kickstart -k gui/$(id -u)/com.example.resume-import   # 重启监听
```

---

## 六、怎么确认它在跑？

| 方式 | 看什么 |
|---|---|
| **状态面板**（推荐） | 浏览器打开 `http://127.0.0.1:8787`：绿点=运行中、心跳秒数、今日入库数、待分配、最近记录，每 5 秒自动刷新 |
| **桌面悬浮窗** | 右下角小卡片：🟢运行中 / 今日入库数 / 心跳秒数 / 最近一条记录；可拖动、位置会记住，点一下打开面板 |
| **提示音 + 后台标签** | 每次成功入库都会响一声并在 Chrome 后台开人才页 |
| **体检** | `bash scripts/healthcheck.sh` 一键全查 |

> 服务清单：`resume-import`（监听）、`resume-mail`（邮箱）、`resume-status`（面板）、`resume-menubar`（Dock 角标）、`resume-digest`（每日摘要）、`resume-profile`（每周维护）。

## 七、日常怎么用

| 频率 | 你要做的 |
|---|---|
| **每天** | 什么都不用做。Boss 上正常沟通，简历自动进库 |
| **每周一** | 自动跑：刷新"我负责的岗位" + 自动映射 + 学习纠正 + 更新评估 |
| **每周（5 分钟）** | 打开报告页 → 看「待分配」→ 在飞书里补投递 |
| **不定期** | 发现映射不对 → `--map-job` 改一下；发现某类简历总投错 → 改完等下周自动学习 |

---

## 七、安装与分发

### 同事（每人一次）

1. 拿到 `简历一键入库.zip` → 解压 → 右键「打开」（首次需要，未签名）
2. 自动安装环境、配置 lark-cli、装后台服务
3. 之后：什么都不用做

### 维护者（发版）

```bash
# 1. 打发布包（版本号自增；第三个参数可内置应用凭证）
bash scripts/build_release.sh v1.0.4 "https://<你的域名>/resume-hunter"

# 2. 上传静态文件（latest.json + zip）
bash scripts/deploy_release.sh <user@host> /var/www/resume-hunter

# 3. 重建客户端（把服务器地址写进 App）
bash scripts/build_client.sh "https://<你的域名>/resume-hunter" v1.0.4
```

同事下次双击 App 会自动检测更新（校验 sha256，失败不影响旧版本）。

---

## 八、注意事项

### 1. 权限（一次列全；申请说明填「招聘提效工具-简历自动入库」）

| 权限 | 级别 | 用途 |
|---|---|---|
| `hire:talent` | 应用 | 建人才、追加简历 |
| `hire:attachment` | 应用 | 上传简历附件 |
| `hire:application` | 应用 | 建投递 |
| `hire:job:readonly` | 应用 | 读职位列表 |
| `mail:user_mailbox.message:readonly` | 用户 | 邮件列表 |
| `mail:user_mailbox.message` | 用户 | 邮件详情（发件人、主题） |
| `mail:user_mailbox.message.attachment:readonly` | 用户 | 附件下载 |
| `hire:job.composite_info:readonly`（可选） | 应用 | 读职位负责人（"我负责的岗位"） |
| `hire:people_cli`（可选） | 应用 | 人事 CLI |

`--check` 按四态显示：待申请 / 待管理员审批 / 待用户授权 / 已生效。

### 2. LLM（可选）

- 默认**不开**也能用（关键词 + 映射）；
- 开启后每份简历约 1 次调用（DeepSeek 成本极低）；
- API key 存本机 `data/llm_config.json`（600 权限），不进安装包、不上传；
- 发给模型的文本已脱敏（无手机/邮箱/身份证/链接）。

### 3. 姓名与作品集

- **中文名优先**：PDF 里能识别到中文姓名就优先用；英文名只在没有中文名时启用（Alex Kim / Sam）。
- **图片为主 → 作品集**：文件名含「作品集/作品/portfolio」，或整页几乎没有文字且含图片，会自动归入 `作品集` 目录，**不做人才入库、不建投递**。

### 4. 准确率取决于映射表

- 平台岗位名（Boss）与飞书职位名经常不一致，**映射是最准的手段**；
- 同名岗位（社招/校招/实习）要分别映射；
- `--auto-map` 只自动应用名称完全一致的，其余进「待确认映射」。

### 4. 重复投递

- 同一人才重复导入会**合并**（按手机号/邮箱识别），不会建重复人才；
- 同一岗位下已有活跃投递会跳过，不重复建。

### 5. 失败文件

- 解析失败的文件移到 `data/failed/`，不会反复重试，也不影响后续文件；
- 修好后可以手动移回监听目录（会重新处理）。

### 6. 隐私

- `data/` 700、日志 600；
- 日志与报告页里的手机号/邮箱已打码；
- 完整数据只在 `data/hire_import_log.jsonl`（本机 600）。

### 7. 不要做的事

- 不要 `--watch --dry-run`（已禁止，监听模式会真实写入）；
- 不要手动改 `hire_watch_state.json`（会破坏断点续跑）；
- 不要多个进程同时跑 `--learn`（有锁但会互相等待）。

---

## 八·五、环境变量与开关

| 变量 | 作用 |
|---|---|
| `LLM_DISABLED=1` | 完全关闭 LLM 调用（回退关键词匹配） |
| `RESUME_LOG_MASK=1` | 审计日志里的手机号/邮箱打码 |
| `RESUME_BOT_ALLOW=ou_xxx,ou_yyy` | 机器人只接受白名单用户发的简历 |
| `RESUME_MAX_FILES_PER_PROCESS` / `RESUME_MAX_RSS_MB` | 内存回收阈值（默认 25 份 / 300MB） |
| `RESUME_TABS_MAX` | 保留最近几个人才页标签（默认 6） |

本地文件：

| 文件 | 说明 |
|---|---|
| `data/config.json` | 租户配置（tenant_domain / app_id / talent_url / console_url） |
| `data/status_token` | 状态面板 `/api/status` 的鉴权 token（0600，HUD 自动读取） |
| `data/needs_review.json` | 待确认队列 |
| `data/import_index.json` | 文件指纹 + 姓名索引 |

## 九、故障排查

| 现象 | 排查 |
|---|---|
| 下载了没反应 | `tail data/logs/hire_watch.log`；看是否有 `✗ 处理失败`；`launchctl kickstart` 重启 |
| 邮件没进来 | 确认 Boss 邮箱设置里开启了"简历发到邮箱"；`--dry-run` 看能扫到几封 |
| 岗位投错 | `--show-alias` 看映射；`--explain` 看判断过程；改完 `--map-job` |
| 报告页没数据 | 先 `--report`；数据来自 `hire_import_log.jsonl` |
| 待分配太多 | 检查重点岗位是否配置、`--show-my-jobs` 是否有权限 |
| 服务反复重启 | 看 `data/logs/hire_watch.log` 最后几行；坏文件会进 `data/failed/` |

---

## 十、实现方式（给维护者）

| 模块 | 职责 |
|---|---|
| `scripts/hire_import.py` | 主程序：解析、入库、决策、报告、学习、状态机 |
| `scripts/mail_import.py` | 邮箱通道：扫邮件 → 下载附件 → 复用主程序 |
| `scripts/evaluate_matching.py` | 对照实验：预测 vs 实际投递 |
| `scripts/lib/state_store.py` | 原子写 + 文件锁 + 损坏备份 |
| `scripts/lib/resume_parser.py` | PDF/Word 文本抽取 |
| `scripts/bootstrap.sh` / `setup_colleague.sh` | 安装 |
| `scripts/refresh_profile.sh` | 每周维护（映射/学习/评估） |
| `client/ResumeHunter.app` | 客户端安装器（校验哈希、原子安装） |
| `scripts/build_release.sh` / `deploy_release.sh` | 发版与部署 |

**关键设计**：

- **统一决策函数** `decide_job()`：所有入口共用，保证结果一致；
- **状态机**：`imported → matched → applied → archived`，崩溃可续跑；
- **原子写 + flock**：多进程并发不丢数据；
- **去重**：人才按手机/邮箱，附件按文件名，学习按事件键；
- **可观测**：每一步都有日志，报告页有"匹配有效性"三档统计。

> 公开脱敏快照。已去掉本地 `data/`、`.venv`、密钥和真实租户。作品集叙事见仓库根目录「[天下无鸽入库助手](../README.md#天下无鸽入库助手)」。这里不写效果数字。
