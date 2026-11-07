# 部署说明（公司服务器）

> 只需要**静态文件托管**，不需要任何服务端程序、数据库或常驻进程。成本最低。

## 一、服务器上要放什么

```
<你的静态目录>/resume-hunter/
├── latest.json                      # 版本清单（客户端靠它判断更新）
└── resume-hunter-v1.0.1.zip         # 安装包
```

## 二、部署（两种方式任选）

### 方式 A：一条命令（推荐）

本地执行：

```bash
cd ~/.claude/skills/resume-hunter

# 1. 构建发布包（版本号自增）
bash scripts/build_release.sh v1.0.2 "https://<你的域名>/resume-hunter"

# 2. 上传到服务器
bash scripts/deploy_release.sh <user@host> /var/www/resume-hunter
```

### 方式 B：手动

```bash
bash scripts/build_release.sh v1.0.2 "https://<你的域名>/resume-hunter"
scp dist/www/* <user@host>:/var/www/resume-hunter/
```

## 三、服务器静态配置（nginx 示例）

```nginx
location /resume-hunter/ {
    alias /var/www/resume-hunter/;
    autoindex off;
    add_header Cache-Control "no-cache";   # 保证 latest.json 不被缓存
}
```

> 如果公司已经有静态站点（如 Nexus / OSS / 对象存储 / 内部 Wiki 附件），
> 直接把 `dist/www/` 里的两个文件放上去、拿到公开链接即可，不用 nginx。

## 四、更新流程（你以后每次发版）

```bash
# 1. 改完代码后，起新版本号
bash scripts/build_release.sh v1.0.3 "https://<域名>/resume-hunter"

# 2. 上传
bash scripts/deploy_release.sh <user@host> /var/www/resume-hunter

# 3. 同事下次打开客户端 App → 自动检测到新版本 → 下载 → 重装
```

同事客户端无需任何操作，双击 App 即为"检查更新 + 修复安装"。

## 五、客户端分发

```bash
bash scripts/build_client.sh "https://<域名>/resume-hunter" v1.0.3
# 产物：dist/简历一键入库.zip
```

把 `简历一键入库.zip` 发给同事：

1. 解压得到「简历一键入库.app」
2. 首次打开：右键 → 打开（未签名 App 需要一次确认）
3. 之后：在 Boss/猎聘点「附件简历 → 下载」即自动入库

## 六、验证部署是否成功

```bash
curl "https://<域名>/resume-hunter/latest.json"
```

应返回包含 `version` 和 `url` 的 JSON。
客户端日志：`~/Library/Application Support/ResumeHunter/logs/launcher.log`

## 七、注意事项

- **秘钥**：当前方案把 app_secret 打包在安装包里（见 INSTALL.md 的安全说明），
  泄露风险由"只开招聘 4 项权限 + 可轮换"控制；后续可升级为服务端代理。
- **未签名 App**：macOS 会提示"无法验证开发者"，右键打开一次即可；
  如需正式分发，申请 Apple Developer 证书后 `codesign --deep --force -s "Developer ID"`。
- **缓存**：`latest.json` 必须禁缓存（`Cache-Control: no-cache`），否则客户端永远看到旧版本。
