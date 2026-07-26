"""
兼容层：事件模板已改为 worlds/*.json 模板世界。

保留 list_templates / pick_template / TEMPLATES 供旧调用方使用。
"""

from __future__ import annotations

from typing import Dict, List

from .blueprint import EventBlueprint
from .world_loader import list_worlds, load_world, pick_world


def list_templates() -> List[Dict[str, str]]:
    return list_worlds()


def pick_template(prompt: str) -> EventBlueprint:
    bp = pick_world(prompt)
    bp.source = "mock"
    return bp


class _TemplateProxy(dict):
    """懒加载：TEMPLATES[id] -> EventBlueprint。"""

    def __contains__(self, key: object) -> bool:  # type: ignore[override]
        if not isinstance(key, str):
            return False
        return any(w["id"] == key for w in list_worlds())

    def __getitem__(self, key: str) -> EventBlueprint:  # type: ignore[override]
        return load_world(key)


TEMPLATES: Dict[str, EventBlueprint] = _TemplateProxy()  # type: ignore[assignment]
