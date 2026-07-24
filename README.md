# UK VAT registration automation

这是一个基于 Python + Playwright 的英国 VAT 注册流程驱动器。当前目录中的 178 张截图用于确认流程入口、GOV.UK 页面结构以及主要交互类型；程序运行时按页面真实标签匹配配置，不依赖易变化的 CSS class。

## 安全边界

- Government Gateway 用户名和密码从环境变量读取并自动填写；仅验证码暂停等待用户输入。
- 遇到缺少的必填字段、未知按钮或配置为 `stop` 的页面时立即停止，并保存截图与页面元数据。
- 初始诚信声明页会自动点击 `Accept and continue`；若检测到明显测试资料会直接停止。
- 到达 `Check your answers` 或正式提交页面时强制停止；程序没有最终提交能力。
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

只有改用经过核实的真实申请资料后，才可显式设置 `allow_live_application: true`。启用后，初始诚信声明页会自动点击 `Accept and continue`；若配置或环境变量里仍有 `example.com`、`TEST-`、`07700 900xxx`、`Synthetic`、`Northstar`、`Tester` 等测试标记，程序会拒绝点击声明按钮。最终复核和正式提交仍会强制停止等待人工处理。

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

## 验证

```bash
python -m compileall -q vat_automation tests
python -m unittest discover -s tests -v
python scripts/check_flow_coverage.py vat-config.test.json
```

覆盖检查基于截图整理出的 84 个逻辑页面；“covered”表示已有专用规则、通用字段映射或特殊页面处理，不代表每个分支都已在真实 HMRC 环境提交验证。
