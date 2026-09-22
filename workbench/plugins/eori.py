from workbench.catalog import Plugin, register

register(
    Plugin(
        id="uk-eori-register",
        name="英国 EORI 注册",
        group="注册",
        description=(
            "用已经注册好的英国 VAT 号申请 EORI 号，流程在服务器本机 Chrome 跑，"
            "同事只看状态、交验证码、核对最终 PDF。不需要身份证明文件。"
        ),
        task_kind="uk_eori",
        order=11,
        intake={
            "files": [
                {"category": "authorization", "label": "授权表", "min": 1, "parse": True},
                {
                    "category": "other",
                    "label": "VAT 证书等补充资料（选填）",
                    "min": 0,
                },
            ],
        },
    )
)
