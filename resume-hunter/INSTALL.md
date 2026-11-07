# 简历一键入库 · 安装与使用说明

把「在 Boss / 猎聘下载简历 → 打开飞书招聘 → 新简历入库 → 上传 → 填来源 → 提交」这一串动作，
压缩成 **只点一次「下载」**。

> 你在招聘网站点「附件简历 → 下载」，3 秒内脚本自动完成：解析简历 → 上传附件 → 创建人才
> →（可选）匹配岗位建投递 → 在浏览器后台打开这条人才页。全程不碰你的鼠标键盘。

---

## 一、适用条件

| 条件 | 说明 |
|---|---|
| 系统 | macOS |
| 飞书招聘 + 邮件 | 管理员一次性开通「招聘 4 项 + 邮件 3 项」（见下文完整清单） |
| 本机工具 | 已安装 `lark-cli` 并完成 `auth login`（邮件通道依赖用户授权） |
| 项目目录 | 包含完整代码与 `.venv` 的目录（可装在含空格路径，如 Application Support） |

> 招聘权限是**应用级**（开通一次全公司共用）；邮件权限是**用户级**（每位同事需 `lark-cli auth login`）。
> `--check` 按四态显示：待申请 / 待管理员审批 / 待用户授权 / 已生效。
> 申请说明可直接填：**招聘提效工具-简历自动入库**。

---

## 二、安装（3 步）

### 1. 进入项目目录

```bash
cd <你的项目目录>/resume-hunter
```

### 2. 运行安装脚本

```bash
bash scripts/setup_colleague.sh
```

脚本会自动完成：

- 检查 `lark-cli` / Python 环境
- 检查飞书招聘 + 邮件权限（四态；缺了会打印完整清单与申请说明）
- 写入本租户人才详情域名（`data/config.json`）
- 创建监听目录 `~/Downloads/招聘简历/待整理下载`
- 生成两个双击入口（见下）
- 安装 launchd：`resume-import` / `resume-mail`（默认每 5 分钟、`--limit 20`）/ `resume-status` / `resume-menubar`
- 安装结束自动跑 `healthcheck.sh`（digest/profile/static 为可选，不阻断）

### 3. 设置你的重点岗位

脚本最后会问你：

```
== 设置重点岗位（用于按简历内容自动匹配投递岗位）==
现在设置重点岗位吗？[Y/n]
```

选 `Y`，按提示输入编号（可多选，逗号分隔）。这些岗位用于**入库时自动匹配投递**。
之后随时可改：

```bash
.venv/bin/python scripts/hire_import.py --init-jobs
```

---

## 三、日常使用（两条入库通道）

### 通道 A：本地监听（下载到目录）

在 Boss / 猎聘点「附件简历」→「下载」，保存到 `~/Downloads/招聘简历/待整理下载`
或系统下载目录（文件名带 `【岗位】` 的都能识别）。约 3 秒内自动入库。

### 通道 B：邮箱（Boss 绑定飞书邮箱）

1. 在 Boss 把简历接收邮箱绑成你的**飞书邮箱**；
2. 候选人投递后，Boss 会把带附件的邮件发到该邮箱；
3. 本机 `resume-mail` 默认每 5 分钟扫描最近 **20** 封（可用环境变量 `RESUME_MAIL_LIMIT` 或 `--limit` 调大；

> 说明：`--limit` / `RESUME_MAIL_LIMIT` 是扫描目标封数（默认 20）；邮件列表接口可能按分页多取，`--dry-run` 摘要里的「列表/拉取」是实际条数，可与 limit 不一致。
   测试报告里的「最近 60 封」多半是本机改过的值，代码默认仍是 20）。

### 不会入库的情况（务必看清）

> **仅在 Boss 会话内预览附件、既没有点下载、也没有转到飞书邮箱 → 不会入库。**
> 本工具不做 Boss 客户端 hook。

**剩下全自动**：入库 + 后台打开人才页（可点「查看入库记录」）。

### 两个双击入口

| 文件 | 作用 |
|---|---|
| `~/Downloads/招聘简历/一键入库服务.command` | 手动启动监听（后台服务异常时用） |
| `~/Downloads/招聘简历/查看入库记录.command` | 打开入库记录页（可点击跳转每个人的人才页） |

---

## 四、常用命令速查

```bash
cd <项目目录>   # 下文统一用 .venv/bin/python 调用

# 权限自检
.venv/bin/python scripts/hire_import.py --check

# 单文件导入（临时手动导一份）
.venv/bin/python scripts/hire_import.py --file "~/Downloads/xxx.pdf" --open

# 目录批量导入（先干跑预览，确认后去掉 --dry-run）
.venv/bin/python scripts/hire_import.py --dir "~/Downloads/招聘简历/待整理下载" --dry-run
.venv/bin/python scripts/hire_import.py --dir "~/Downloads/招聘简历/待整理下载"

# 重点岗位
.venv/bin/python scripts/hire_import.py --init-jobs      # 交互式设置
.venv/bin/python scripts/hire_import.py --show-focus     # 查看

# 岗位映射（招聘网站岗位名 → 飞书职位，解决两边名字对不上）
.venv/bin/python scripts/hire_import.py --show-alias
.venv/bin/python scripts/hire_import.py --map-job "社区产品经理（示例业务）" "社区产品经理"

# 入库记录 / 摘要
.venv/bin/python scripts/hire_import.py --report           # 今天
.venv/bin/python scripts/hire_import.py --report --days 7  # 最近 7 天
.venv/bin/python scripts/hire_import.py --digest           # 昨天摘要（每天 9:30 自动发）
```

---

## 五、常见问题

**Q: 下载了简历但没入库？**

1. 确认后台服务在跑：`launchctl list | grep resume-import`
2. 手动重启：
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.example.resume-import
   ```
3. 看日志：`data/logs/hire_watch.log`
4. 确认文件落在了被监听的目录（默认 `~/Downloads/招聘简历/待整理下载` 和 `~/Downloads` 根目录）

**Q: 提示权限不足？**

`--check` 会打印缺少的权限和申请链接，发给管理员开通即可。

**Q: 同一个人导了两次会重复吗？**

不会。脚本先按手机号/邮箱查重：已存在则**把新简历追加到原记录**，不会新建人才；
同一份简历重复导入会跳过追加。

**Q: 要不要开 LLM 选岗？**

可选。不开也能用（关键词匹配 + 岗位映射）。开启方式：

```bash
.venv/bin/python scripts/hire_import.py --setup-llm   # 交互：填 key → 选模型 → 保存
.venv/bin/python scripts/hire_import.py --llm-status  # 查看当前配置
.venv/bin/python scripts/hire_import.py --llm-off     # 关闭
```

配置保存在本机 `data/llm_config.json`，不会上传；服务自动读取，无需重启。

**Q: 入库后没有投递？**

说明没匹配到岗位：先 `--show-focus` 看重点岗位是否配置，或用 `--map-job` 建立映射。
配置了重点岗位但没匹配上时，脚本**故意不建投递**，避免投错岗位。

**Q: 想让浏览器不要抢焦点？**

默认已经是后台打开（不抢焦点）。如需改为前台打开，把服务参数里的
`--open-mode background` 改成 `foreground`。

**Q: 如何停止 / 卸载？**

```bash
# 停止后台服务
launchctl bootout gui/$(id -u)/com.example.resume-import

# 彻底卸载：删除服务配置 + 双击入口 + 项目目录
rm -f ~/Library/LaunchAgents/com.example.resume-import.plist
rm -f ~/Library/LaunchAgents/com.example.resume-digest.plist
rm -f ~/Downloads/招聘简历/*.command
```

---

## 六、数据与隐私

- 简历文件、日志、配置都**只存在本机** `data/` 目录
- 上传到飞书的只有：简历附件、姓名、手机、邮箱（可选的教育/工作经历）
- `data/browser_state/` 含招聘网站登录态，**不要外发**
- 打包分发给同事时，**不要带 `data/` 和 `archive/`**（含隐私与历史登录态）

---

## 七、给管理员的一次性准备

1. 在 [飞书开发者后台](https://open.feishu.cn/app) 为应用
   **一轮**开通并审批以下权限（申请说明填：`招聘提效工具-简历自动入库`）：

   | 权限 | 级别 | 用途 |
   |---|---|---|
   | `hire:talent` | 应用 | 建人才 / 追加简历 |
   | `hire:attachment` | 应用 | 上传招聘附件 |
   | `hire:job:readonly` | 应用 | 读职位列表 |
   | `hire:application` | 应用 | 建投递 |
   | `mail:user_mailbox.message:readonly` | 用户 | 邮件列表 |
   | `mail:user_mailbox.message` | 用户 | 邮件详情（发件人、主题） |
   | `mail:user_mailbox.message.attachment:readonly` | 用户 | 附件下载 |

   可选：`hire:people_cli`、`hire:job.composite_info:readonly`。

2. 给每位同事的机器配置好 `lark-cli`（同一应用），并让同事完成一次 `lark-cli auth login`
   （用户级邮件权限在这一步变成「已生效」）。
3. 把项目目录分发给同事（不含 `data/`、`archive/`）。安装路径可以含空格。
4. 用 `.venv/bin/python scripts/hire_import.py --check` 核对四态全部为「已生效」。

## 安全说明（2026-11 更新）

- **数据目录权限**：`data/` 与 `data/logs/` 已收紧为 700，日志文件 600（仅本人可读）。
- **日志脱敏**：控制台/日志/报告页中的手机号与邮箱均打码（如 138****5678）；完整数据仅存于本机 `data/hire_import_log.jsonl`（600 权限）。
- **LLM 输入脱敏**：发给大模型的简历文本会先去掉手机号、邮箱、身份证号与链接，只保留内容。
- **安装包校验**：客户端下载更新包后会校验 `latest.json` 里的 `sha256`，不匹配直接中止；且只接受 https 地址（本机测试除外）。
- **原子安装**：新版本先装到临时目录，成功后才切换；失败不影响现有版本。
