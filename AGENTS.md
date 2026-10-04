# AGENTS.md

本文件面向 AI 编码代理，假设读者对本项目一无所知。更新日期：2026-09-27。本文描述当前工作区实现；功能已实现不代表已通过真实 HMRC 联调。

## 项目概述

这是一个**配置驱动的英国 VAT（增值税）及 EORI 注册流程自动化工具**。用 Python + Playwright 驱动本机 Chrome，在 GOV.UK / HMRC 网站上办理 VAT / EORI 注册申请。核心安全设计：按页面真实标签匹配配置（不依赖易变化的 CSS class），在危险边界（正式提交、未知按钮、缺少必填字段）自动停止并等待人工确认。

项目包含四个可执行入口（`pyproject.toml` 的 `[project.scripts]`）：

| 命令 | 入口 | 用途 |
| --- | --- | --- |
| `vat-register` | `vat_automation.cli:main` | 命令行单次运行 HMRC 注册流程 |
| `vat-web` | `vat_automation.web:main` | 单用户本地 Web UI（默认 `127.0.0.1:8765`） |
| `vat-web-user` | `vat_automation.auth:main` | Web UI 账号管理（add/remove/list） |
| `vat-bench` | `workbench.app:main` | 销售/交付工作台（多人共用，默认 `127.0.0.1:8770`） |

## 技术栈

- Python ≥ 3.11，setuptools 打包（`pyproject.toml`），`uv.lock` 存在（可用 uv 管理依赖），`.venv/` 为本地虚拟环境。
- 浏览器自动化：Playwright（`browser_channel: "chrome"` 默认用系统 Chrome，无需下载 Chromium）。
- Web 服务：FastAPI + uvicorn + pydantic + python-multipart。
- 文档处理：pypdf、pillow、pypinyin；可选 extra `pip install -e '.[ocr]'` 安装 paddleocr（证件识别用）。
- 存储：工作台使用 `workbench-data/` 下的 JSON；vat-web 公司索引、私有初稿和办理记录使用 `vat-web-data/` 下的 JSON；邮箱池与 Authenticator 各自使用本机 SQLite。Authenticator 种子使用 cryptography 的 AES-GCM 加密，二维码用 zxing-cpp 本地解码。
- 项目的注释、文档字符串、用户文档（README.md）均以中文为主，修改代码时应保持一致。

## 目录结构与模块划分

```
vat_automation/        # 核心自动化包
  runner.py            # VatAutomation：Playwright 主循环、安全动作白名单、
                       # 暂停/复核/远程编辑回调、审计日志、提交回执判定（项目核心）
  config.py            # Settings/PageRule 配置模型、地址拆分（35 字符上限，
                       # 不截断）、doc:/env: 占位符、动态日期、测试标记检测
  cli.py               # vat-register 命令行入口
  web.py               # 单用户本地 Web UI（FastAPI，会话/CSRF/任务管理）
  auth.py              # scrypt 密码散列、会话签名、登录限流（用户名管理）
  document_parser.py   # 从 PDF/DOCX/XLSX/TXT 等本地提取 VAT 字段
  document_translation.py # 解析后按需调用 DeepSeek 翻译中文地址和业务描述
  customer_store.py    # 公共公司索引、用户私有申请空间及办理记录
  company_identity.py  # 公司身份归一化、USCC 校验和香港 BRN 识别
  countries.py         # 国家字段归一化及明确地址国家片段识别
  credential_store.py  # 本机私有 HMRC 凭据存储
  mail_pool.py          # 独立邮箱分配、租约与本机 SQLite 状态
  mail_verification.py  # 验证邮件匹配和轮询；skymail.py 为邮箱接口客户端
  authenticator.py      # Authenticator 页面流程及动态码处理
  authenticator_store.py # 加密种子存储与 pending/active 状态
  eori_flow.py          # EORI 流程规则
  envfile.py            # 读取项目根目录 .env
  screenshot_flow.py   # 由 HMRC 截图整理出的 84 个逻辑页面流程表（flow 配置生成器）
  tls.py               # 自签 TLS 证书生成（SAN 含局域网 IP），局域网模式必需
  static/              # Web UI 页面（index.html / login.html / setup.html）
workbench/             # 销售交付工作台（多人，插件化）
  app.py               # FastAPI 主应用，复用 web.py 的 JobManager
  catalog.py           # 插件注册表：新增业务 = 新增插件模块并 register()，勿改菜单结构
  plugins/             # uk_vat.py（英国 VAT）、eori.py（英国 EORI）、translate.py（证件翻译）、sa_vat.py（占位）
  store.py             # workbench-data/ JSON 存储（0600 权限写文件）
  id_card*.py          # 身份证 OCR（paddleocr）、字段翻译（百度 API）、PDF 渲染
scripts/               # 开发辅助脚本（非入口）
  generate_test_config.py   # 生成纯虚构测试客户配置 vat-config.test.json
  check_flow_coverage.py    # 检查流程配置对 84 个截图页面的覆盖率
  build_flow_inventory.py   # 从截图构建流程清单（输出 artifacts-flow/）
  browser_smoke.py          # Playwright 冒烟测试（headless）
  generate_eori_config.py   # 生成 EORI 流程配置及虚构测试配置
  eori_smoke.py             # EORI 本地模拟页面冒烟
tests/                 # unittest 测试，见「测试」节
docs/                  # 身份证翻译双机架构设计文档
artifacts*/            # 运行产物：截图、PDF、audit.jsonl、current-page.json（git 忽略）
```

## 构建、安装与运行

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium   # 备用；默认直接用系统 Chrome
```

运行方式：

```bash
vat-register --config vat-config.json             # 命令行
vat-register --config vat-config.json --resume    # 复用登录会话和最后 URL
vat-register --config vat-config.test.json --fresh-session   # 全新临时浏览器配置（不可与 --resume 同用）
.venv/bin/vat-web --config vat-config.flow.json   # 单用户 Web UI（:8765）
.venv/bin/vat-bench --uk-vat-config vat-config.flow.json     # 工作台（:8770）
```

## 配置体系（重要，勿混用）

有两类配置，用途不同：

1. **`vat-config.flow.json`**：Web 用「流程表」。EORI 对应 `vat-config.eori.flow.json`，定义来源为 `eori_flow.py`。只描述每页从资料袋读哪个字段（`doc:` 前缀）或环境变量（`env:` 前缀），不含真实客户值。`doc:first_name`、`doc:vat_contact_email|env:HMRC_EMAIL` 在填表时才解析，`|` 表示从左到右、空值跳过。
2. **`vat-config.example.json` → `vat-config.json`**：命令行用字面量配置，`REPLACE_ME` 换成真实值；测试假客户用 `scripts/generate_test_config.py` 生成 `vat-config.test.json`。

配置语义（`config.py` 为准）：

- `answers` 是全局「标签 → 答案」映射；`pages[]` 按 `match.path_contains` / `match.heading_contains` 覆盖全局，**后写的页面规则优先**；单选值必须等于页面选项原文；文件上传用绝对路径。
- `action`：`continue`（默认）或 `stop`（填完即停，适合文件上传/人工判断）。
- 顶层或页面级 `address` 结构化地址与 `doc:home`/`doc:business` 在 VAT 中统一展开为 HMRC 国际地址表的 **5 行**（Address line 1–5，另加 `Postcode`/`Country`）；每行 **35 字符上限，宁可报错也不截断**、不缩写。
- EORI 街道地址只有两行，不能直接套用 VAT 五行地址规则；细节见 `docs/eori-registration-flow.md`。
- 动态值：`date:uk-next-month-first.day/.month/.year` 按英国 `Europe/London` 日期解析为下月 1 日；`vat-return-stagger:uk-next-month-first` 选对应季度申报组。
- `FIXED_VAT_PAGE_RULES`（config.py 内硬编码）：营业额固定填 10000/0/0（标准/降低/零税率），SIC 搜索 47910，追加在最后防止配置覆盖——这是当前业务模型的固定规则，改动前需确认业务含义。
- `allow_live_application: false`（默认）会在进入真实申请数据区前停止；置 `true` 前必须清除所有测试标记（`example.com`、`TEST-`、`Synthetic`、`Northstar`、`Tester`、保留手机号 `07700 900xxx`），否则程序拒绝继续。

## 测试与验证

测试用标准库 **unittest**（`unittest.TestCase` 风格，无 pytest 依赖，虽然目录里有 `.pytest_cache`）。以下为按需执行的命令；用户明确要求不测试或不检查时不执行：

```bash
python -m compileall -q vat_automation tests workbench scripts   # 语法检查
python -m unittest discover -s tests -v                          # 运行当前测试集
python scripts/check_flow_coverage.py vat-config.flow.json       # 84 个截图页面覆盖检查
python scripts/check_flow_coverage.py vat-config.test.json
```

2026-09-27 国家/税号改造之前的运行记录（不代表改造后的测试状态）：242 项测试，241 通过、1 失败；`test_auth_resume_state_keeps_current_url` 仍要求恢复原认证 URL，而 `_save_state()` 已改为保存起始 URL，预期尚待统一。同次 VAT 覆盖记录为 84/84。本次文档更新未重跑测试或检查。

注意：覆盖检查的 "covered" 只表示有规则映射，不代表在真实 HMRC 环境提交验证过。

## 代码风格约定

- 注释/文档字符串用中文；代码标识符用英文。
- 页面匹配一律走 `normalize()`（大小写、标点、空白归一化）后的标签文本，**禁止按 CSS class 选择器绑定 HMRC 页面**。
- 类型标注齐全（`from __future__ import annotations`，`str | None` 等新语法），数据类用 `@dataclass(slots=True)`。
- 行尾统一 LF（`.gitattributes`：`text=auto eol=lf`）。
- 业务菜单由插件登记（`workbench/catalog.py`），新增业务种类 = 新增插件模块调用 `register()`，不改工作台菜单结构。

## 安全边界（修改代码时必须保持）

- **凭据只经环境变量/内存/本机私有存储传入，不写进流程配置和日志**：`credential_store.py` 管理 0600 凭据文件。工作台按客户保存；vat-web 按登录用户与私有公司归属隔离，同用户同公司可跨 VAT/EORI 复用。旧凭据不自动认领，不以项目编号或全局 `HMRC_USER_ID` 代替客户账号。显式传入凭据 dict，避免密码被 Chrome 子进程继承。密码、验证码不进入状态或审计；当前客户 GG 号可在专用界面供核对，历史记录不保存明文 GG 号。
- **人工确认后才提交**：到达 `Check your answers` 保存整页复核 PDF 并暂停；只有网页用户明确勾选确认后才点击 `Confirm and submit`；暂停与复核均有 30 分钟超时。
- 百度翻译及 DeepSeek API 密钥放项目根目录 `.env`（`DEEPSEEK_API_KEY`）；DeepSeek 仅接收中文地址、业务描述及必要国家／邮编，不接收整份文档或登录凭据。邮箱池账号放 `.mail-pool/` 私有存储；Authenticator 主密钥为 `.authenticator-key`，加密种子为 `.authenticator/vault.sqlite3`，不得写进普通凭据或配置。详见对应功能文档。
- 动作白名单 `SAFE_ACTIONS`（runner.py）：只允许 `Save and continue` 等安全按钮；未知按钮/缺失必填字段/`action: stop` 停止自动推进并保存现场。Web 可保留浏览器等待人工继续或取消，默认 10 分钟；测试资料安全拦截仍立即结束。认证页面的截图与 URL 需脱敏，不能保存认证秘密。
- Web 安全：scrypt 密码散列（含用户名不存在的恒时假散列）、登录失败 5 次指数退避锁定、会话 8 小时 TTL（重启即失效）、CSRF（SameSite Cookie + 来源校验 + 请求头令牌）、用户间数据隔离。
- 局域网模式（`--host 0.0.0.0`）自动生成自签证书（`certs/`，git 忽略），无证书拒绝启动；`--insecure-http` 会明文传输密码/证件，仅调试用。
- 身份证件以 0600 权限存 0700 临时目录，进程退出删除；`artifacts/` 内含客户个人信息，不得提交或共享。
- git 忽略：`.browser-profile*/`、`artifacts*/`、`vat-config*.json`（例外：`vat-config.example.json`、`vat-config.flow.json`、`vat-config.eori.flow.json`）、`users.json`、`workbench-data/`、`certs/`、`.env`、`*.png/*.pdf/*.docx` 等二进制。

## 当前功能边界与文档入口

- 2026-09-28 新增 DeepSeek 字段翻译：`deepseek-flash`、`https://api.deepseek.com`，从 `.env` 读取配置；未运行测试或真实 API 调用。原文保留，译文需核对；详见 README 的 DeepSeek 资料翻译说明。

- vat-web 主流程：选择 VAT/EORI → 上传解析 → 核对并确认公司关联 → 开始办理 → 最终人工复核。公共公司索引不授予他人的凭据、证件或申请访问权；全服务合计一次一个自动化任务。
- vat-bench 保持共享客户协作模型；vat-web 的公司隔离、邮箱池和 Authenticator 界面能力不能直接视为工作台已具备。
- 执行结束与业务成功分开：仅明确回执可判为 HMRC 已接收，明确 EORI 分配页及有效编号才判为已取得 EORI；其余结果待核实，服务重启不自动重提。
- [公司关联与申请隔离](docs/customer-isolation.md)、[邮箱池](docs/mail-pool.md)、[Authenticator](docs/authenticator.md) 描述新增能力及私有存储边界。Authenticator 文档明确尚未完成真实 HMRC 绑定/登录验证。
- [历史交接与多国家待办](docs/kimi-handoff.md)：当前已修改地址国家默认值、税号国家固定值；按用户要求未运行测试或检查。地址拆分、邮编和不同国家分支仍待完善。
- 国家解析优先自动提取：公司地址没有国家或明确省市信息时沿用公司注册地；居住地址独立识别国家或明确中国省市，不套用公司注册地。仅上传识别时补齐，人工清空后不重新兜底。流程读取资料结果，不固定 China。
- **已确认业务规则：VAT 海外税号直接填写公司注册号**（`company_registration_number`），有号码即选择 Yes；不拆分税号字段，不增加有／没有／未确认选项。税号国家使用公司注册地（`company_registration_country`），不固定为 China。未经用户要求不要再次改为独立税号模型。两个网页入口启动、vat-web 暂停后继续均调用 `validate_application_values()` 校验地址国家。
- 新建账号以 vat-web 自动管理 Authenticator 为主要使用方式；用户已要求移除新增的短信手机号国家输入和启动必填校验，短信备用方式保留原有设置。不要再次增加国家确认步骤，申请联系手机号与短信验证手机号分别处理。
- [AI 辅助维护待办](docs/ai-assisted-maintenance-todo.md) 仅为计划，未实现运行时 AI 自动操作。

## 部署形态

新增 tCloud 内部试用容器部署：`compose.yaml` 管理本项目的 Web（Chrome + Xvfb）与独立 Nginx 反向代理，默认 HTTPS 16671 端口，使用自签证书；详见 `docs/server-deployment.md`。本次为全新环境，不迁移本地客户、GG、邮箱池或 Authenticator 数据。不运行真实注册流程。仍可在服务器本机以 editable 方式运行 `vat-web` 或 `vat-bench`，两者能力不能混同。Playwright Chrome 始终在服务端运行，同事通过浏览器看状态、输验证码、核对最终 PDF。证件 OCR 双机设计见 `docs/id-card-translation-architecture.md`，目前 OCR 仍在工作台进程内运行。
