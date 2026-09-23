# 英国 EORI 注册流程（自动化实现说明）

> 依据：`materials/` 下客户提供的 EORI 注册流程文档（22 张截图，属客户内部资料，未提交进仓库）。
> 截图里的客户真值只用来核对页面结构，代码和配置里一律用 `doc:`／`env:` 占位符。

## 1. 流程概览

HMRC 的 EORI 服务与 VAT 服务同一个域名（`tax.service.gov.uk`），但入口、页面和
提交按钮不同，所以单独放在 `vat_automation/eori_flow.py`，与 VAT 流程互不影响。
海外（非英国）公司走 `third-country-organisation` 分支，不需要 UTR。

| 截图 | 页面 | 自动化取值 |
| --- | --- | --- |
| image1、image2 | GOV.UK「Get an EORI number」「Apply for an EORI number」指南页 | `start_url` 入口，点 `Start now` |
| image4 | HMRC 登录方式（Government Gateway／One Login／Create new sign in details） | `HMRC_SIGN_IN_METHOD`，缺省用流程配置的 `default_sign_in_method`（EORI 默认新建账号） |
| image5 | Government Gateway 短信验证码 | 人工在网页输入 |
| image3 | `/register/vat-group`：Is your organisation part of a VAT group in the UK? | `No` |
| image6 | `/register/matching/what-is-your-email`：What email address can we use for customs notifications? | `doc:vat_contact_email` 或 `env:HMRC_EMAIL` |
| image7 | `/register/matching/check-your-email`：Is … the email address you want to use? | `Yes`（标题里带客户邮箱，靠 `default_answer` 兜底） |
| image8 | Enter code to confirm your email address | 人工在网页输入 |
| image9 | Where is your organisation established? | `Rest of the world` |
| image10 | What do you want to apply as? | `Organisation` |
| image11 | What is your registered company name? | `doc:business_name` |
| image12 | Does your organisation have a Corporation Tax UTR issued in the UK? | `No` |
| image13 | Enter your organisation address | `doc:business`（EORI 四栏地址） |
| image14 | When was the organisation established? | `company_incorporation_date` 拆成日/月/年 |
| image15 | Your Standard Industrial Classification (SIC) code | `47910`（与 VAT 注册一致） |
| image16 | Do you consent to show the organisation's name and address… | `No` |
| image17 | Is your organisation VAT registered in the UK? | `Yes` |
| image18 | Your UK VAT details（VAT 号 + VAT 注册地址邮编） | `doc:vat_number`、`doc:business_postcode` |
| image19 | When did you become VAT registered? | `vat_registration_date` 拆成日/月/年 |
| image20 | EORI number application contact details | `doc:full_name`、`doc:phone` |
| image21 | Do you want us to use this address to send you information… | `Yes` |
| image22 | Check your answers | 存整页 PDF 并暂停，人工确认后才点提交 |

## 2. 与 VAT 流程的差异

| 项目 | VAT 流程 | EORI 流程 |
| --- | --- | --- |
| 入口 | `gov.uk/log-in-register-hmrc-online-services` | `gov.uk/eori/apply-for-eori` |
| 申请路径 | `/register-for-vat/…` | `/customs-registration-services/eori-only/register/…` |
| 地址栏 | HMRC 国际地址表 5 行 | 两行街道地址 + Town or city + Region or state + 邮编 + 国家 |
| 身份证明 | 必须上传 3 份 | 不需要（`identity_documents_required: 0`） |
| 最终核对页 | `/register-for-vat/check-your-answers` | `/register/review-details` |

## 3. 资料袋契约

Web UI 解析授权表后按下面的键提供给自动化；命令行配置里用同名字面量。

| doc: 键 | 说明 |
| --- | --- |
| `business_name` | 公司正式名称 |
| `full_name` | 联系人姓名 |
| `phone` | 联系人电话 |
| `premises`／`street`／`locality`／`city`／`region`／`postcode`／`country` | 公司注册地址 |
| `vat_number` | 英国 VAT 号（9 位，可带 GB 前缀） |
| `vat_registration_date` | VAT 注册生效日期 |
| `company_incorporation_date` | 公司成立日期 |
| `vat_contact_email` | 接收 customs 通知的邮箱，缺省用 `env:HMRC_EMAIL` |

`vat_registration_date`、`company_incorporation_date` 会被 `prepare_document_values()`
自动拆成 `<prefix>.day/.month/.year`（前缀分别是 `vat-registration-date`、
`company-incorporation-date`）。日期支持 `YYYY-MM-DD`、`DD/MM/YYYY`、`DD-MM-YYYY`、
`YYYY/MM/DD`，解析不了会直接报错，不会猜。

## 4. 运行方式

```bash
# 改动 eori_flow.py 后重新生成可提交的流程配置
.venv/bin/python scripts/generate_eori_config.py

# 单用户 Web UI（资料袋解析 + 验证码 + 最终核对），EORI 用 8766 与 VAT 分开跑
.venv/bin/vat-web --config vat-config.eori.flow.json --port 8766

# 销售交付工作台：一个菜单里同时有「英国 VAT 注册」和「英国 EORI 注册」
.venv/bin/vat-bench --uk-vat-config vat-config.flow.json \
  --uk-eori-config vat-config.eori.flow.json

# 命令行：先生成纯虚构的冒烟配置，不会写进真实申请
.venv/bin/python scripts/generate_eori_config.py --test
.venv/bin/vat-register --config vat-config.eori.test.json

# 截图页面覆盖率检查
.venv/bin/python scripts/check_flow_coverage.py --flow eori

# 离线冒烟：本地起模拟页面，用真实 runner 跑完整条流程（不访问 HMRC）
.venv/bin/python scripts/eori_smoke.py
```

需要的登录环境变量与 VAT 流程相同：`HMRC_USER_ID`、`HMRC_PASSWORD`、
`HMRC_EMAIL`、`HMRC_MFA_PHONE`。登录方式优先级是：网页/环境变量给的
`HMRC_SIGN_IN_METHOD` > 流程配置的 `default_sign_in_method` >
`Create new sign in details`。

两种登录方式的选择依据是**客户有没有现成的 Government Gateway 账号**：

| 情况 | 登录方式 | 需要提供 | 会发生什么 |
| --- | --- | --- | --- |
| 新客户，没有 GG 账号 | `Create new sign in details`（默认） | 登录邮箱、手机号、密码（+ 姓名） | 程序在申请过程中建号：邮箱验证码 → 姓名 → 密码 → 生成 User ID → MFA 短信验证码 |
| 客户已有 GG 账号（例如我们自己帮客户做过 VAT 注册并留下 User ID/密码） | `Government Gateway` | Gateway User ID、密码，手机号用于 MFA | 直接登录，不会再走建号和邮箱验证 |

所以没有账号时选 Government Gateway 是走不通的：HMRC 会提示 User ID/密码不正确，
程序会停在认证页并保存 `auth-validation-error` 截图。
`HMRC_MFA_PHONE_COUNTRY` 缺省取资料里的 `country`。

### 账号从 VAT 注册传到 EORI

Government Gateway 账号通常是在**英国 VAT 注册**那一步新建的（程序会走
邮箱验证码 → 姓名 → 密码 → 生成 User ID → MFA）。把账号留下来给 EORI 复用：

1. VAT 注册跑到确认页时，程序会抓到 HMRC 显示的 Government Gateway User ID；
2. **工作台**会把该 User ID 和这次用的邮箱/密码/手机号自动挂到**该客户**名下
   （`workbench-data/credentials.json`，权限 0600，git 忽略），界面只显示掩码后的
   User ID；单用户 `vat-web` 则存在 `hmrc-credentials.json`（按登录账号），
   也可以点「保存登录信息到该客户」/「保存我的 HMRC 登录信息」手动补存；
3. 之后给同一客户跑 EORI 时，把登录方式改成 `Government Gateway`，输入框留空即可：
   取值优先级是 表单填写 > 该客户已保存 > `.env`/进程环境 > 本机上一位操作者的内存记录。

单用户 `vat-web` 没有客户概念，改为**按登录账号**存 `hmrc-credentials.json`
（0600、git 忽略）：跑完新账号后点「保存我的 HMRC 登录信息」，下次留空即用。
项目根目录 `.env` 只是可选的默认值来源（优先级最低），程序不再往里写。

### 固定密码 + 每客户 User ID（推荐配置）

如果每次注册用的邮箱/密码都一样（固定不变），这样分两层最省事：

| 内容 | 放哪 | 说明 |
| --- | --- | --- |
| `HMRC_EMAIL`、`HMRC_PASSWORD`（固定值） | 项目根目录 `.env` | 所有客户共用，填一次；网页密码框会提示"留空即用 .env 里的固定密码" |
| `HMRC_USER_ID`（每个客户不同） | 客户记录 / 项目编号记录 | 建号成功后自动保存，下次同客户跑 EORI 直接取到 |

合并是**按字段**做的：某个字段表单里填了就用表单的，否则用该客户已存的，再否则用
`.env` 的固定值。所以给已有账号跑 EORI 时，只要选 `Government Gateway` +
（可选）填 User ID，密码会自动用固定值补上。

## 5. 安全边界

- 进入 EORI 数据区（`/customs-registration-services/eori-only/`）前，若配置是
  `allow_live_application: false`，程序立即停止并保存截图，虚构资料不会写进真实申请。
- 到达 `review-details` 时保存整页 PDF／截图并暂停，只有网页用户明确确认后才点提交；
  暂停与复核都有 30 分钟超时。
- 提交按钮按 `Confirm and submit` → `Accept and submit` → `Submit` 依次尝试，
  都找不到就停止，不会误点别的按钮。
- 地址宁可报错也不截断：两行街道地址放不下时抛错，要求人工改地址。
- 出错（缺资料、找不到安全按钮、网络错误、HMRC 拒绝页）默认**停留 10 分钟**等人工：
  页面和 Chrome 都留着，网页上可点「继续当前任务」（从当前页重试）或「取消任务」；
  超时自动取消。停留时长由配置 `error_hold_seconds` 控制（0 = 出错立即停止）。

## 6. 真实环境试跑记录

2026-09-22 用工作台跑过一次真实申请（客户资料来自授权表），从 `Start now` 到
邮箱确认页共 20 个页面全部按配置走通，包含首次开户自动创建 Government Gateway
账号（邮箱验证码、短信验证码各由人工输入一次）和 MFA 设置。程序停在
`/register/matching/check-your-email`：当时配置按截图 OCR 写成 "the mail address"，
真实文案是 "the em**ai**l address you want to use"，标题里还带着客户邮箱，
所以没有匹配到规则、按安全策略停止并存下截图（`artifacts-eori/current-page.json`）。
修正方式：该页补了路径规则 + `default_answer: Yes`，并根据审计日志补上真实路径：

- `/register/vat-group`
- `/register/matching/what-is-your-email`
- `/register/matching/check-your-email`

这几条都写成回归测试（`tests/test_eori_flow.py` 的 `EoriLiveRunRegressionTests`）。

当天第二次运行在点完 `Start now` 后遇到 `ERR_CONNECTION_CLOSED`（Chrome 错误页
`chrome-error://chromewebdata/`），一次网络抖动就中断了整个申请，而且报错文案是
"找不到安全的继续按钮"，容易误判成配置问题。已按此改成：

- 识别 Chrome 错误页（URL 前缀 + 中英文标题），退回上一个正常页面自动重试 2 次，
  整个任务最多恢复 3 次；仍失败才停止，原因为 `network-error`，文案明确提示检查网络。
- 网页上填过的 HMRC 登录信息记在服务端进程内存里（重启即失效，可手动清除），
  失败重试不用再输密码；密码框不再强制必填，只在没记住时校验。

## 7. 已知限制（需要在真实环境复核后再改）

- 截图里看不到 URL 的页面（邮箱通知、VAT 证书信息、VAT 注册日期、联系方式、地址确认）
  按页面标题匹配；真实文案若有出入，程序会停在那一页并留下截图和 `current-page.json`，
  按截图补 `heading_contains` 即可。
- SIC 固定 47910、是否 VAT group 固定 `No`、是否同意公开名称地址固定 `No`，都是当前
  海外电商客户的业务模型；换业务类型前需要确认。
- 真实环境只走到邮箱确认页（见上），邮箱之后的页面还没有实测记录；单元测试 193 个、
  覆盖率检查、`scripts/eori_smoke.py`（本地模拟页面走通 16 个填表页并停在最终核对）
  都通过，但真实 HMRC 全流程尚未提交过完整申请。

## 8. 工作台接入

工作台按插件登记业务菜单（`workbench/plugins/eori.py`，`task_kind` 为 `uk_eori`），
每个业务配一条独立的自动化通道，流程配置只在服务端启动参数里指定：

| 菜单 | 插件 | 流程配置 | 身份证明 |
| --- | --- | --- | --- |
| 英国 VAT 注册 | `uk-vat-register` | `--uk-vat-config`（默认 `vat-config.flow.json`） | 必须 3 份 |
| 英国 EORI 注册 | `uk-eori-register` | `--uk-eori-config`（默认 `vat-config.eori.flow.json`） | 不需要 |

工作台里 EORI 任务的流程与 VAT 完全一致：勾选客户资料 → 启动 → 交验证码 →
到达 `Check your answers` 时网页显示核对内容并可下载整页 PDF → 只有点「确认并提交到 HMRC」
才会真正提交；远程改值（Change 项）也复用同一套。
