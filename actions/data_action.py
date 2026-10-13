"""
数据动作 — 由内容包 JSON 定义的动作，引擎只负责解释执行。

    {
      "name": "move",
      "capability": "travel",
      "description": "Walk to an adjacent location",
      "duration": 1.0,
      "params": [
        {"name": "c", "type": "Person", "source": "self"},
        {"name": "from", "type": "Location", "source": "here"},
        {"name": "to", "type": "Location", "source": "adjacent"}
      ],
      "requires": [ {"op": "person_at", "person": "@c", "location": "@from"}, ... ],
      "effects":  [ {"op": "relocate", "entity": "@c", "label": "Person", "to": "@to"}, ... ],
      "enter_state": "asleep"          # 可选：执行后角色进入该持续状态
    }

占位符：@<参数名>、@actor（= 第一个参数）、@<参数名>.<属性>。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from action import Action, Param, get_property
from graph_ops import apply_all, check_all


class DataAction(Action):
    def __init__(self, spec: Dict[str, Any]):
        params = [Param(str(p["name"]), str(p["type"])) for p in spec["params"]]
        super().__init__(
            name=str(spec["name"]),
            description=str(spec.get("description") or spec["name"]),
            parameters=params,
            category=str(spec.get("category") or spec.get("capability") or "content"),
            preconditions_pddl=list(spec.get("pddl_preconditions") or []),
            effects_pddl=list(spec.get("pddl_effects") or []),
        )
        self.spec = spec
        self.capability = spec.get("capability")
        self.duration = float(spec.get("duration", 1.0))
        self.enter_state = spec.get("enter_state") or None
        self.requires: List[Dict[str, Any]] = list(spec.get("requires") or [])
        self.effects: List[Dict[str, Any]] = list(spec.get("effects") or [])
        self.param_sources: Dict[str, str] = {
            str(p["name"]): str(p.get("source") or "") for p in spec["params"]
        }
        self.narrative = str(spec.get("narrative") or "")

    def _ctx(self, kwargs: Dict[str, Any]) -> Dict[str, str]:
        ctx = {p.name: str(kwargs.get(p.name, "")) for p in self.parameters}
        ctx["actor"] = ctx[self.parameters[0].name]
        return ctx

    def _missing(self, kwargs: Dict[str, Any]) -> List[str]:
        return [p.name for p in self.parameters if not kwargs.get(p.name)]

    def check(self, world, **kwargs) -> Tuple[bool, str]:
        missing = self._missing(kwargs)
        if missing:
            return False, f"{self.name}: missing argument(s) {', '.join(missing)}"
        blocked = self._untargetable(world, kwargs)
        if blocked:
            return False, blocked
        return check_all(world, self.requires, self._ctx(kwargs))

    def apply(self, world, **kwargs) -> None:
        ctx = self._ctx(kwargs)
        apply_all(world, self.effects, ctx)
        if self.enter_state:
            from graph_ops import apply_op

            apply_op(world, {"op": "set_state", "entity": "@actor", "state": self.enter_state}, ctx)

    def describe(self, **kwargs) -> str:
        if not self.narrative:
            return ""
        text = self.narrative
        for k, v in self._ctx(kwargs).items():
            text = text.replace(f"@{k}", str(v))
        return text

    def _untargetable(self, world, kwargs: Dict[str, Any]) -> str:
        """处于不可打扰状态（如睡着）的人不能被当作对象。"""
        from content import get_content

        content = get_content()
        for p in self.parameters[1:]:
            if p.type != "Person":
                continue
            who = kwargs.get(p.name)
            state = content.state(get_property(world, who, "Person", "state"))
            if state is not None and not state.targetable:
                return f"{who} {state.description}"
        return ""
