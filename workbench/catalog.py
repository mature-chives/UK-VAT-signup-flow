from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Plugin:
    """一条可登记的业务能力。新增业务=登记一个插件，不要改工作台菜单结构。"""

    id: str
    name: str
    group: str
    description: str
    task_kind: str
    placeholder: bool = False
    order: int = 100
    intake: dict[str, object] = field(default_factory=dict)

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "group": self.group,
            "description": self.description,
            "task_kind": self.task_kind,
            "placeholder": self.placeholder,
            "order": self.order,
            "intake": dict(self.intake),
        }


_REGISTRY: dict[str, Plugin] = {}


def register(plugin: Plugin) -> None:
    if not plugin.id or plugin.id in _REGISTRY:
        raise ValueError(f"插件编码无效或已存在：{plugin.id}")
    _REGISTRY[plugin.id] = plugin


def get_plugin(plugin_id: str) -> Plugin | None:
    return _REGISTRY.get(plugin_id)


def list_plugins() -> list[Plugin]:
    return sorted(_REGISTRY.values(), key=lambda item: (item.order, item.group, item.id))


def load_builtin_plugins() -> None:
    """导入内置插件模块，由其自行 register。"""
    if _REGISTRY:
        return
    from .plugins import eori, sa_vat, translate, uk_vat  # noqa: F401
