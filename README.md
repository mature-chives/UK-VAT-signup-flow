# UK VAT registration automation

这是一个基于 Python + Playwright 的英国 VAT 注册流程驱动器。当前目录中的 178 张截图用于确认流程入口、GOV.UK 页面结构以及主要交互类型；程序运行时按页面真实标签匹配配置，不依赖易变化的 CSS class。

## 安全边界

- Government Gateway 用户名和密码从环境变量读取并自动填写；仅验证码暂停等待用户输入。
- 遇到缺少的必填字段、未知按钮或配置为 `stop` 的页面时立即停止，并保存截图与页面元数据。
- 初始诚信声明页会自动点击 `Accept and continue`；若检测到明显测试资料会直接停止。
- 到达 `Check your answers` 时保存整页复核文件并暂停；只有网页用户明确勾选确认后，程序才会点击 `Confirm and submit`。
- 测试配置默认设置 `allow_live_application: false`，进入真实 VAT 申请数据区前停止，防止把随机资料写入 HMRC。
- 浏览器会话保存在本地 `.browser-profile/`，运行记录保存在 `artifacts/`，两者均默认忽略，不应提交到版本库。
- 不要把 Government Gateway 用户名、密码或 MFA 密钥写入 JSON；用户名和密码仅通过进程环境变量传入，验证码只在内存中短暂使用。

## 安装

建议使用独立虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
```

默认使用系统已安装的 Chrome（`browser_channel: "chrome"`），因此通常无需额外下载 Chromium；上面的浏览器安装命令可作为没有 Chrome 时的备用方式。

## 配置

复制示例文件，然后使用真实资料替换所有 `REPLACE_ME`：

```bash
cp vat-config.example.json vat-config.json
```

`answers` 是跨页面的标签到答案映射；`pages` 可按 URL 路径或页面标题提供覆盖值。推荐优先使用截图或实际页面上完整的英文标签作为 key。单选题的值必须等于页面显示的选项文本，文件上传必须使用绝对路径。

示例：

```json
{
  "answers": {
    "Country": "China",
    "Business name": "Example Trading Ltd"
  },
  "pages": [
    {
      "match": {"path_contains": "/home-address/international"},
      "answers": {"Address line 1": "Example address"}
    }
  ]
}
```

多个国际地址页面使用同一地址时，推荐在顶层配置结构化 `address`。程序会按顺序生成地址行，并确保地址行和城市字段不超过 35 个字符：

```json
{
  "address": {
    "premises": "Room 601,602,603, Building 3",
    "street": "No. 528 Xingqi Road, Donghu Street",
    "locality": "Linping District",
    "city": "Hangzhou City",
    "region": "Zhejiang",
    "postcode": "填写真实邮编",
    "country": "China"
  }
}
```

生成结果为 `Room 601,602,603, Building 3`、`No. 528 Xingqi Road, Donghu Street`、`Linping, Hangzhou, Zhejiang`。某个页面需要不同地址时，可在对应的 `pages[]` 中使用同样的 `address` 对象覆盖。无法在 35 个字符内安全缩写时，程序会在打开浏览器前报错，不会截断地址。

同一标签在不同页面含义不同时，应放入 `pages[].answers`，页面配置会覆盖全局配置。`action` 支持：

- `continue`：填写完毕后点击 `Save and continue` 或 `Continue`，默认值。
- `stop`：填完当前页面后停止，适合文件上传或需要人工判断的步骤。

日期值 `date:uk-next-month-first.day`、`.month`、`.year` 会在每次启动时按英国 `Europe/London` 当地日期解析为下个月 1 日。12 月运行时会自动跨年到下一年 1 月 1 日。
`vat-return-stagger:uk-next-month-first` 会使用同一个注册日期月份，自动选择对应的季度 VAT Returns 申报组。

只有改用经过核实的真实申请资料后，才可显式设置 `allow_live_application: true`。启用后，初始诚信声明页会自动点击 `Accept and continue`；若配置或环境变量里仍有 `example.com`、`TEST-`、`07700 900xxx`、`Synthetic`、`Northstar`、`Tester` 等测试标记，程序会拒绝点击声明按钮。最终复核会强制暂停并等待网页用户明确确认，未确认时不会提交。

## 运行

```bash
vat-register --config vat-config.json
```

生成纯虚构资料并进行测试：

```bash
python scripts/generate_test_config.py --headed
vat-register --config vat-config.test.json
```

每次使用全新浏览器会话、从 GOV.UK `Sign in or set up an account`
重新开始：

```bash
vat-register --config vat-config.test.json --fresh-session
```

`--fresh-session` 不复用 Cookie、登录状态或浏览器历史，不能与 `--resume`
同时使用。流程结束后临时浏览器配置会自动删除。

浏览器打开后会自动登录；验证码到达时，程序会在终端暂停等待输入。配置不足时，查看：

- `artifacts/current-page.json`：当前 URL、标题和缺少的字段。
- `artifacts/*-missing-config.png`：停止时的完整页面截图。
- `artifacts/audit.jsonl`：访问页面和点击动作的审计记录，不记录填写值。

补充配置后，可复用登录会话和最后 URL：

```bash
vat-register --config vat-config.json --resume
```

登录凭据和可选的 MFA 设置通过环境变量提供：

```bash
export HMRC_EMAIL='用于接收开户验证码的真实邮箱'
export HMRC_PASSWORD='你的密码'
export HMRC_SIGN_IN_METHOD='Create new sign in details'
export HMRC_IS_TAX_AGENT='No'
export HMRC_ACCESS_AS_BUSINESS='Yes'
export HMRC_MFA_METHOD='Text message'
export HMRC_MFA_PHONE='用于接收验证码的手机号'
export HMRC_MFA_PHONE_IS_UK='No'
export HMRC_MFA_PHONE_COUNTRY='China'
vat-register --config vat-config.test.json
```

首次开户时，程序会自动选择 `Create new sign in details`、回答主体问题、填写邮箱、姓名和密码，并选择验证方式；邮箱验证码和手机验证码到达后，在终端提示处输入即可继续。已有账号登录时，可将 `HMRC_SIGN_IN_METHOD` 改为 `Government Gateway`，并额外提供 `HMRC_USER_ID`。凭据和验证码不会写入审计日志。

## 本地 Web UI

安装依赖后直接启动：

```bash
.venv/bin/python -m pip install -e .
.venv/bin/vat-web --config vat-config.json
```

还没有账号时，终端会打印一个仅本次启动有效的初始化码，浏览器打开后会自动进入“创建管理员账号”页面，输入初始化码即可完成初始化。管理员登录后可在页面右侧“账号管理”区块为同事添加账号、重置密码或删除账号（首个账号自动是管理员，最后一个管理员不可删除）。命令行方式 `vat-web-user add/remove/list` 仍然可用。

浏览器会自动打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，先登录再使用。页面支持：

- 上传 PDF、DOCX、XLSX、TXT、JSON、CSV 或 TSV 资料文档，在本地提取 VAT 字段；
- 只返回英国 VAT 注册会使用的字段，忽略签证、前任会计师、平台链接等无关内容；
- 在页面检查和修正提取结果，再将其合并到自动化配置；
- 输入开户邮箱、密码和 MFA 手机号；
- 一次上传三份身份证明，后续按 HMRC 页面顺序自动上传；
- 默认创建新登录信息，手机号国家为 `China`，英国手机号为 `No`；
- 固定使用标准税率营业额 `10000`、降低税率 `0`、零税率 `0` 和 SIC `47910`；
- 启动可见的本地 Chrome 自动化；
- 在网页中输入邮箱验证码和短信验证码；
- 在安全页面边界暂停自动化，修改并重新确认资料后从当前 HMRC 页面继续；
- 到达 `Check your answers` 时先点击 `Show all sections` 展开全部答案，再保存整页 PDF（失败时保存整页截图）供下载核对；如需修改，网页会列出 HMRC 的全部 `Change` 项，并把所选修改页的字段、当前值、选项和校验错误同步到远程网页，用户保存后自动继续并重新生成最终核对存档；逐项核对并明确勾选确认后，程序点击 `Confirm and submit`；
- 查看当前 HMRC 页面、运行状态和最近操作；
- 显示安全停止或失败原因。

Web UI 默认只绑定 `127.0.0.1`，一次只运行一个任务。配置文件路径由启动参数 `--config` 在服务端固定，网页无法修改。文档提取不调用外部 AI 或网络 API；三份身份证明按账号隔离，以 `0600` 文件权限保存在 `0700` 本地临时目录，进程正常退出时删除。密码和验证码不会写入配置、状态接口或审计日志，密码提交成功后会从页面输入框清空。使用 `Ctrl+C` 关闭本地服务。

暂停不会关闭 Chrome 或丢失 HMRC 登录会话。点击“应用修改并继续”后，程序将新资料合并到当前任务并重新处理当前页面。暂停和最终核对的等待都有 30 分钟上限，超时后自动结束并关闭浏览器。文档中形如 `8613592850576(国际区号+电话号)` 的电话号码会清洗为纯数字 `8613592850576`，不自动补 `+`。

资料文件名应包含项目编号，例如 `客户信息及服务授权表-新注册VAT-AB223322公司名.docx`。程序会结合文档中的公司英文名生成 HMRC 申请参考名称：`AB223322-UK-公司英文名`。

指定其他端口或不自动打开浏览器：

```bash
.venv/bin/vat-web --config vat-config.json --port 9000 --no-open
```

## 局域网多人使用

同事的电脑只需要浏览器，不需要安装 Playwright；自动化的 Chrome 始终在服务器这台机器上运行。同事在网页上看到的是状态文字、验证码输入框和最终核对的 PDF，不是 HMRC 页面本身。

1. 以局域网地址启动。程序会自动生成自签 TLS 证书（`certs/`，SAN 包含本机局域网 IP 和 `.local` 主机名），并打印同事可访问的地址：

   ```bash
   .venv/bin/vat-web --config vat-config.json --host 0.0.0.0
   ```

   无法准备证书时会拒绝启动。也可用 `--ssl-certfile` / `--ssl-keyfile` 指定自备证书（例如 mkcert 签发）。`--insecure-http` 可跳过 TLS，但登录密码、HMRC 凭据、验证码和客户身份证件都会明文过网，不要在正式使用时开启。

2. 首次使用：在自己浏览器打开页面，按终端打印的初始化码创建管理员账号，然后在“账号管理”区块为每位同事添加账号（密码至少 12 个字符）。

3. 同事首次访问 `https://<服务器IP>:8765` 会看到自签证书警告，手动选择继续访问一次即可；之后登录自己的账号使用。

安全边界与限制：

- 所有 API 都要求登录；登录失败连续 5 次会按 IP 和用户名锁定 5 分钟起（指数退避）。会话有效期 8 小时，重启服务后所有人需重新登录。
- 所有写操作都有 CSRF 防护（SameSite Cookie + 来源校验 + 请求头令牌）。
- 每位同事的提取资料、身份证明文件、运行状态、最终核对文档互相隔离；审计日志记录操作人。
- 一次只运行一个客户的申请。他人任务运行时，页面会显示当前占用者，需等待其结束。
- HMRC 凭据只通过内存传给自动化进程，不写入进程环境变量，不会被浏览器子进程继承。
- **正式提交必须人工确认。** 到达 `Check your answers` 后程序保存复核文件并暂停；只有当前任务用户在网页勾选确认并点击“确认并提交到 HMRC”，程序才会精确点击 `Confirm and submit`。30 分钟内未确认则关闭浏览器且不提交。
- 服务器本机是整个系统的安全边界：`artifacts/` 内有含客户个人信息的截图和 PDF，请勿共享该机器或让其休眠导致会话中断。

## 验证

```bash
python -m compileall -q vat_automation tests
python -m unittest discover -s tests -v
python scripts/check_flow_coverage.py vat-config.test.json
```

覆盖检查基于截图整理出的 84 个逻辑页面；“covered”表示已有专用规则、通用字段映射或特殊页面处理，不代表每个分支都已在真实 HMRC 环境提交验证。
