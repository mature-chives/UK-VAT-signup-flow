from workbench.catalog import Plugin, register

register(
    Plugin(
        id="translate-id",
        name="翻译证件照",
        group="翻译",
        description="上传身份证正反面，本机识别后核对，再生成一份两页英文翻译件。",
        task_kind="translate",
        order=30,
        intake={
            "files": [
                {"category": "id-card-front", "label": "身份证正面", "min": 1, "max": 1},
                {"category": "id-card-back", "label": "身份证背面", "min": 1, "max": 1},
            ]
        },
    )
)

for item in (
    ("translate-license", "翻译营业执照", "营业执照翻译，上传原件后回传译文。", 31),
    ("translate-poa", "翻译 POA", "授权委托书翻译，上传原件后回传译文。", 32),
):
    plugin_id, name, description, order = item
    register(
        Plugin(
            id=plugin_id,
            name=name,
            group="翻译",
            description=description,
            task_kind="translate",
            order=order,
            intake={
                "files": [
                    {"category": "translation-source", "label": "翻译原件", "min": 0},
                ]
            },
        )
    )
