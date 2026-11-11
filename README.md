# Supplier Scout · 寻源供应商工作台

供应商模型报价与联系方式研究工具。支持本机运行和账号密码保护的云端部署。

## 云端结构

- Vercel Python HTTP Function：登录、名单、报价/联系方式、来源证据、导出与持久任务入队。
- 独立私有 Postgres：供应商、报价历史、会话、加密模型配置、任务租约及检查点。
- Vercel Linux Daemon：Moli 浏览器观察/操作循环；支持公告、联系入口及真实二维码解码。
- 联系方式模式保留原报价和人工结论。停止请求、发布中断和过期租约不会让旧进程覆盖新结果。

所有业务页面、接口和导出均需登录。没有注册入口或默认生产密码。会话仅保存随机 token 的 SHA256 摘要；模型 Key 使用独立环境密钥加密保存。源码不包含个人名单、数据库、截图或凭据。

## 本机运行

```sh
python3 server.py --port 8765
```

现有本机工作区默认不启用外网登录保护。云端入口始终要求登录配置。数据目录 `data/` 不应删除或提交。

## 云端环境变量

| 变量 | 用途 |
| --- | --- |
| `DATABASE_URL` | 私有应用角色的 Postgres TLS 连接地址 |
| `SCOUT_ENCRYPTION_KEY` | 独立 Fernet 密钥，用于加密模型配置 |
| `SCOUT_AUTH_USERNAME` / `SCOUT_AUTH_PASSWORD_HASH` | 登录名及 `auth.hash_password()` 生成的口令校验值 |
| `SCOUT_VERCEL_PROJECT_ID` / `SCOUT_VERCEL_TEAM_ID` | 当前项目的浏览器进程管理范围 |

云端浏览器启停使用当前生产项目的 Vercel OIDC 凭据；也支持独立的项目级 `SCOUT_VERCEL_SCALER_TOKEN`。凭据不返回浏览器，不写入源码。批次进度与停止标记保存在数据库。

`vercel.json` 默认不运行空闲浏览器实例。任务入队时启动，空闲60秒后停止，避免持续消耗免费内存额度。必须在生产环境验证授权与实际进程状态，不能仅凭 HTTP 200 判定采集成功。

数据库使用 `scout_private` 私有 schema，禁止匿名/普通 Data API 角色访问。先通过部署管理员创建独立应用登录角色，再应用 `cloud_schema.sql`。`scripts/import_local.py` 仅在云端供应商表为空时导入原 SQLite，并重置序列；API Key 单独加密迁移。不要把导入源文件上传到代码仓库。

Linux Daemon 镜像锁定 Python、Node、Moli 版本及下载校验值。二维码使用 Node 解码，macOS 本机保留 Vision。云端文件上传上限3MiB；本机保持5MiB。

## 验证

```sh
python3 -m unittest discover -s tests -v
node --check static/app.js
```

云端数据库集成测试仅在迁移前的临时空项目中通过 `SCOUT_TEST_DATABASE_URL` 启用。不要对真实名单执行破坏性初始化。

邮件目前仅生成草稿，由人工发送；飞书自动同步和定时报表尚未接通。
