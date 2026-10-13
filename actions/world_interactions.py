"""世界会话临时交互表 — 从物品 effects_on × accepts_from 编译。"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from graph_ops import apply_all, check_all, make_ctx
from action import get_item_location, get_person_location, get_property

_lock = threading.Lock()
_interactions: List["WorldInteraction"] = []


@dataclass
class WorldInteraction:
    tool: str
    target: str
    preconditions: List[Dict[str, Any]] = field(default_factory=list)
    ops: List[Dict[str, Any]] = field(default_factory=list)
    consume: Optional[bool] = None  # True/False 来自配置；None 表示仅看 ops

    def key(self) -> str:
        return f"{self.tool}|{self.target}"

    @property
    def consumes_tool(self) -> bool:
        if self.consume is not None:
            return bool(self.consume)
        for op in self.ops or []:
            if op.get("op") != "delete_entity":
                continue
            ent = op.get("entity")
            if ent in ("@tool", self.tool):
                return True
        return False


def clear_interactions() -> None:
    global _interactions
    with _lock:
        _interactions = []


def set_interactions(items: List[WorldInteraction]) -> None:
    global _interactions
    with _lock:
        _interactions = list(items)


def list_interactions() -> List[WorldInteraction]:
    with _lock:
        return list(_interactions)


def find_interaction(tool: str, target: str) -> Optional[WorldInteraction]:
    with _lock:
        for it in _interactions:
            if it.tool == tool and it.target == target:
                return it
    return None


def _parse_json_field(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None
    return None


def normalize_effect(effect: Dict[str, Any]) -> Dict[str, Any]:
    """规范化 effects_on 条目：consume 控制工具是否消失。"""
    e = dict(effect)
    consume = e.get("consume", None)
    ops = [dict(o) for o in (e.get("ops") or []) if isinstance(o, dict)]

    def _is_tool_delete(op: Dict[str, Any]) -> bool:
        return op.get("op") == "delete_entity" and op.get("entity") in ("@tool",)

    if consume is True:
        if not any(_is_tool_delete(o) for o in ops):
            ops.append({"op": "delete_entity", "entity": "@tool", "label": "Item"})
    elif consume is False:
        ops = [o for o in ops if not _is_tool_delete(o)]

    e["ops"] = ops
    if consume is not None:
        e["consume"] = bool(consume)
    return e


def _item_effects_on(world, name: str) -> List[Dict[str, Any]]:
    raw = get_property(world, name, "Item", "effects_on")
    data = _parse_json_field(raw)
    if not isinstance(data, list):
        return []
    return [normalize_effect(x) for x in data if isinstance(x, dict)]


def _accepts_from_entity(world, name: str, label: str) -> List[str]:
    raw = get_property(world, name, label, "accepts_from")
    data = _parse_json_field(raw)
    if isinstance(data, list):
        return [str(x) for x in data if x]
    if label == "Item":
        key_item = get_property(world, name, "Item", "key_item") or ""
        if key_item:
            return [str(key_item)]
    return []


def _entity_label(world, name: str) -> Optional[str]:
    if world.get_entity_by_name(name, label="Item"):
        return "Item"
    if world.get_entity_by_name(name, label="Person"):
        return "Person"
    return None


def target_location(world, target: str) -> Optional[str]:
    loc = get_item_location(world, target)
    if loc:
        return loc
    person_loc = get_person_location(world, target)
    if person_loc:
        return person_loc
    # 被持有的物品：视为持有者所在地（可对背包文书盖章等）
    from action import get_item_holder

    holder = get_item_holder(world, target)
    if holder:
        return get_person_location(world, holder)
    return None


def _tool_accepted(tool: str, accepts: List[str], *, tool_kind: str = "") -> bool:
    if tool in accepts:
        return True
    kind = (tool_kind or "").strip().lower()
    if kind and f"kind:{kind}" in [str(a).strip().lower() for a in accepts]:
        return True
    return False


def _item_kind(world, name: str) -> str:
    return str(get_property(world, name, "Item", "item_kind") or "").strip().lower()


def compile_interactions_from_world(world) -> List[WorldInteraction]:
    """扫描 Item 节点，编译当前图中的临时交互。"""
    names = world.list_entities("Item") or []
    kinds = {n: _item_kind(world, n) for n in names}
    out: List[WorldInteraction] = []
    seen = set()
    for tool in names:
        tool_kind = kinds[tool]
        for effect in _item_effects_on(world, tool):
            for target in expand_target(effect.get("target") or "", kinds):
                label = _entity_label(world, target)
                if not label:
                    continue
                accepts = _accepts_from_entity(world, target, label)
                if not _tool_accepted(tool, accepts, tool_kind=tool_kind):
                    continue
                key = f"{tool}|{target}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(_interaction(tool, target, effect))
    return out


def expand_target(target: str, kinds: Dict[str, str]) -> List[str]:
    """effects_on.target 可写物品名，或 kind:<类别>（匹配世界里所有该类物品）。"""
    target = (target or "").strip()
    if not target:
        return []
    if target.lower().startswith("kind:"):
        want = target[len("kind:"):].strip().lower()
        return sorted(n for n, k in kinds.items() if k == want)
    return [target]


def _interaction(tool: str, target: str, effect: Dict[str, Any]) -> WorldInteraction:
    consume = effect.get("consume", None)
    return WorldInteraction(
        tool=tool,
        target=target,
        preconditions=list(effect.get("preconditions") or []),
        ops=list(effect.get("ops") or []),
        consume=None if consume is None else bool(consume),
    )


def compile_interactions_from_blueprint(bp) -> List[WorldInteraction]:
    """从 Blueprint ItemSpec × (Item|Person).accepts_from 编译。"""
    items = list(getattr(bp, "items", None) or [])
    persons = list(getattr(bp, "persons", None) or [])
    by_item = {it.name: it for it in items}
    by_person = {p.name: p for p in persons}
    kinds = {it.name: str(getattr(it, "item_kind", "") or "").strip().lower() for it in items}
    out: List[WorldInteraction] = []
    seen = set()
    for tool_item in items:
        tool_kind = kinds[tool_item.name]
        effects = list(getattr(tool_item, "effects_on", None) or [])
        for raw in effects:
            if not isinstance(raw, dict):
                continue
            effect = normalize_effect(raw)
            for target in expand_target(effect.get("target") or "", kinds):
                accepts: List[str] = []
                if target in by_item:
                    ti = by_item[target]
                    accepts = list(getattr(ti, "accepts_from", None) or [])
                    if not accepts and getattr(ti, "key_item", ""):
                        accepts = [ti.key_item]
                elif target in by_person:
                    accepts = list(getattr(by_person[target], "accepts_from", None) or [])
                else:
                    continue
                if not _tool_accepted(tool_item.name, accepts, tool_kind=tool_kind):
                    continue
                key = f"{tool_item.name}|{target}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(_interaction(tool_item.name, target, effect))
    return out


def refresh_interactions_from_world(world) -> List[WorldInteraction]:
    items = compile_interactions_from_world(world)
    set_interactions(items)
    return items


def can_use_on(world, actor: str, tool: str, target: str) -> Tuple[bool, str, Optional[WorldInteraction]]:
    inter = find_interaction(tool, target)
    if inter is None:
        refresh_interactions_from_world(world)
        inter = find_interaction(tool, target)
    if inter is None:
        return False, f"{tool} cannot be used on {target} (no world interaction configured)", None
    target_loc = target_location(world, target) or ""
    ctx = make_ctx(actor=actor, tool=tool, target=target, target_loc=target_loc)
    ok, reason = check_all(world, inter.preconditions, ctx)
    if not ok:
        return False, reason, inter
    return True, "ok", inter


def execute_use_on(world, actor: str, tool: str, target: str) -> Tuple[bool, str]:
    ok, reason, inter = can_use_on(world, actor, tool, target)
    if not ok or inter is None:
        return False, reason
    target_loc = target_location(world, target) or ""
    ctx = make_ctx(actor=actor, tool=tool, target=target, target_loc=target_loc)
    try:
        apply_all(world, inter.ops, ctx)
    except Exception as e:
        return False, f"Graph operation failed: {e}"
    # 生成物 / 删除物后刷新交互表
    try:
        refresh_interactions_from_world(world)
    except Exception:
        pass
    return True, "ok"


def interactions_for_tool(tool: str) -> List[WorldInteraction]:
    with _lock:
        return [it for it in _interactions if it.tool == tool]


def tools_for_target(target: str) -> List[str]:
    with _lock:
        return [it.tool for it in _interactions if it.target == target]


def use_on_catalog() -> Dict[str, Dict[str, Any]]:
    """UI：target -> {tools, consume_by_tool}。"""
    with _lock:
        out: Dict[str, Dict[str, Any]] = {}
        for it in _interactions:
            slot = out.setdefault(it.target, {"tools": [], "consume_by_tool": {}})
            if it.tool not in slot["tools"]:
                slot["tools"].append(it.tool)
            slot["consume_by_tool"][it.tool] = it.consumes_tool
        return out


def link_pair(inter: WorldInteraction) -> Optional[Tuple[str, str]]:
    """从交互 ops 中取出第一条 Location link 的两端。"""
    for op in inter.ops or []:
        if op.get("op") != "link":
            continue
        a, b = op.get("a"), op.get("b")
        if a and b:
            return str(a), str(b)
    return None


def destination_from_interaction(
    inter: WorldInteraction, *, from_loc: str
) -> Optional[str]:
    pair = link_pair(inter)
    if not pair:
        return None
    a, b = pair
    if a == from_loc:
        return b
    if b == from_loc:
        return a
    return b if a else None
