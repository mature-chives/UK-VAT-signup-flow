# AGENTS.md

本文件面向 AI 编码代理，假设读者对本项目一无所知。所有信息基于对代码库的实际检查（截至 2026-09-20）。

## 项目概述

这是一个**配置驱动的英国 VAT（增值税）注册流程自动化工具**。用 Python + Playwright 驱动本机 Chrome，在 GOV.UK / HMRC 网站上完成 VAT 注册申请。核心安全设计：按页面真实标签匹配配置（不依赖易变化的 CSS class），在危险边界（正式提交、未知按钮、缺少必填字段）自动停止并等待人工确认。

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
- 无数据库：工作台数据存 `workbench-data/` 下的 JSON 文件。
- 项目的注释、文档字符串、用户文档（README.md）均以中文为主，修改代码时应保持一致。

## 目录结构与模块划分

```
vat_automation/        # 核心自动化包
  runner.py            # VatAutomation：Playwright 主循环、安全动作白名单、
                       # 暂停/复核/远程编辑回调、审计日志（1426 行，项目核心）
  config.py            # Settings/PageRule 配置模型、地址拆分（35 字符上限，
                       # 不截断）、doc:/env: 占位符、动态日期、测试标记检测
  cli.py               # vat-register 命令行入口
  web.py               # 单用户本地 Web UI（FastAPI，会话/CSRF/任务管理）
  auth.py              # scrypt 密码散列、会话签名、登录限流（用户名管理）
  document_parser.py   # 从 PDF/DOCX/XLSX/TXT 等本地提取 VAT 字段（不调用外部 AI/API）
  screenshot_flow.py   # 由 HMRC 截图整理出的 84 个逻辑页面流程表（flow 配置生成器）
  tls.py               # 自签 TLS 证书生成（SAN 含局域网 IP），局域网模式必需
  static/              # Web UI 页面（index.html / login.html / setup.html）
workbench/             # 销售交付工作台（多人，插件化）
  app.py               # FastAPI 主应用（823 行）
  catalog.py           # 插件注册表：新增业务 = 新增插件模块并 register()，勿改菜单结构
  plugins/             # uk_vat.py（英国 VAT）、translate.py（证件翻译）、sa_vat.py（占位）
  store.py             # workbench-data/ JSON 存储（0600 权限写文件）
  id_card*.py          # 身份证 OCR（paddleocr）、字段翻译（百度 API）、PDF 渲染
  envfile.py           # 读取项目根目录 .env（百度翻译密钥）
scripts/               # 开发辅助脚本（非入口）
  generate_test_config.py   # 生成纯虚构测试客户配置 vat-config.test.json
  check_flow_coverage.py    # 检查流程配置对 84 个截图页面的覆盖率
  build_flow_inventory.py   # 从截图构建流程清单（输出 artifacts-flow/）
  browser_smoke.py          # Playwright 冒烟测试（headless）
tests/                 # unittest 测试（165 个），见「测试」节
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

1. **`vat-config.flow.json`**：Web 用「流程表」。只描述每页从资料袋读哪个字段（`doc:` 前缀）或环境变量（`env:` 前缀），不含真实客户值。`doc:first_name`、`doc:vat_contact_email|env:HMRC_EMAIL` 在填表时才解析，`|` 表示从左到右、空值跳过。
2. **`vat-config.example.json` → `vat-config.json`**：命令行用字面量配置，`REPLACE_ME` 换成真实值；测试假客户用 `scripts/generate_test_config.py` 生成 `vat-config.test.json`。

配置语义（`config.py` 为准）：

- `answers` 是全局「标签 → 答案」映射；`pages[]` 按 `match.path_contains` / `match.heading_contains` 覆盖全局，**后写的页面规则优先**；单选值必须等于页面选项原文；文件上传用绝对路径。
- `action`：`continue`（默认）或 `stop`（填完即停，适合文件上传/人工判断）。
- 顶层或页面级 `address` 结构化地址与 `doc:home`/`doc:business` 统一展开为 HMRC 国际地址表的 **5 行**（Address line 1–5，另加 `Postcode`/`Country`）；每行 **35 字符上限，宁可报错也不截断**、不缩写。
- 动态值：`date:uk-next-month-first.day/.month/.year` 按英国 `Europe/London` 日期解析为下月 1 日；`vat-return-stagger:uk-next-month-first` 选对应季度申报组。
- `FIXED_VAT_PAGE_RULES`（config.py 内硬编码）：营业额固定填 10000/0/0（标准/降低/零税率），SIC 搜索 47910，追加在最后防止配置覆盖——这是当前业务模型的固定规则，改动前需确认业务含义。
- `allow_live_application: false`（默认）会在进入真实申请数据区前停止；置 `true` 前必须清除所有测试标记（`example.com`、`TEST-`、`Synthetic`、`Northstar`、`Tester`、保留手机号 `07700 900xxx`），否则程序拒绝继续。

## 测试与验证

测试用标准库 **unittest**（`unittest.TestCase` 风格，无 pytest 依赖，虽然目录里有 `.pytest_cache`）。已实测通过：

```bash
python -m compileall -q vat_automation tests workbench scripts   # 语法检查
python -m unittest discover -s tests -v                          # 165 个测试，全部通过
python scripts/check_flow_coverage.py vat-config.flow.json       # 84 个截图页面覆盖检查
python scripts/check_flow_coverage.py vat-config.test.json
```

注意：覆盖检查的 "covered" 只表示有规则映射，不代表在真实 HMRC 环境提交验证过。

## 代码风格约定

- 注释/文档字符串用中文；代码标识符用英文。
- 页面匹配一律走 `normalize()`（大小写、标点、空白归一化）后的标签文本，**禁止按 CSS class 选择器绑定 HMRC 页面**。
- 类型标注齐全（`from __future__ import annotations`，`str | None` 等新语法），数据类用 `@dataclass(slots=True)`。
- 行尾统一 LF（`.gitattributes`：`text=auto eol=lf`）。
- 业务菜单由插件登记（`workbench/catalog.py`），新增业务种类 = 新增插件模块调用 `register()`，不改工作台菜单结构。

## 安全边界（修改代码时必须保持）

- **凭据只经环境变量/内存/本机私有存储传入，不写进流程配置和日志**：Government Gateway 用户名/密码/MFA 通过进程环境变量（含项目根目录 `.env`，权限 0600、git 忽略）、内存 dict，或工作台按客户保存的 `workbench-data/credentials.json`（0600、git 忽略）传入；多用户场景（`VatAutomation(credentials=...)`）显式传 dict 以避免密码被 Chrome 子进程继承。密码和验证码不写入流程配置、状态接口或审计日志（审计里只留掩码），界面只显示掩码后的 User ID。
- **人工确认后才提交**：到达 `Check your answers` 保存整页复核 PDF 并暂停；只有网页用户明确勾选确认后才点击 `Confirm and submit`；暂停与复核均有 30 分钟超时。
- **凭据之外的密钥**放项目根目录 `.env`（git 忽略，模板 `.env.example`，目前用于百度翻译）。
- 动作白名单 `SAFE_ACTIONS`（runner.py）：只允许 `Save and continue` 等安全按钮；未知按钮/缺失必填字段/`action: stop` 立即停止，保存截图与 `artifacts/current-page.json`。
- Web 安全：scrypt 密码散列（含用户名不存在的恒时假散列）、登录失败 5 次指数退避锁定、会话 8 小时 TTL（重启即失效）、CSRF（SameSite Cookie + 来源校验 + 请求头令牌）、用户间数据隔离。
- 局域网模式（`--host 0.0.0.0`）自动生成自签证书（`certs/`，git 忽略），无证书拒绝启动；`--insecure-http` 会明文传输密码/证件，仅调试用。
- 身份证件以 0600 权限存 0700 临时目录，进程退出删除；`artifacts/` 内含客户个人信息，不得提交或共享。
- git 忽略：`.browser-profile*/`、`artifacts*/`、`vat-config*.json`（例外：`vat-config.example.json`、`vat-config.flow.json`）、`users.json`、`workbench-data/`、`certs/`、`.env`、`*.png/*.pdf/*.docx` 等二进制。

## 部署形态

无容器/CI 配置；部署 = 在服务器本机以 editable 方式安装并运行 `vat-web`（单用户）或 `vat-bench`（多人）。Playwright Chrome 始终跑在服务器本机，同事只通过浏览器看状态、输验证码、核对最终 PDF。`docs/id-card-translation-architecture.md` 描述了证件 OCR 拆分为双机架构（Workbench + OCR Worker）的设计，目前 OCR 仍在工作台进程内运行。
