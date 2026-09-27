# SkyMail 独立账号邮箱池

适用于 `https://neo.dpdns.org` 的 50 个独立登录邮箱账号。无需管理员邮箱凭据；不创建用户、不发信、不删除邮箱或邮件。不使用 AI。

## 本地配置

在项目根目录执行（不用启动新的端口）：

```bash
.venv/bin/python -m vat_automation.mail_pool init
```

生成 `.mail-pool/accounts.json`，内有 50 个空白账号条目，按希望轮询的顺序在本机填写。邮箱密码和 GG 密码不是一回事。此目录已被 Git 忽略；目录权限 0700，JSON 文件权限 0600。不要把清单上传网页、聊天或工单，也不要放入 `.env` 让 Chrome 继承。

格式如下（仅示例，不能原样用于真实注册）：

```json
{
  "base_url": "https://neo.dpdns.org",
  "code_rules": [],
  "accounts": [
    {"email": "mailbox01@example.com", "password": "在本机填写邮箱密码"},
    {"email": "mailbox02@example.com", "password": "在本机填写邮箱密码"}
  ]
}
```

填写后导入，先检查第一个账号，确认实例行为与文档一致，再检查全部并启用：

```bash
.venv/bin/python -m vat_automation.mail_pool import .mail-pool/accounts.json
.venv/bin/python -m vat_automation.mail_pool check --index 1
.venv/bin/python -m vat_automation.mail_pool check --all
.venv/bin/python -m vat_automation.mail_pool enable
.venv/bin/python -m vat_automation.mail_pool status
```

检查只会登录、确认身份和读取一条邮件摘要；不会输出令牌、密码或邮件内容。令牌仅缓存在当前任务内。遇到错误停止检查，不反复尝试密码。全部导入账号检查通过才能启用；重新导入会停用邮箱池并要求重新检查。编辑器若改变文件权限，先执行 `chmod 600 .mail-pool/accounts.json`。

CLI 的 `--directory` 用于隔离诊断；Web 当前固定使用项目根目录 `.mail-pool`，不要把另一个目录的配置误认为已经接入 Web。

## 在 Web 中使用

升级后等现有任务结束再重启 `vat-web`。确认公司资料并明确填写公司电子邮箱 / EORI 通知邮箱（防止原流程的空值回退使用池内邮箱）、选择“创建新的 GG 账号”，勾选“使用邮箱池分配开户邮箱”。HMRC 密码仍需提供；可同时启用 [Authenticator 自动管理](authenticator.md)，不启用时按原短信流程填写验证手机号。

- 正常新申请按清单顺序分配，忙碌、未通过检查、停用或冷却中的邮箱跳过。
- 同一申请归属重试使用原邮箱；有保存邮箱时只使用该地址，不偷偷覆盖它。
- 分配结果保存在当前用户的私有 HMRC 凭据中；邮件密码不会传给 HMRC 执行器。
- VAT 任务将同一池内邮箱用于 GG 开户和申请的「个人电子邮箱」（`email`，`/register-for-vat/email-address`），覆盖原解析值并同步保存到当前用户的 VAT 资料草稿和办理记录。启动前显示自动分配提示，分配后显示实际地址；暂停修改资料也保留该地址。
- 「公司电子邮箱」（`vat_contact_email`，原标签“VAT沟通邮箱”）和 EORI 通知邮箱不变。EORI 不覆盖个人邮箱；未启用邮箱池时沿用资料里的邮箱。
- 已有 GG 登录不分配新邮箱；本版也不自动读取其账号恢复或 MFA 邮件。
- 暂停期间保留占用；任务结束释放并冷却 5 分钟。全部忙碌时提示稍后重试，本版不自动排队或开启第二个申请。

## 自动收码：必须先核对真实邮件模板

未配置 GG 用途的 `code_rules` 时，GG 建号验证码继续由用户输入。VAT 个人邮箱验证码已有经本机邮件样例确认的内置模板，无需修改邮箱账号配置；不能凭“某个六位数”猜验证码。

当前支持两个独立环节：

- GG 建号：`/registration/email` → `/registration/code`，用途 `gg_signup`。
- VAT 个人邮箱：`/register-for-vat/email-address` → `/register-for-vat/email-address-verification`，用途 `vat_personal_email`。仅在启用邮箱池的 VAT 任务中接入，确认将提交的个人邮箱与本次分配地址一致后才自动取码。

两者使用同一邮箱，但各自记录发码前游标、时间和尝试状态，且只匹配本用途模板。完成 GG 自动验证不会耗掉 VAT 环节的自动尝试，也不会把 GG 邮件误用于 VAT。官方域名及页面路径必须匹配，不能按“第三次验证码”的出现次数猜用途。
不覆盖公司联系邮箱、EORI 通知邮箱验证或重发验证码。Authenticator 的自动动态码由独立模块处理。模板按发件地址、主题、正文中紧邻代码的固定前缀、字符集和长度匹配，不能默认所有验证码都是数字。

2026-09-26 根据用户提供的本机 `.eml` 样例，已确认该 GG 建号邮件使用以下模板。样例中的实际验证码、收件人和链接未复制到配置或文档中；静态样例通过不代表网站端已验证成功。`.eml` 已加入 Git 忽略。

```json
"code_rules": [{
  "purpose": "gg_signup",
  "sender": "no-reply@access.service.gov.uk",
  "subject_contains": "Confirm your email address - Government Gateway",
  "code_prefix": "Your confirmation code is",
  "charset": "uppercase_letters",
  "length": 6
}]
```

数字验证码使用 `"charset": "digits", "length": 6`；旧格式 `"digits": 6` 仍兼容。大写字母模板不会接受纯数字、小写字母或超长字符串；两种格式不要混填。修改本机导入清单中的模板后，通常需要重新导入、检查和启用，不能只改导入文件就认为运行配置已更新。

根据 `materials/Your_email_confirmation_code.eml` 核对的 VAT 内置模板为：

```json
{
  "purpose": "vat_personal_email",
  "sender": "noreply@tax.service.gov.uk",
  "subject_contains": "Your email confirmation code",
  "code_prefix": "Your code is",
  "charset": "uppercase_letters",
  "length": 6
}
```

纯文本和 HTML 正文均按 `Your code is:` 后的代码提取，不依赖段落字体或 CSS 样式。样例中的实际验证码和收件人不进入配置；内置规则可由同用途的显式配置替代。本次升级不重新导入邮箱账号、不改变邮箱启用、已检查状态、轮询顺序或占用。
自动填写失败时只报告固定错误；VAT 个人邮箱验证码页与 GG 认证页一样不保存真实截图 / PDF，避免输入值进入运行产物。

发码前记录邮箱游标和时间；只读取该 `accountId` 的收件（`allReceive=0`），再次精确核对收件人。正文只在内存中解析，HTML 不执行、不加载资源。每 4 秒按游标增量查询，最多等待 150 秒，整个过程可停止或由人工输入抢先接管。

单次发码至多自动提交一次；多封候选、无效时间、游标异常、网络/鉴权错误、限流或超时均转人工，不自动重发、不逐个试码。使用过的邮件 ID 持久记录，验证码和正文不落库。发件地址匹配不是邮件身份的密码学证明；邮件服务本身仍需做好反伪造和访问控制。迟到邮件不能仅靠时间绝对区分，冷却和模板过滤也不构成零串码保证，有歧义必须人工处理。

## 运维与安全

- `.mail-pool/config.json` 含明文邮箱密码，依靠本机权限保护，不是加密保险箱；限制服务器访问并加密备份。
- SQLite 只保存顺序、申请绑定摘要、邮箱 ID、占用及已尝试邮件 ID，不保存密码、令牌、正文或验证码。
- 邮箱池是共享基础设施，普通 Web 用户不能浏览它的收件箱或导出凭据，只能查看本人任务状态。
- 调度采用 SQLite 事务；异常退出的占用不会自动过期，避免原浏览器仍在运行时被重复分配。
- 如需修改邮箱清单，先结束活动任务。建议暂停新任务后执行连接检查，避免令牌重登影响正在运行的请求。
- 若程序崩溃，必须先确认原任务和浏览器已经停止，再由本机管理员解除指定序号的占用：

```bash
.venv/bin/python -m vat_automation.mail_pool release --index 1 --confirm-stopped
```

解除占用不删除申请绑定，并保留 5 分钟冷却。不能在原任务仍运行时强制解除。可用 `disable` 阻止新的分配；已运行任务保留其配置，需停止任务才能中止取码。

正式提交仍由用户在 Check your answers 核对并明确确认。本功能不修改该安全边界。

接口依据：https://doc.skymail.ink/api/api-doc.html 。部署版本、限流、令牌失效行为及实际 HMRC 邮件仍需在本机受控验证；公开文档与本地模拟不等于真实联调成功。
