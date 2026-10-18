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
            raise ValueError(f"Unknown placeholder: {token}")
        return ctx[key]
    return token


class _GraphCtx(dict):
    """占位符上下文；支持 `@target.prop` / `@actor.prop` 读取实体属性。"""

    def __init__(self, world, base: Dict[str, str]):
        super().__init__(base)
        self._world = world

    def _lookup(self, key: str) -> Any:
        head, _, prop = key.partition(".")
        if not prop or not dict.__contains__(self, head):
            raise KeyError(key)
        entity = dict.__getitem__(self, head)
        for label in ("Item", "Person", "Location"):
            if self._world.get_entity_by_name(entity, label=label):
                value = get_property(self._world, entity, label, prop)
                if value is None:
                    raise KeyError(key)
                return value
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        if dict.__contains__(self, key):
            return True
        try:
            self._lookup(str(key))
            return True
        except KeyError:
            return False

    def __missing__(self, key: str) -> Any:
        return self._lookup(key)


def _with_world(world, ctx: Dict[str, str]) -> Dict[str, str]:
    if isinstance(ctx, _GraphCtx) or world is None:
        return ctx
    return _GraphCtx(world, ctx)


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
    """条件可带 "message"（失败时的说明，支持 @占位符）。"""
    ctx = _with_world(world, ctx)
    ok, reason = _check_condition(world, cond, ctx)
    if not ok and cond.get("message"):
        reason = render_text(str(cond["message"]), ctx)
    return ok, reason


def render_text(text: str, ctx: Dict[str, str]) -> str:
    import re

    def sub(m: "re.Match[str]") -> str:
        try:
            return str(resolve_token(m.group(0), ctx))
        except Exception:
            return m.group(0)

    return re.sub(r"@[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", sub, text)


def _check_condition(world, cond: Dict[str, Any], ctx: Dict[str, str]) -> Tuple[bool, str]:
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
                    return False, f"{entity}.{prop} expected {want}, got {got}"
            elif got != want:
                return False, f"{entity}.{prop} expected {want}, got {got}"
            return True, "ok"

        if op == "prop_neq":
            entity = resolve_token(cond["entity"], ctx)
            label = cond.get("label") or "Item"
            prop = cond["prop"]
            want = cond.get("value")
            got = get_property(world, entity, label, prop)
            # strict：只有真等于 value 才算（未设置的属性不等于 false）
            if isinstance(want, bool) and not cond.get("strict"):
                if bool(got) == want:
                    return False, cond.get("message") or f"{entity}.{prop} should not be {want}"
            elif got == want:
                return False, cond.get("message") or f"{entity}.{prop} should not be {want}"
            return True, "ok"

        if op == "prop_gte":
            entity = resolve_token(cond["entity"], ctx)
            label = cond.get("label") or "Item"
            prop = cond["prop"]
            # value 可以是另一个实体的属性：{"value": "@i.value"}（钱够不够付这件东西的价）
            want = float(resolve_token(cond.get("value") or 0, ctx) or 0)
            got = get_property(world, entity, label, prop)
            try:
                got_n = float(got if got is not None else 0)
            except (TypeError, ValueError):
                return False, f"{entity}.{prop} is not numeric: {got}"
            if got_n < want:
                return False, f"{entity}.{prop}={got_n} < {want}"
            return True, "ok"

        if op == "has_item":
            person = resolve_token(cond["person"], ctx)
            item = resolve_token(cond["item"], ctx)
            if get_item_holder(world, item) != person:
                return False, f"{person} is not holding {item}"
            return True, "ok"

        if op == "person_at":
            person = resolve_token(cond["person"], ctx)
            loc = resolve_token(cond["location"], ctx)
            cur = get_person_location(world, person)
            if cur != loc:
                return False, f"{person} is not at {loc} (at {cur})"
            return True, "ok"

        if op == "item_at":
            entity = resolve_token(cond["entity"], ctx)
            loc = resolve_token(cond["location"], ctx)
            cur = get_item_location(world, entity)
            if cur != loc:
                return False, f"{entity} is not at {loc} (at {cur})"
            return True, "ok"

        if op == "has_rel":
            a = resolve_token(cond["a"], ctx)
            b = resolve_token(cond["b"], ctx)
            a_label = cond.get("a_label") or "Location"
            b_label = cond.get("b_label") or "Location"
            rel = cond.get("rel") or "ADJACENT_TO"
            has = world.has_directed_relationship if cond.get("directed") else world.has_relationship
            if not has(a, a_label, b, b_label, rel):
                return False, cond.get("message") or f"Missing relationship {a}-[{rel}]->{b}"
            return True, "ok"

        if op == "any":
            reasons = []
            for sub in cond.get("of") or []:
                ok, why = check_condition(world, sub, ctx)
                if ok:
                    return True, "ok"
                reasons.append(why)
            return False, cond.get("message") or " / ".join(reasons) or "no alternative holds"

        if op == "prop_in":
            # values 里任一匹配；prefix=true 时按前缀匹配（均不区分大小写）
            entity = resolve_token(cond["entity"], ctx)
            label = cond.get("label") or "Item"
            prop = cond["prop"]
            got = entity if prop == "name" else get_property(world, entity, label, prop)
            got_s = str(got or "").lower()
            values = [str(v).lower() for v in cond.get("values") or []]
            hit = any(got_s.startswith(v) for v in values) if cond.get("prefix") else got_s in values
            if not hit:
                return False, cond.get("message") or f"{entity}.{prop}={got} not in {values}"
            return True, "ok"

        if op == "neq":
            a = resolve_token(cond["a"], ctx)
            b = resolve_token(cond["b"], ctx)
            if a == b:
                return False, cond.get("message") or f"{a} must differ from {b}"
            return True, "ok"

        if op == "not_held":
            item = resolve_token(cond["item"], ctx)
            holder = get_item_holder(world, item)
            if holder is not None:
                return False, f"{item} is already held by {holder}"
            return True, "ok"

        if op == "prop_lte":
            entity = resolve_token(cond["entity"], ctx)
            label = cond.get("label") or "Item"
            prop = cond["prop"]
            want = float(resolve_token(cond.get("value") or 0, ctx) or 0)
            got = get_property(world, entity, label, prop)
            try:
                got_n = float(got if got is not None else 0)
            except (TypeError, ValueError):
                return False, f"{entity}.{prop} is not numeric: {got}"
            if got_n > want:
                return False, cond.get("message") or f"{entity}.{prop}={got_n} > {want}"
            return True, "ok"

        if op == "feels_gte":
            # a 对 b 的好感 ≥ value（relations.py）；规划时当作成立，执行时才真正检查
            from relations import feeling

            a_name, b_name = resolve_token(cond["a"], ctx), resolve_token(cond["b"], ctx)
            want = float(resolve_token(cond.get("value") or 0, ctx) or 0)
            got = feeling(world, a_name, b_name)
            if got < want:
                return False, f"{a_name} doesn't feel close enough to {b_name} ({got} < {want:g})"
            return True, "ok"

        if op == "not_hostile":
            # a、b 不属于互相敌对的联盟
            from relations import hostile

            a_name, b_name = resolve_token(cond["a"], ctx), resolve_token(cond["b"], ctx)
            if hostile(world, a_name, b_name):
                return False, f"{a_name} and {b_name} belong to hostile alliances"
            return True, "ok"

        if op == "count":
            # 统计物品数：按 archetype / kind（可多个），可限定 location；gte / lte 比较
            n = count_items(
                world,
                archetypes=_as_list(cond.get("archetype")),
                kinds=_as_list(cond.get("kind")),
                location=resolve_token(cond["location"], ctx) if cond.get("location") else None,
            )
            if cond.get("gte") is not None and n < int(cond["gte"]):
                return False, cond.get("message") or f"Need at least {cond['gte']} (have {n})"
            if cond.get("lte") is not None and n > int(cond["lte"]):
                return False, cond.get("message") or f"Need at most {cond['lte']} (have {n})"
            return True, "ok"

        if op == "not_has_rel":
            a = resolve_token(cond["a"], ctx)
            b = resolve_token(cond["b"], ctx)
            a_label = cond.get("a_label") or "Location"
            b_label = cond.get("b_label") or "Location"
            rel = cond.get("rel") or "ADJACENT_TO"
            if world.has_relationship(a, a_label, b, b_label, rel):
                return False, f"Relationship should not exist {a}-[{rel}]->{b}"
            return True, "ok"

        return False, f"Unknown condition primitive: {op}"
    except Exception as e:
        return False, str(e)


def _as_list(raw: Any) -> List[str]:
    if raw is None or raw == "":
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [str(x).strip().lower() for x in items if str(x).strip()]


def count_items(
    world,
    *,
    archetypes: Optional[List[str]] = None,
    kinds: Optional[List[str]] = None,
    location: Optional[str] = None,
) -> int:
    rows = world.run_query(
        """
        MATCH (i:Item)
        OPTIONAL MATCH (i)-[:LOCATED_AT]->(l:Location)
        WHERE $loc IS NULL OR l.name = $loc
        WITH i, l
        WHERE ($loc IS NULL OR l IS NOT NULL)
          AND (size($arch) = 0 OR toLower(coalesce(i.archetype, '')) IN $arch)
          AND (size($kinds) = 0 OR toLower(coalesce(i.item_kind, '')) IN $kinds)
        RETURN count(i) AS n
        """,
        loc=location,
        arch=archetypes or [],
        kinds=kinds or [],
    ) or []
    return int(rows[0]["n"]) if rows else 0


def _place_spawned(world, name: str, op_spec: Dict[str, Any], ctx) -> None:
    """格子坐标：显式 x/y（可用 @self.x）原样写入；否则靠近 near 实体，或找区域中心附近空位。"""
    import spatial

    if op_spec.get("x") is not None and op_spec.get("y") is not None:
        try:
            x = int(float(resolve_token(op_spec["x"], ctx)))
            y = int(float(resolve_token(op_spec["y"], ctx)))
        except (TypeError, ValueError, KeyError):
            x = y = None  # type: ignore[assignment]
        if x is not None and y is not None:
            world.run_query("MATCH (i:Item {name: $n}) SET i.x = $x, i.y = $y", n=name, x=x, y=y)
            return
    near = None
    if op_spec.get("near"):
        try:
            near = spatial.entity_cell(world, str(resolve_token(op_spec["near"], ctx)))
        except (KeyError, ValueError):
            near = None
    spatial.place_item(world, name, near=near)


def apply_op(world, op_spec: Dict[str, Any], ctx: Dict[str, str]) -> None:
    op = (op_spec.get("op") or "").strip()
    ctx = _with_world(world, ctx)

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
        delta = resolve_token(op_spec.get("delta", 0), ctx)
        try:
            delta_n = float(delta) * float(op_spec.get("scale", 1))
        except (TypeError, ValueError):
            delta_n = 0.0
        cur = get_property(world, entity, label, prop)
        try:
            cur_n = float(cur if cur is not None else 0)
        except (TypeError, ValueError):
            cur_n = 0.0
        new_v = cur_n + delta_n
        if op_spec.get("min") is not None:
            new_v = max(float(op_spec["min"]), new_v)
        if op_spec.get("max") is not None:
            new_v = min(float(op_spec["max"]), new_v)
        # 整数属性保持 int（fuel 等）
        if float(new_v).is_integer():
            new_v = int(new_v)
        set_property(world, entity, label, prop, new_v)
        return

    if op == "spawn_item":
        # 在地点生成或交到角色手上；name 已存在则自动加后缀；可用 "archetype" 套原型
        archetype = str(op_spec.get("archetype") or "")
        if archetype:
            from content import apply_archetype

            op_spec = apply_archetype(dict(op_spec))
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
        if archetype:
            props["archetype"] = archetype
        if op_spec.get("footprint") is not None:
            import spatial

            props["footprint_w"], props["footprint_h"] = spatial.footprint_of(op_spec)
        import json

        for key in ("effects_on", "accepts_from", "script"):
            if op_spec.get(key) is not None:
                raw = op_spec[key]
                props[key] = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        script = op_spec.get("script")
        init = script.get("initial_state") if isinstance(script, dict) else None
        for k, v in (init or {}).items():
            if k not in props and k != "name" and isinstance(v, (str, int, float, bool)):
                props[k] = v
        world.add_entity(name, entity_type="Item", **props)
        held_by = op_spec.get("held_by")
        located_at = op_spec.get("located_at")
        if held_by:
            holder = resolve_token(held_by, ctx)
            world.link_entities(holder, "Person", name, "Item", "HAS_ITEM")
        elif located_at:
            loc = resolve_token(located_at, ctx)
            world.link_entities(name, "Item", loc, "Location", "LOCATED_AT")
            _place_spawned(world, name, op_spec, ctx)
        return

    if op == "spawn_location":
        # 新地点（已存在则只补连通）；adjacent_to 可为单个或列表，默认双向连通
        name = str(resolve_token(op_spec["name"], ctx))
        if not world.get_entity_by_name(name, label="Location"):
            props = {"description": str(op_spec.get("description") or "")}
            if op_spec.get("tags"):
                props["tags"] = str(op_spec["tags"])
            world.add_entity(name, entity_type="Location", **props)
        adj = op_spec.get("adjacent_to") or []
        for other in adj if isinstance(adj, list) else [adj]:
            other = str(resolve_token(other, ctx))
            if not world.has_directed_relationship(name, "Location", other, "Location", "ADJACENT_TO"):
                world.link_entities(name, "Location", other, "Location", "ADJACENT_TO")
            if op_spec.get("bidirectional", True) and not world.has_directed_relationship(
                other, "Location", name, "Location", "ADJACENT_TO"
            ):
                world.link_entities(other, "Location", name, "Location", "ADJACENT_TO")
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

    if op == "relocate":
        # 把实体从任何地方（地点 / 持有者）挪到 to
        entity = resolve_token(op_spec["entity"], ctx)
        label = op_spec.get("label") or "Item"
        to = resolve_token(op_spec["to"], ctx)
        world.run_query(
            f"MATCH (n:{label} {{name: $n}})-[r:LOCATED_AT]->() DELETE r", n=entity
        )
        if label == "Item":
            world.run_query("MATCH (:Person)-[r:HAS_ITEM]->(i:Item {name: $n}) DELETE r", n=entity)
        world.link_entities(entity, label, to, "Location", "LOCATED_AT")
        return

    if op == "set_state":
        # 角色持续状态（内容包 states/ 定义）；state 为空 = 清除
        entity = resolve_token(op_spec["entity"], ctx)
        state = op_spec.get("state") or None
        set_property(world, entity, "Person", "state", state)
        return

    raise ValueError(f"Unknown operation primitive: {op}")


def check_all(world, conditions: List[Dict[str, Any]], ctx: Dict[str, str]) -> Tuple[bool, str]:
    for cond in conditions or []:
        ok, reason = check_condition(world, cond, ctx)
        if not ok:
            return False, reason
    return True, "ok"


def apply_all(world, ops: List[Dict[str, Any]], ctx: Dict[str, str]) -> None:
    """依次执行；带 "when": [条件...] 的操作只在条件全部成立时执行。"""
    ctx = _with_world(world, ctx)
    for op_spec in ops or []:
        when = op_spec.get("when")
        if when and not check_all(world, when, ctx)[0]:
            continue
        apply_op(world, op_spec, ctx)


def make_ctx(*, actor: str, tool: str, target: str, target_loc: str = "") -> Dict[str, str]:
    return _ctx(actor=actor, tool=tool, target=target, target_loc=target_loc)
