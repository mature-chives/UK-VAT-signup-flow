from workbench.catalog import Plugin, register

register(
    Plugin(
        id="uk-vat-register",
        name="英国 VAT 注册",
        group="注册",
        description="解析授权表、上传身份证明，在服务器本机 Chrome 走 HMRC 注册。同事只看状态和验证码。",
        task_kind="uk_vat",
        order=10,
        intake={
            "files": [
                {"category": "authorization", "label": "授权表", "min": 1, "parse": True},
                {"category": "identity", "label": "身份证明", "min": 3, "max": 3},
            ],
            "credentials": True,
        },
    )
)
