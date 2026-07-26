"""通用图操作原语（白名单）— 不含 unlock 等游戏语义。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from action import (
    get_item_holder,
    get_item_location,
    get_person_location,
    get_property,
    set_property,
)


def resolve_token(token: Any, ctx: Dict[str, str]) -> Any:
    if not isinstance(token, str):
        return token
    if token.startswith("@"):
        key = token[1:]
        if key not in ctx:
            raise ValueError(f"未知占位符: {token}")
        return ctx[key]
    return token


def _ctx(
    *,
    actor: str,
    tool: str,
    target: str,
    target_loc: str = "",
) -> Dict[str, str]:
    return {
        "actor": actor,
        "tool": tool,
        "target": target,
        "target_loc": target_loc or "",
    }


def check_condition(world, cond: Dict[str, Any], ctx: Dict[str, str]) -> Tuple[bool, str]:
    op = (cond.get("op") or "").strip()
    try:
        if op == "prop_eq":
            entity = resolve_token(cond["entity"], ctx)
            label = cond.get("label") or "Item"
            prop = cond["prop"]
            want = cond.get("value")
            got = get_property(world, entity, label, prop)
            if isinstance(want, bool):
                if bool(got) != want:
                    return False, f"{entity}.{prop} 期望 {want}，实际 {got}"
            elif got != want:
                return False, f"{entity}.{prop} 期望 {want}，实际 {got}"
            return True, "ok"

        if op == "prop_neq":
            entity = resolve_token(cond["entity"], ctx)
            label = cond.get("label") or "Item"
            prop = cond["prop"]
            want = cond.get("value")
            got = get_property(world, entity, label, prop)
            if isinstance(want, bool):
                if bool(got) == want:
                    return False, f"{entity}.{prop} 不应为 {want}"
            elif got == want:
                return False, f"{entity}.{prop} 不应为 {want}"
            return True, "ok"

        if op == "prop_gte":
            entity = resolve_token(cond["entity"], ctx)
            label = cond.get("label") or "Item"
            prop = cond["prop"]
            want = float(cond.get("value") or 0)
            got = get_property(world, entity, label, prop)
            try:
                got_n = float(got if got is not None else 0)
            except (TypeError, ValueError):
                return False, f"{entity}.{prop} 不是数值: {got}"
            if got_n < want:
                return False, f"{entity}.{prop}={got_n} < {want}"
            return True, "ok"

        if op == "has_item":
            person = resolve_token(cond["person"], ctx)
            item = resolve_token(cond["item"], ctx)
            if get_item_holder(world, item) != person:
                return False, f"{person} 未持有 {item}"
            return True, "ok"

        if op == "person_at":
            person = resolve_token(cond["person"], ctx)
            loc = resolve_token(cond["location"], ctx)
            cur = get_person_location(world, person)
            if cur != loc:
                return False, f"{person} 不在 {loc}（在 {cur}）"
            return True, "ok"

        if op == "item_at":
            entity = resolve_token(cond["entity"], ctx)
            loc = resolve_token(cond["location"], ctx)
            cur = get_item_location(world, entity)
            if cur != loc:
                return False, f"{entity} 不在 {loc}（在 {cur}）"
            return True, "ok"

        if op == "has_rel":
            a = resolve_token(cond["a"], ctx)
            b = resolve_token(cond["b"], ctx)
            a_label = cond.get("a_label") or "Location"
            b_label = cond.get("b_label") or "Location"
            rel = cond.get("rel") or "ADJACENT_TO"
            if not world.has_relationship(a, a_label, b, b_label, rel):
                return False, f"缺少关系 {a}-[{rel}]->{b}"
            return True, "ok"

        if op == "not_has_rel":
            a = resolve_token(cond["a"], ctx)
            b = resolve_token(cond["b"], ctx)
            a_label = cond.get("a_label") or "Location"
            b_label = cond.get("b_label") or "Location"
            rel = cond.get("rel") or "ADJACENT_TO"
            if world.has_relationship(a, a_label, b, b_label, rel):
                return False, f"不应存在关系 {a}-[{rel}]->{b}"
            return True, "ok"

        return False, f"未知条件原语: {op}"
    except Exception as e:
        return False, str(e)


def apply_op(world, op_spec: Dict[str, Any], ctx: Dict[str, str]) -> None:
    op = (op_spec.get("op") or "").strip()

    if op == "set_prop":
        entity = resolve_token(op_spec["entity"], ctx)
        label = op_spec.get("label") or "Item"
        prop = op_spec["prop"]
        value = resolve_token(op_spec.get("value"), ctx)
        set_property(world, entity, label, prop, value)
        return

    if op == "add_prop":
        entity = resolve_token(op_spec["entity"], ctx)
        label = op_spec.get("label") or "Item"
        prop = op_spec["prop"]
        delta = op_spec.get("delta", 0)
        try:
            delta_n = float(delta)
        except (TypeError, ValueError):
            delta_n = 0.0
        cur = get_property(world, entity, label, prop)
        try:
            cur_n = float(cur if cur is not None else 0)
        except (TypeError, ValueError):
            cur_n = 0.0
        new_v = cur_n + delta_n
        # 整数属性保持 int（fuel 等）
        if float(new_v).is_integer():
            new_v = int(new_v)
        set_property(world, entity, label, prop, new_v)
        return

    if op == "spawn_item":
        # 在地点生成或交到角色手上；name 已存在则自动加后缀
        base_name = str(resolve_token(op_spec.get("name") or "Item", ctx))
        name = base_name
        n = 1
        while world.get_entity_by_name(name, label="Item"):
            n += 1
            name = f"{base_name}_{n}"
        props: Dict[str, Any] = {}
        if op_spec.get("item_kind") is not None:
            props["item_kind"] = str(op_spec.get("item_kind") or "")
        if op_spec.get("portable") is not None:
            props["portable"] = bool(op_spec.get("portable"))
        else:
            props["portable"] = True
        if op_spec.get("value") is not None:
            props["value"] = int(op_spec.get("value") or 0)
        if op_spec.get("description"):
            props["description"] = str(op_spec.get("description"))
        if op_spec.get("effects_on") is not None:
            import json

            props["effects_on"] = (
                op_spec["effects_on"]
                if isinstance(op_spec["effects_on"], str)
                else json.dumps(op_spec["effects_on"], ensure_ascii=False)
            )
        world.add_entity(name, entity_type="Item", **props)
        held_by = op_spec.get("held_by")
        located_at = op_spec.get("located_at")
        if held_by:
            holder = resolve_token(held_by, ctx)
            world.link_entities(holder, "Person", name, "Item", "HAS_ITEM")
        elif located_at:
            loc = resolve_token(located_at, ctx)
            world.link_entities(name, "Item", loc, "Location", "LOCATED_AT")
        return

    if op == "link":
        a = resolve_token(op_spec["a"], ctx)
        b = resolve_token(op_spec["b"], ctx)
        a_label = op_spec.get("a_label") or "Location"
        b_label = op_spec.get("b_label") or "Location"
        rel = op_spec.get("rel") or "ADJACENT_TO"
        # 注意：world.has_relationship 是无向匹配；双向时必须各建一条有向边，
        # 否则 Courtyard→Cellar 建完后会误判反向已存在，导致进地窖出不来。
        world.link_entities(a, a_label, b, b_label, rel)
        if op_spec.get("bidirectional"):
            world.link_entities(b, b_label, a, a_label, rel)
        return

    if op == "unlink":
        a = resolve_token(op_spec["a"], ctx)
        b = resolve_token(op_spec["b"], ctx)
        a_label = op_spec.get("a_label") or "Location"
        b_label = op_spec.get("b_label") or "Location"
        rel = op_spec.get("rel") or "ADJACENT_TO"
        world.run_query(
            f"""
            MATCH (a:{a_label} {{name: $a}})-[r:{rel}]->(b:{b_label} {{name: $b}})
            DELETE r
            """,
            a=a,
            b=b,
        )
        if op_spec.get("bidirectional"):
            world.run_query(
                f"""
                MATCH (a:{b_label} {{name: $a}})-[r:{rel}]->(b:{a_label} {{name: $b}})
                DELETE r
                """,
                a=b,
                b=a,
            )
        return

    if op == "delete_entity":
        entity = resolve_token(op_spec["entity"], ctx)
        label = op_spec.get("label") or "Item"
        world.delete_entity(entity, label=label)
        return

    raise ValueError(f"未知操作原语: {op}")


def check_all(world, conditions: List[Dict[str, Any]], ctx: Dict[str, str]) -> Tuple[bool, str]:
    for cond in conditions or []:
        ok, reason = check_condition(world, cond, ctx)
        if not ok:
            return False, reason
    return True, "ok"


def apply_all(world, ops: List[Dict[str, Any]], ctx: Dict[str, str]) -> None:
    for op_spec in ops or []:
        apply_op(world, op_spec, ctx)


def make_ctx(*, actor: str, tool: str, target: str, target_loc: str = "") -> Dict[str, str]:
    return _ctx(actor=actor, tool=tool, target=target, target_loc=target_loc)
