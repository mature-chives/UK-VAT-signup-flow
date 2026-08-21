from workbench.catalog import Plugin, register

register(
    Plugin(
        id="sa-vat-register",
        name="沙特 VAT 注册",
        group="注册",
        description="占位：可建任务并挂客户资料，自动化尚未接入。",
        task_kind="placeholder",
        placeholder=True,
        order=20,
        intake={
            "files": [
                {"category": "other", "label": "客户资料", "min": 0},
            ]
        },
    )
)
