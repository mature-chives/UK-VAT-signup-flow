# UK VAT registration automation

这是一个基于 Python + Playwright 的英国 VAT 注册流程驱动器。当前目录中的 178 张截图用于确认流程入口、GOV.UK 页面结构以及主要交互类型；程序运行时按页面真实标签匹配配置，不依赖易变化的 CSS class。

开发入口见 [AGENTS.md](AGENTS.md)。当前支持 VAT / EORI；公司关联与申请隔离见 [客户隔离说明](docs/customer-isolation.md)，可选自动验证能力见 [邮箱池](docs/mail-pool.md) 和 [Authenticator](docs/authenticator.md)。[历史交接与多国家改进计划](docs/kimi-handoff.md) 保留尚待处理的国家和地址问题，不能作为当前全部功能的概述。

## 安全边界

- Government Gateway 凭据从任务表单、客户私有存储或环境默认值读取。默认验证码由人工输入；vat-web 可选邮箱池及 Authenticator 自动处理，最终申请提交仍必须人工确认。
- 遇到缺少的必填字段、未知按钮或配置为 `stop` 的页面时立即停止，并保存截图与页面元数据。
- 初始诚信声明页会自动点击 `Accept and continue`；若检测到明显测试资料会直接停止。
- 到达 `Check your answers` 时保存整页复核文件并暂停；只有网页用户明确勾选确认后，程序才会点击 `Confirm and submit`。
- 测试配置默认设置 `allow_live_application: false`，进入真实 VAT 申请数据区前停止，防止把随机资料写入 HMRC。
- 本轮登录信息可在服务端内存中复用，按客户保存的凭据可跨重启保留；页面可清除对应的普通 HMRC 凭据。密码不写入流程配置、状态接口或审计日志，清除普通凭据不会删除 Authenticator 密钥。
- HMRC 登录信息分两层存：所有客户共用的固定值（例如邮箱和密码）放项目根目录 `.env`；每个客户各自的账号存 `workbench-data/credentials.json`（工作台）或 `hmrc-credentials.json`（vat-web，按登录用户身份＋随机客户 ID 隔离）。vat-web 不再以项目编号作为凭据键，也不会用全局 `HMRC_USER_ID` 代替客户账号；旧凭据保留但不自动认领。密码不返回前端，当前客户的 GG 号可供核对。
- 取值优先级（逐个字段）：任务表单填写 > 该客户本机存储 > 进程内存（同客户上一轮用过的）> `.env` 默认值。换客户不会串号，`.env` 里的固定密码对谁都可用。凭据不进入审计日志、状态接口或任务记录（审计里只留掩码）。
- 浏览器连不上 HMRC（Chrome 报 `ERR_CONNECTION_CLOSED` 之类）时，程序会先退回上一页自动重试 2 次；仍失败才停止并保存 `artifacts*-network-error.png`，不会把网络抖动当成配置问题。
- 出错（缺资料、找不到安全按钮、网络错误、HMRC 业务拒绝页如 "We cannot verify your VAT details"）时默认**不立刻收摊**：程序把 Chrome 和页面留住，最多停留 10 分钟（配置项 `error_hold_seconds`），网页上可以点「继续当前任务」让它从当前页重试，或点「取消任务」结束这一轮；超时自动按取消处理。安全拦截（测试资料不许写入真实申请）仍然立即停止、不停留。
- 浏览器会话保存在本地 `.browser-profile/`，运行记录保存在 `artifacts/`，两者均默认忽略，不应提交到版本库。
- 不要把 Government Gateway 凭据或 MFA 密钥写入流程 JSON。普通登录信息由专用私有凭据存储管理，TOTP 种子由独立加密存储管理；验证码只在内存中短暂使用。

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

有两份配置，不要混用：

- `vat-config.flow.json`：Web 用的流程表。只描述每一页读资料袋的哪个字段（`doc:`）或开户凭据（`env:`），不含客户姓名、邮箱、地址等真值。
- `vat-config.example.json` / `vat-register`：命令行用的字面量配置。复制后把 `REPLACE_ME` 换成真实资料；测试假客户用 `scripts/generate_test_config.py`。

```bash
cp vat-config.example.json vat-config.json
```

`answers` 是跨页面的标签到答案映射；`pages` 可按 URL 路径或页面标题提供覆盖值。推荐优先使用截图或实际页面上完整的英文标签作为 key。单选题的值必须等于页面显示的选项文本，文件上传必须使用绝对路径。流程表里的 `doc:first_name`、`doc:vat_contact_email|env:HMRC_EMAIL` 在填表时才解析；`|` 表示从左到右，空值跳过。

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

多个国际地址页面使用同一地址时，推荐在顶层配置结构化 `address`。程序会把地址分配到 HMRC 国际地址表的 5 个地址栏（Address line 1–5），并确保每行不超过 35 个字符：

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

生成结果依次填入 Address line 1–4：`Room 601,602,603, Building 3`、`No. 528 Xingqi Road, Donghu Street`、`Linping District, Hangzhou City`、`Zhejiang`。某个页面需要不同地址时，可在对应的 `pages[]` 中使用同样的 `address` 对象覆盖。地址无法在不截断的情况下放入 5 行时，程序会在打开浏览器前报错。

同一标签在不同页面含义不同时，应放入 `pages[].answers`，页面配置会覆盖全局配置。`action` 支持：

- `continue`：填写完毕后点击 `Save and continue` 或 `Continue`，默认值。
- `stop`：填完当前页面后停止，适合文件上传或需要人工判断的步骤。

日期值 `date:uk-next-month-first.day`、`.month`、`.year` 会在每次启动时按英国 `Europe/London` 当地日期解析为下个月 1 日。12 月运行时会自动跨年到下一年 1 月 1 日。
`vat-return-stagger:uk-next-month-first` 会使用同一个注册日期月份，自动选择对应的季度 VAT Returns 申报组。

只有改用经过核实的真实申请资料后，才可显式设置 `allow_live_application: true`。启用后，初始诚信声明页会自动点击 `Accept and continue`；若配置或环境变量里仍有 `example.com`、`TEST-`、`07700 900xxx`、`Synthetic`、`Northstar`、`Tester` 等测试标记，程序会拒绝点击声明按钮。最终复核会强制暂停并等待网页用户明确确认，未确认时不会提交。

## 国家、海外税号与旧资料

公司注册地、公司地址国家、个人居住国家和短信验证手机号国家分别保存，不相互兜底。地址缺少国家时留空，必须人工补充；解析仅从明确的地址末尾国家片段提取常见国家，不从注册地推断。国家字段支持中国、香港、爱尔兰、英国的常见中英文名称及代码，其余国家请填写 HMRC 对应英文名称。

本项目已确认的业务规则：**VAT 海外税号直接填写公司注册号**（`company_registration_number`），有公司注册号时自动选择 Yes；不增加独立税号输入或“有／没有／未确认”选项。税号国家读取公司注册国家／地区（`company_registration_country`），不固定为 China。请核对公司注册号及注册地即可；缺少号码时不自动编造或选择 No。

vat-web 及工作台共用启动校验，vat-web 暂停后继续也重新校验。工作台在客户常用字段中修改并保存；清空字段会清除旧值，并保留未在表单展示的其他资料。已移除新增的短信手机号国家输入及启动必填校验。vat-web 新建账号勾选“自动管理 Authenticator”后无需短信手机号；短信备用流程保留原有网页设置（China / 非英国号码）。

旧资料不自动迁移或补号，请重新核对地址国家、公司注册号及注册地。已生成的自定义流程配置不会自动改写；若仍含固定国家，需按新流程表手动调整。此轮改动按用户要求未运行测试、检查或真实 HMRC 联调；国际地址拆分、邮编规则和不同公司类型分支仍待后续完善，不能据此宣称全面支持所有国家。

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

登录凭据和可选的 MFA 设置通过环境变量提供（写进项目根目录 `.env` 效果相同，网页端还会自动读取 `.env` 作为默认值）：

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

## 英国 EORI 注册流程

EORI 注册是与 VAT 分开的一套流程（`vat_automation/eori_flow.py`）：同样由本机 Chrome 驱动
HMRC 的 `customs-registration-services/eori-only` 服务，入口是
`https://www.gov.uk/eori/apply-for-eori`，页面更少、不需要上传身份证明，地址栏只有两行街道地址。
客户真值一律走 `doc:`／`env:` 占位符，流程表为 `vat-config.eori.flow.json`。

```bash
# 改动流程定义后重新生成配置
.venv/bin/python scripts/generate_eori_config.py

# 单用户 Web UI：同一端口内选择 VAT 或 EORI
.venv/bin/vat-web

# 纯虚构资料的本地冒烟配置，进入 EORI 数据区前会停止
.venv/bin/python scripts/generate_eori_config.py --test
vat-register --config vat-config.eori.test.json

# 离线冒烟：本地模拟页面走通整条流程，不访问 HMRC
.venv/bin/python scripts/eori_smoke.py
```

流程页面清单、资料袋字段和安全边界见 [EORI 注册流程说明](docs/eori-registration-flow.md)。
单用户页面和工作台（`vat-bench`）均支持 VAT、EORI 业务选择，无需为两项注册分别启动端口。

## 销售交付工作台

业务菜单由插件登记，不写死种类。新增一种业务=增加一个插件模块并 `register()`。销售与交付暂用同一套菜单；客户资料可在多个任务间勾选复用。注册类业务的 Chrome 仍在服务器本机运行，同事只看状态、交验证码、核对最终 PDF。

目前已登记两个注册业务，各自绑定一份流程配置，互不影响：

| 菜单 | 插件 | 流程配置 |
| --- | --- | --- |
| 英国 VAT 注册 | `uk-vat-register` | `--uk-vat-config`（默认 `vat-config.flow.json`），需要 3 份身份证明 |
| 英国 EORI 注册 | `uk-eori-register` | `--uk-eori-config`（默认 `vat-config.eori.flow.json`），不需要身份证明 |

登录方式优先级：网页/环境变量 `HMRC_SIGN_IN_METHOD` > 流程配置的 `default_sign_in_method` > `Create new sign in details`。默认都是新建账号（程序自动建 Government Gateway，需要邮箱、手机、密码）；**只有客户已有 Government Gateway 账号时**才在网页上把登录方式改成 `Government Gateway`，并填 Gateway User ID + 密码（工作台在自动化操作区也有该输入框）。

```bash
.venv/bin/vat-bench --uk-vat-config vat-config.flow.json \
  --uk-eori-config vat-config.eori.flow.json
```

两个业务都会走到最终核对页暂停：网页展示核对内容、可下载整页 PDF、可按字段远程改值，只有点「确认并提交到 HMRC」才会提交。

证件识别需要已安装 `paddleocr` 的 Python（可选 extra：`pip install -e '.[ocr]'`）。百度翻译密钥放项目根目录 `.env`（已忽略，模板见 `.env.example`），启动时自动读入。

```bash
cp .env.example .env
# 填 TRANSLATE_APP_ID / TRANSLATE_API_KEY
.venv/bin/python -m pip install -e .
.venv/bin/vat-bench --uk-vat-config vat-config.flow.json
```

默认 [http://127.0.0.1:8770](http://127.0.0.1:8770)。内置插件：英国 VAT 注册（接入现有自动化）、翻译证件照（本机识别后生成两页英文 PDF）、翻译营业执照 / POA（回传译文）、沙特 VAT 注册（占位）。数据在 `workbench-data/`，默认不入库。不要把密钥写入 JSON 或提交 `.env`。

## 本地 Web UI

安装依赖后直接启动：

```bash
.venv/bin/python -m pip install -e .
.venv/bin/vat-web --config vat-config.flow.json
```

还没有账号时，终端会打印一个仅本次启动有效的初始化码，浏览器打开后会自动进入“创建管理员账号”页面，输入初始化码即可完成初始化。管理员登录后可展开页面底部“账号管理”区块为同事添加账号、重置密码或删除账号（首个账号自动是管理员，最后一个管理员不可删除）。命令行方式 `vat-web-user add/remove/list` 仍然可用。

浏览器会自动打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，先登录再使用。页面支持：

- 通过页面顶部「英国 VAT 注册」「英国 EORI 注册」两个业务菜单进入对应页面，两项业务共用账号登录和端口；
- 无需“我的客户”或手动建档，进入业务页面直接上传资料。确认资料时按内地统一社会信用代码（18 位、校验位检查）或香港 BRN（明确类型、8 位）自动关联公司；缺号/类型不明确/格式错误仍可独立申请，同号但名称或成立日期冲突时需核对或明确选择独立申请，不自动覆盖；
- 已确认的公司基础信息（名称、注册地、编号及成立日期）可供登录用户检索，GG 凭据、证件、个人资料和完整办理记录仍按用户隔离；同一用户、同一公司的 VAT/EORI 可复用自己的凭据。共享编号匹配不等于获准使用他人账号；
- 页面分为“准备资料”和“运行与核对”两个阶段：启动成功后自动进入运行工作区并定位到状态区，验证码、录制连接和最终复核集中展示；启动失败则保留准备表单和错误提示；
- 普通用户使用“上传客户资料 → 英国税务账号 → 开始办理”的简化页面；技术配置、浏览器会话选项、邮箱池检查统计和当前页面地址收进管理员折叠区域，普通用户通过页面启动时默认使用全新会话。VAT 的固定营业额、行业分类和季度申报说明仍可在“申报设置”展开核对，不符合实际情况请勿提交；
- 登录区域按方式切换：GG 登录填写已有 User ID 和密码；新建账号填写开户邮箱、姓名、密码和验证手机号。EORI 通知邮箱独立展示并要求核对，留空不会自动使用登录邮箱；
- 上传 PDF、DOCX、XLSX、TXT、JSON、CSV 或 TSV 资料文档，在本地提取 VAT 字段；
- 可直接上传 `materials/` 同款“英国VAT注册信息采集表” `.xlsx`：识别合并分组下的法人英文名、公司/居住地址分项、VAT 号及注册日期，Excel 日期自动转为 `YYYY-MM-DD`；邮箱密码等非注册资料不导入。普通两列 Excel 仍支持，旧版 `.xls` 需先另存为 `.xlsx`；
- 香港公司地址会保留地址组首行的楼宇信息，并把 `HONG KONG, CHINA` 统一为 `Hong Kong`。该模板“公司中文名称”行 B 列的 12 位 GG 号在解析后自动填入单用户登录框，并切换为 Government Gateway 登录，完整显示供核对；运行中的任务不更改登录账号。GG 号不混入客户资料或日志，密码仍需单独提供并保持隐藏；
- 只返回英国 VAT 注册会使用的字段，忽略签证、前任会计师、平台链接等无关内容；
- 在页面检查和修正提取结果，确认后作为资料袋交给自动化（不写回 JSON）；
- 输入开户邮箱、密码和 MFA 手机号；
- VAT 一次上传三份身份证明，后续按 HMRC 页面顺序自动上传；EORI 不需要身份证明，改为核对已有 VAT 号、VAT 生效日期等资料；
- EORI 项目编号仅用于本地管理，选填，不影响资料确认或启动；系统自动生成客户 ID，编号重复或留空都不会串凭据。EORI 页面不显示无关的居住地址国家和个人邮箱缺失提示，VAT 页面保持原有校验；
- 新建账号建议使用“自动管理 Authenticator”，勾选后不要求短信手机号或国家；短信备用流程沿用原有 China / 非英国号码设置，不增加国家填写步骤。申请资料中的联系手机号仍按业务要求填写；
- VAT 固定使用标准税率营业额 `10000`、降低税率 `0`、零税率 `0`；两个流程均使用 SIC `47910`；
- 启动可见的本地 Chrome 自动化；
- 在网页中输入邮箱验证码和短信验证码；
- 随时点击运行工作区顶部吸顶工具条的“停止本次注册”：等待验证码、暂停、最终核对或页面卡住时均可结束当前任务，关闭本次自动化浏览器后释放占用，无需重启服务；
- 仅管理员可在“管理员设置”勾选“允许录制本次浏览器操作”，连接 `browser-action-recorder` 记录本次 HMRC 标签页；默认关闭，仅允许本机连接；普通用户不显示此选项，后端也拒绝其开启录制或继续录制任务的请求；
- 在安全页面边界暂停自动化，修改并重新确认资料后从当前 HMRC 页面继续；
- 到达 `Check your answers` 时先点击 `Show all sections` 展开全部答案，再保存整页 PDF（失败时保存整页截图）供下载核对；如需修改，网页会列出 HMRC 的全部 `Change` 项，并把所选修改页的字段、当前值、选项和校验错误同步到远程网页；保存后经申请进度页回到最终核对并重新存档，不继续走整份注册；逐项核对并明确勾选确认后，程序点击 `Confirm and submit`；
- 查看运行状态和中文操作摘要；管理员可展开当前 HMRC 页面、原始事件和诊断信息。常见技术错误使用中文操作提示，后台原始日志及状态接口保持不变；官方最终核对内容不做自动翻译或改写；
- “最近操作”默认显示最近 5 条，可展开全部本次任务记录：重新登录会清空已结束任务的展示记录，运行中的任务保留；新任务从空列表开始，不从历史审计回填。后台审计日志仍保留；
- 显示安全停止或失败原因。

Web UI 默认只绑定 `127.0.0.1`，VAT / EORI 合计一次只运行一个任务。默认加载 `vat-config.flow.json` 及同目录的 `vat-config.eori.flow.json`，自定义 EORI 配置可用 `--eori-config PATH`。原有 `--config PATH` 仍指定默认进入的流程（也兼容直接指定 EORI 配置）。网页只能选择服务端已配置的业务，无法指定文件路径。

两个流程的运行状态、上传文件、验证码和最终复核分别保存，同一客户已保存的 HMRC 凭据可以复用。切换客户或业务会清空本页未保存输入，但不会终止正在运行的任务，切回即可继续处理。文档提取不调用外部 AI 或网络 API；身份证明按用户、客户及流程隔离，以 `0600` 文件权限保存在 `0700` 本地临时目录，进程正常退出时删除，重启后需重新上传。密码和验证码不会写入资料初稿、状态接口或审计日志，密码提交成功后会从页面输入框清空。使用 `Ctrl+C` 关闭本地服务。

暂停不会关闭 Chrome 或丢失 HMRC 登录会话。点击“应用修改并继续”后，程序将新资料合并到当前任务并重新处理当前页面。暂停和最终核对的等待都有 30 分钟上限，超时后自动结束并关闭浏览器。文档中形如 `8613592850576(国际区号+电话号)` 的电话号码会清洗为纯数字 `8613592850576`，不自动补 `+`。

运行时，本页导入资料折叠为只读；点击“暂停并修改资料”，安全暂停后自动展开编辑区，重新确认再继续，运行中不能改换公司。继续后自动收起资料并回到运行状态。任务结束可“重新办理本公司”或“办理另一家公司”，再次启动前须重新核对资料和登录账号。点击“确认资料无误”时自动建立申请归属并保存初稿，刷新后可恢复但必须重新确认；尚无本业务资料时可从本人同公司另一业务恢复初稿。未保存输入不会恢复。重新上传或修改公司身份字段会清除旧账号输入，避免串用。

公司基础索引、私有资料和每次办理记录保存在主配置同目录的 `vat-web-data/`（目录 `0700`、JSON `0600`，Git 忽略），凭据仍单独保存。每次启动生成新的办理记录及产物目录；非全新 Chrome 配置也按用户、公司、业务及 GG 账号分别保存。页面“我保存的申请（继续办理）”可恢复本人的申请，“本公司 · 我的办理记录”可下载已有复核或提交后回执。结果分为“已取得 EORI”“HMRC 已接收申请，等待处理”“结果待核实”；自动化正常结束不等于注册获批，无法明确识别的回执一律待核实。服务重启后未结束的记录显示“服务中断”，不会自动重提。

升级需要在当前任务结束后重启 `vat-web`。旧按用户名/项目编号保存的凭据和旧产物不删除、不自动迁移归属；首次为客户重新核对并保存 GG 信息即可。更改 GG 号须填写对应密码，不能混用旧账号密码。新操作接口需要 `customer_id`，运行控制另校验 `X-VAT-Run-ID`，旧标签页请刷新；命令行及 `vat-bench` 不受影响。需求与边界见 [客户隔离说明](docs/customer-isolation.md)。

“停止本次注册”会显示“正在停止”，待浏览器清理完成后才允许重新开始。停止会清空本次待处理的验证码和修改指令，保留已保存的账号凭据、上传资料及已有复核文件。它不会撤销 HMRC 已保存的资料或已发出的提交请求；若提交时停止，应先核实 HMRC 的申请结果，避免重复申请。关闭网页、退出登录或切换业务菜单仍不会停止后台任务。

录制连接适用于 VAT 和 EORI：勾选后使用系统临时目录中的全新 Chrome 配置，并由 Chrome 自动分配空闲调试端口，避免与 `9222` 等已有端口冲突。启动后先停在空白页，运行工作区自动展开本机连接地址和本次申请标签页 ID；让录制技能在服务所在电脑上先列出标签页、选定该 ID 并开始录制，再点击“开始注册流程”。继续后连接信息自动折叠，仍可手动展开查看。只开启连接不会自动录制，也不能给已经启动的任务补开连接。等待连接最长 30 分钟，期间也可以停止任务。

调试连接只监听回环地址，不经 Web 服务代理或暴露到局域网；普通用户的状态接口不返回连接地址或标签页 ID，管理员也只能查看本人任务的连接。任务完成、失败或停止后，浏览器关闭，连接信息随之清除。`browser-action-recorder` 只记录操作元数据，不记录输入值、密码或验证码；页面标题仍可能含客户信息，录制文件应留在本机私有临时目录，不提交到 Git。

资料文件名应包含项目编号，例如 `客户信息及服务授权表-新注册VAT-AB223322公司名.docx`。程序会结合文档中的公司英文名生成 HMRC 申请参考名称：`AB223322-UK-公司英文名`。

指定其他端口或不自动打开浏览器：

```bash
.venv/bin/vat-web --config vat-config.flow.json --port 9000 --no-open
```

## 局域网多人使用

同事的电脑只需要浏览器，不需要安装 Playwright；自动化的 Chrome 始终在服务器这台机器上运行。同事在网页上看到的是状态文字、验证码输入框和最终核对的 PDF，不是 HMRC 页面本身。

1. 以局域网地址启动。程序会自动生成自签 TLS 证书（`certs/`，SAN 包含本机局域网 IP 和 `.local` 主机名），并打印同事可访问的地址：

   ```bash
   .venv/bin/vat-web --config vat-config.flow.json --host 0.0.0.0
   ```

   无法准备证书时会拒绝启动。也可用 `--ssl-certfile` / `--ssl-keyfile` 指定自备证书（例如 mkcert 签发）。`--insecure-http` 可跳过 TLS，但登录密码、HMRC 凭据、验证码和客户身份证件都会明文过网，不要在正式使用时开启。

2. 首次使用：在自己浏览器打开页面，按终端打印的初始化码创建管理员账号，然后在“账号管理”区块为每位同事添加账号（密码至少 12 个字符）。

3. 同事首次访问 `https://<服务器IP>:8765` 会看到自签证书警告，手动选择继续访问一次即可；之后登录自己的账号使用。

安全边界与限制：

- 所有 API 都要求登录；登录失败连续 5 次会按 IP 和用户名锁定 5 分钟起（指数退避）。会话有效期 8 小时，重启服务后所有人需重新登录。
- 所有写操作都有 CSRF 防护（SameSite Cookie + 来源校验 + 请求头令牌）。
- 每位同事的提取资料、身份证明文件、运行状态、最终核对文档互相隔离；审计日志记录操作人。
- vat-web 一次只运行一个客户的申请。他人任务运行时只显示资源忙，不公开申请所属人或客户姓名，需等待任务结束。
- HMRC 凭据只通过内存传给自动化进程，不写入进程环境变量，不会被浏览器子进程继承。
- **正式提交必须人工确认。** 到达 `Check your answers` 后程序保存复核文件并暂停；只有当前任务用户在网页勾选确认并点击“确认并提交到 HMRC”，程序才会精确点击 `Confirm and submit`。30 分钟内未确认则关闭浏览器且不提交。
- 服务器本机是整个系统的安全边界：`artifacts/` 内有含客户个人信息的截图和 PDF，请勿共享该机器或让其休眠导致会话中断。

## 验证

SkyMail 独立账号的邮箱池导入、连接检查及 GG 建号自动收码配置见 [邮箱池说明](docs/mail-pool.md)。默认关闭；已有账号登录及最终人工复核保持不变。

启用邮箱池的 VAT 新 GG 任务会复用同一邮箱作为申请的个人电子邮箱，并自动处理该处首次邮箱验证码；公司电子邮箱不变。GG 和 VAT 邮件按用途独立匹配，异常或重复发码时转人工输入。

`vat-web` 登录区可勾选「自动管理 Authenticator」，自动绑定新 GG 的验证器并为已托管账号生成动态码；不再要求开户短信手机号。启用时与录制互斥，仍保留最终人工复核。存储、备份及中断限制见 [Authenticator 说明](docs/authenticator.md)。

首次绑定时操作页会临时展示二维码，供用户扫码添加到手机 Authenticator；不增加确认或手机验码步骤，不暂停自动化。二维码仅在当前任务内展示最多 5 分钟，不写入运行记录。

```bash
python -m compileall -q vat_automation tests
python -m unittest discover -s tests -v
python scripts/check_flow_coverage.py vat-config.flow.json
python scripts/check_flow_coverage.py vat-config.test.json
python scripts/check_flow_coverage.py --flow eori
```

覆盖检查基于截图整理出的 84 个逻辑页面；“covered”表示已有专用规则、通用字段映射或特殊页面处理，不代表每个分支都已在真实 HMRC 环境提交验证。
EORI 流程同理：`--flow eori` 检查 `materials/` 里的 17 个 EORI 截图页面（10 个路径页 + 7 个标题页）都有规则映射。
