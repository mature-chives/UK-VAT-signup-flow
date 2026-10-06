# tCloud 内部试用部署

本项目使用独立 Docker Compose 项目 `uk-vat-signup`，不调整其他容器、代理或服务。Chrome 与 Xvfb 运行在容器内；不安装 OCR 可选依赖。一次仍只运行一个自动化任务。

本次 tCloud 在独立新目录部署，按用户要求迁移本地 Web 用户、客户、GG 凭据、邮箱池、验证器和 `.env`，沿用原有 Web 登录账号。私有数据通过 SSH 单独传输；SQLite 使用在线备份接口。原环境数据保留，不复制浏览器会话，不恢复执行旧任务。两端不要同时使用同一组邮箱办理。

## 域名入口

域名为 `uk.amberaccount.cn`，A 记录指向 `43.162.101.208`。宿主机 Nginx 新增独立站点 `/etc/nginx/sites-available/uk-vat-signup`，通过 `sites-enabled` 启用；配置来源为 `deploy/nginx-domain.conf`，不修改其他域名站点。

访问路径：`https://uk.amberaccount.cn` → 宿主机 Nginx 443 → `https://127.0.0.1:16671` → 项目 Nginx → Web。外部域名证书与容器内部自签证书分开，内部两级代理都校验后端证书。域名入口只需要云防火墙放行 80/443，不要求公网开放 16671。

域名证书通过 Certbot webroot 方式申请，验证目录 `/var/www/uk-vat-acme`，证书路径 `/etc/letsencrypt/live/uk.amberaccount.cn/`。沿用服务器的 `certbot.timer` 自动续期；域名专属部署钩子 `/etc/letsencrypt/renewal-hooks/deploy/uk-vat-signup`（源码 `deploy/renew-domain.sh`，root 所有、0750）仅在该证书续期后检查并平滑重载 Nginx。普通 HTTP 请求跳转到 HTTPS，证书验证路径保留 HTTP 访问。

## 目录与启动

服务器项目目录：`/home/ubuntu/uk-vat-signup`，归属 UID 1000，目录权限 0700。

```sh
cd /home/ubuntu/uk-vat-signup
mkdir -p certs
chmod 700 certs
docker compose build web
docker compose up -d
```

容器入口为 `https://127.0.0.1:16671`，由本项目独立 Nginx 容器反向代理；Web 后端不映射宿主机端口。后端与代理之间也使用 TLS，代理校验后端证书，因此后端可直接识别 HTTPS，不需要放宽代理信任或二维码权限。容器内部使用包含服务器 IP 的自签证书，日常访问使用上方域名入口，无需手动信任自签证书。

其他环境可通过 `VAT_PUBLIC_HOST` 和 `VAT_HTTPS_PORT` 指定访问域名/IP、端口。已有证书不会在启动时覆盖；默认后端证书还含 `localhost`，供 Nginx 校验。生产使用可为 Nginx 单独挂载受信任的域名证书，保留后端的内部证书，不要关闭后端证书校验。

## 私有数据

`.dockerignore` 仅允许代码、依赖清单、流程配置和启动脚本进入镜像；密钥和客户数据不进 Git 或镜像。运行时服务器项目目录挂载到 `/app`，以下内容需私下配置或迁移：

- `.env`：默认密码、翻译 API 等配置。
- `users.json`：Web 登录账号。未迁移时首次启动需用初始化码创建管理员。
- `hmrc-credentials.json` 与 `vat-web-data/`：客户归属、GG 凭据、初稿及办理记录。
- `.mail-pool/`：邮箱账号、绑定和租约状态。
- `.authenticator-key` 与 `.authenticator/`：必须成套保留，否则无法解密原验证器。
- `certs/`：本服务器 TLS 证书和私钥。

私有文件保持 0600，私有目录保持 0700。迁移 SQLite 时使用在线备份接口，避免只复制数据库主文件遗漏 WAL。不要复制旧浏览器会话；不要让两台环境同时使用同一组邮箱运行任务。迁移数据不代表自动恢复或重新提交旧任务。

升级时先结束本项目正在运行的注册任务，再更新代码、重新构建并启动 `web`。备份上述私有数据，保留对应 Git 提交用于回退；不要执行全服务器清理命令。

部署只确认服务启动和 HTTPS 入口可用，不等于真实 VAT/EORI 注册或审批成功。已知 VAT 在人工确认前浏览器先进入反馈页时的收尾判断问题仍待单独修复。
