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
| image4 | HMRC 登录方式（Government Gateway／One Login／Create new sign in details） | `HMRC_SIGN_IN_METHOD` |
| image5 | Government Gateway 短信验证码 | 人工在网页输入 |
| image3 | Is your organisation part of a VAT group in the UK? | `No` |
| image6 | What email address can we use for customs notifications? | `doc:vat_contact_email` 或 `env:HMRC_EMAIL` |
| image7 | Is … the mail address you want to use? | `Yes` |
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

# 单用户 Web UI（资料袋解析 + 验证码 + 最终核对）
.venv/bin/vat-web --config vat-config.eori.flow.json

# 命令行：先生成纯虚构的冒烟配置，不会写进真实申请
.venv/bin/python scripts/generate_eori_config.py --test
.venv/bin/vat-register --config vat-config.eori.test.json

# 截图页面覆盖率检查
.venv/bin/python scripts/check_flow_coverage.py --flow eori

# 离线冒烟：本地起模拟页面，用真实 runner 跑完整条流程（不访问 HMRC）
.venv/bin/python scripts/eori_smoke.py
```

需要的登录环境变量与 VAT 流程相同：`HMRC_USER_ID`、`HMRC_PASSWORD`、
`HMRC_EMAIL`、`HMRC_MFA_PHONE`；`HMRC_SIGN_IN_METHOD` 缺省
`Create new sign in details`，`HMRC_MFA_PHONE_COUNTRY` 缺省取资料里的 `country`。

## 5. 安全边界

- 进入 EORI 数据区（`/customs-registration-services/eori-only/`）前，若配置是
  `allow_live_application: false`，程序立即停止并保存截图，虚构资料不会写进真实申请。
- 到达 `review-details` 时保存整页 PDF／截图并暂停，只有网页用户明确确认后才点提交；
  暂停与复核都有 30 分钟超时。
- 提交按钮按 `Confirm and submit` → `Accept and submit` → `Submit` 依次尝试，
  都找不到就停止，不会误点别的按钮。
- 地址宁可报错也不截断：两行街道地址放不下时抛错，要求人工改地址。

## 6. 已知限制（需要在真实环境复核后再改）

- 截图里看不到 URL 的页面（邮箱通知、VAT 证书信息、VAT 注册日期、联系方式、地址确认）
  按页面标题匹配；真实文案若有出入，程序会停在那一页并留下截图和 `current-page.json`，
  按截图补 `heading_contains` 即可。
- SIC 固定 47910、是否 VAT group 固定 `No`、是否同意公开名称地址固定 `No`，都是当前
  海外电商客户的业务模型；换业务类型前需要确认。
- 尚未接入销售交付工作台（`vat-bench`）：工作台目前只登记 VAT 插件，EORI 走
  `vat-web` 或命令行。接入方式见 `workbench/catalog.py` 的插件登记约定。
- 流程只在离线层面验证过（186 个单元测试、覆盖率检查、`scripts/eori_smoke.py`
  本地模拟页面走通 16 个填表页并停在最终核对），没有在真实 HMRC 环境跑完整申请。
