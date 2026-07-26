"""
物品行为脚本 — 通用解释器，不绑定篝火/伐木等具体玩法。

物品可挂 script（JSON），例如：
{
  "lore": "自然语言：给 LLM 读，用来讲故事和做决策",
  "on_tick": [
    {"op": "add_prop", "prop": "fuel", "delta_per_sec": -0.35, "min": 0},
    {"op": "derive_bool", "prop": "lit", "from_prop": "fuel", "gt": 0.05}
  ],
  "alerts": [
    {"when": {"prop_lt": ["fuel", 18]}, "signal": "urgent",
     "message": "Fuel is low.", "prefer_kind": "wood", "use_on": true}
  ],
  "on_tick_also": [
    {"op": "ensure_kind", "item_kind": "wood", "min": 3, "at": "Forest",
     "prefix": "Wood", "spawn": {...}}
  ]
}

数值变化走白名单 op；故事与动机走 lore / alert.message，由 LLM 发挥。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld


def parse_script(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            data = json.loads(s)
            return data if isinstance(data, dict) else {}
        except Exception:
            # 纯自然语言也当作 lore
            return {"lore": s}
    return {}


def load_item_script(world: "Neo4jWorld", name: str) -> Dict[str, Any]:
    from action import get_property

    return parse_script(get_property(world, name, "Item", "script"))


def list_scripted_items(world: "Neo4jWorld") -> List[Tuple[str, Dict[str, Any]]]:
    rows = world.run_query(
        """
        MATCH (i:Item)
        WHERE i.script IS NOT NULL AND trim(toString(i.script)) <> ''
        RETURN i.name AS name, i.script AS script
        ORDER BY name
        """
    ) or []
    out: List[Tuple[str, Dict[str, Any]]] = []
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        script = parse_script(r.get("script"))
        if script:
            out.append((str(name), script))
    return out


def tick_item_scripts(world: "Neo4jWorld", dt: float) -> Dict[str, Any]:
    """对所有带 script 的物品执行 on_tick，并汇总 alerts。"""
    report: Dict[str, Any] = {"events": [], "alerts": [], "spawned": [], "items": {}}
    need_refresh = False
    for name, script in list_scripted_items(world):
        item_events: List[Dict[str, Any]] = []
        for rule in list(script.get("on_tick") or []):
            if not isinstance(rule, dict):
                continue
            ev, spawned = _run_tick_op(world, name, rule, dt)
            if ev:
                item_events.extend(ev if isinstance(ev, list) else [ev])
            if spawned:
                report["spawned"].extend(spawned)
                need_refresh = True
        alerts = _eval_alerts(world, name, script)
        report["alerts"].extend(alerts)
        report["items"][name] = {
            "lore": (script.get("lore") or script.get("summary") or "").strip(),
            "events": item_events,
            "alerts": alerts,
        }
        if item_events:
            report["events"].extend(item_events)

    if need_refresh and report["spawned"]:
        try:
            from world_interactions import refresh_interactions_from_world

            refresh_interactions_from_world(world)
        except Exception:
            pass
    return report


def nearby_item_lores(
    world: "Neo4jWorld",
    location: Optional[str],
    *,
    limit: int = 6,
) -> List[str]:
    """给 Prompt：当前地点可见物品的行为 lore。"""
    if not location:
        return []
    rows = world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(l:Location {name: $loc})
        WHERE i.script IS NOT NULL
        RETURN i.name AS name, i.script AS script,
               coalesce(i.fuel, null) AS fuel,
               coalesce(i.lit, null) AS lit,
               coalesce(i.item_kind, '') AS kind
        ORDER BY name
        LIMIT $lim
        """,
        loc=location,
        lim=int(limit),
    ) or []
    # 也包括被同地人物持有的？先只做地上/固定物
    lines: List[str] = []
    for r in rows:
        script = parse_script(r.get("script"))
        lore = (script.get("lore") or script.get("summary") or "").strip()
        if not lore:
            continue
        extra = []
        if r.get("fuel") is not None:
            extra.append(f"fuel={r.get('fuel')}")
        if r.get("lit") is not None:
            extra.append(f"lit={r.get('lit')}")
        state = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"- {r.get('name')}{state}: {lore}")
    return lines


def active_script_alerts(world: "Neo4jWorld") -> List[Dict[str, Any]]:
    """当前所有触发中的脚本告警（供决策打断）。"""
    alerts: List[Dict[str, Any]] = []
    for name, script in list_scripted_items(world):
        alerts.extend(_eval_alerts(world, name, script))
    # 紧急优先
    rank = {"critical": 0, "urgent": 1, "info": 2}
    alerts.sort(key=lambda a: rank.get(str(a.get("signal") or "info"), 9))
    return alerts


def alert_to_goal(
    world: "Neo4jWorld",
    character: str,
    alert: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """把脚本告警转成目标候选；具体故事理由用 message，留给 LLM 上下文。"""
    item = alert.get("item")
    if not item:
        return None
    msg = alert.get("message") or f"{item} needs attention"
    prefer_kind = (alert.get("prefer_kind") or "").strip().lower()
    use_on = bool(alert.get("use_on"))

    held = _held_of_kind(world, character, prefer_kind) if prefer_kind else None
    if use_on and held:
        return {
            "goal": "Use item on",
            "target_entity": item,
            "reason": msg,
            "priority": 0.95 if alert.get("signal") == "critical" else 0.9,
            "confidence": 0.88,
            "source": "item_script",
            "tool_hint": held,
            "script_signal": alert.get("signal"),
        }
    if prefer_kind:
        ground = _ground_of_kind(world, prefer_kind)
        if ground:
            return {
                "goal": "Collect item",
                "target_entity": ground,
                "reason": msg,
                "priority": 0.93,
                "confidence": 0.85,
                "source": "item_script",
                "script_signal": alert.get("signal"),
            }
        spawn_at = alert.get("seek_location") or _kind_home(world, prefer_kind)
        if spawn_at:
            return {
                "goal": "Go to location",
                "target_entity": spawn_at,
                "reason": msg,
                "priority": 0.92,
                "confidence": 0.8,
                "source": "item_script",
                "script_signal": alert.get("signal"),
            }
    # 默认走到该物品所在地
    loc = _item_location(world, str(item))
    if loc:
        return {
            "goal": "Go to location",
            "target_entity": loc,
            "reason": msg,
            "priority": 0.9,
            "confidence": 0.75,
            "source": "item_script",
            "script_signal": alert.get("signal"),
        }
    return None


# ----- tick ops -----

def _run_tick_op(
    world: "Neo4jWorld",
    item: str,
    rule: Dict[str, Any],
    dt: float,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    from action import get_property, set_property

    op = (rule.get("op") or "").strip()
    events: List[Dict[str, Any]] = []
    spawned: List[str] = []

    if op == "add_prop":
        prop = rule.get("prop") or ""
        if not prop:
            return events, spawned
        try:
            if "delta_per_sec" in rule:
                delta = float(rule.get("delta_per_sec") or 0) * float(dt)
            else:
                delta = float(rule.get("delta") or 0)
        except (TypeError, ValueError):
            return events, spawned
        cur = get_property(world, item, "Item", prop)
        try:
            cur_n = float(cur if cur is not None else 0)
        except (TypeError, ValueError):
            cur_n = 0.0
        new_v = cur_n + delta
        if rule.get("min") is not None:
            new_v = max(float(rule["min"]), new_v)
        if rule.get("max") is not None:
            new_v = min(float(rule["max"]), new_v)
        if float(new_v).is_integer():
            new_v = int(new_v)
        else:
            new_v = round(new_v, 2)
        set_property(world, item, "Item", prop, new_v)
        return events, spawned

    if op == "derive_bool":
        prop = rule.get("prop") or ""
        from_prop = rule.get("from_prop") or ""
        if not prop or not from_prop:
            return events, spawned
        cur = get_property(world, item, "Item", from_prop)
        try:
            cur_n = float(cur if cur is not None else 0)
        except (TypeError, ValueError):
            cur_n = 0.0
        if rule.get("gt") is not None:
            val = cur_n > float(rule["gt"])
        elif rule.get("gte") is not None:
            val = cur_n >= float(rule["gte"])
        elif rule.get("lt") is not None:
            val = cur_n < float(rule["lt"])
        else:
            val = bool(cur)
        was = bool(get_property(world, item, "Item", prop))
        set_property(world, item, "Item", prop, bool(val))
        if was and not val:
            events.append({"type": "prop_false", "item": item, "prop": prop})
        elif (not was) and val:
            events.append({"type": "prop_true", "item": item, "prop": prop})
        return events, spawned

    if op == "ensure_kind":
        kind = (rule.get("item_kind") or "").strip().lower()
        minimum = int(rule.get("min") or 0)
        at = (rule.get("at") or "").strip()
        prefix = (rule.get("prefix") or kind.capitalize() or "Item").strip()
        spawn_spec = rule.get("spawn") if isinstance(rule.get("spawn"), dict) else {}
        if not kind or not at or minimum <= 0:
            return events, spawned
        if not world.get_entity_by_name(at, label="Location"):
            return events, spawned
        have = _count_kind(world, kind)
        need = max(0, minimum - have)
        for _ in range(need):
            name = _next_name(world, prefix)
            props = {
                "value": int(spawn_spec.get("value") or 5),
                "portable": bool(spawn_spec.get("portable", True)),
                "item_kind": kind,
                "description": str(spawn_spec.get("description") or ""),
            }
            if spawn_spec.get("effects_on") is not None:
                props["effects_on"] = json.dumps(spawn_spec["effects_on"], ensure_ascii=False)
            if spawn_spec.get("script") is not None:
                props["script"] = (
                    spawn_spec["script"]
                    if isinstance(spawn_spec["script"], str)
                    else json.dumps(spawn_spec["script"], ensure_ascii=False)
                )
            world.add_entity(name, entity_type="Item", **props)
            world.link_entities(name, "Item", at, "Location", "LOCATED_AT")
            spawned.append(name)
            events.append({"type": "spawn", "item": name, "at": at, "kind": kind})
        return events, spawned

    return events, spawned


def _eval_alerts(
    world: "Neo4jWorld",
    item: str,
    script: Dict[str, Any],
) -> List[Dict[str, Any]]:
    from action import get_property

    out: List[Dict[str, Any]] = []
    for raw in script.get("alerts") or []:
        if not isinstance(raw, dict):
            continue
        when = raw.get("when") or {}
        if not _when_true(world, item, when):
            continue
        out.append(
            {
                "item": item,
                "signal": raw.get("signal") or "info",
                "message": raw.get("message") or "",
                "prefer_kind": raw.get("prefer_kind") or "",
                "use_on": bool(raw.get("use_on", False)),
                "seek_location": raw.get("seek_location") or "",
                "lore": (script.get("lore") or "").strip(),
            }
        )
    return out


def _when_true(world: "Neo4jWorld", item: str, when: Dict[str, Any]) -> bool:
    from action import get_property

    if not when:
        return True
    for key, spec in when.items():
        if key == "prop_lt" and isinstance(spec, (list, tuple)) and len(spec) >= 2:
            cur = get_property(world, item, "Item", str(spec[0]))
            try:
                return float(cur if cur is not None else 0) < float(spec[1])
            except (TypeError, ValueError):
                return False
        if key == "prop_lte" and isinstance(spec, (list, tuple)) and len(spec) >= 2:
            cur = get_property(world, item, "Item", str(spec[0]))
            try:
                return float(cur if cur is not None else 0) <= float(spec[1])
            except (TypeError, ValueError):
                return False
        if key == "prop_gt" and isinstance(spec, (list, tuple)) and len(spec) >= 2:
            cur = get_property(world, item, "Item", str(spec[0]))
            try:
                return float(cur if cur is not None else 0) > float(spec[1])
            except (TypeError, ValueError):
                return False
        if key == "prop_eq" and isinstance(spec, (list, tuple)) and len(spec) >= 2:
            cur = get_property(world, item, "Item", str(spec[0]))
            return cur == spec[1]
        if key == "prop_true":
            return bool(get_property(world, item, "Item", str(spec)))
        if key == "prop_false":
            return not bool(get_property(world, item, "Item", str(spec)))
    return False


def _count_kind(world: "Neo4jWorld", kind: str) -> int:
    rows = world.run_query(
        """
        MATCH (i:Item)
        WHERE coalesce(i.item_kind, '') = $k
        RETURN count(i) AS n
        """,
        k=kind,
    )
    return int((rows or [{}])[0].get("n") or 0)


def _next_name(world: "Neo4jWorld", prefix: str) -> str:
    for n in range(1, 40):
        name = f"{prefix}{n}"
        if not world.get_entity_by_name(name, label="Item"):
            return name
    n = 1
    while world.get_entity_by_name(f"{prefix}_{n}", label="Item"):
        n += 1
    return f"{prefix}_{n}"


def _held_of_kind(world: "Neo4jWorld", person: str, kind: str) -> Optional[str]:
    if not kind:
        return None
    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})-[:HAS_ITEM]->(i:Item)
        WHERE coalesce(i.item_kind, '') = $k
        RETURN i.name AS name ORDER BY i.name LIMIT 1
        """,
        n=person,
        k=kind,
    )
    return rows[0]["name"] if rows else None


def _ground_of_kind(world: "Neo4jWorld", kind: str) -> Optional[str]:
    rows = world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(:Location)
        WHERE coalesce(i.item_kind, '') = $k AND coalesce(i.portable, true) = true
        RETURN i.name AS name ORDER BY i.name LIMIT 1
        """,
        k=kind,
    )
    return rows[0]["name"] if rows else None


def _item_location(world: "Neo4jWorld", item: str) -> Optional[str]:
    rows = world.run_query(
        "MATCH (i:Item {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1",
        n=item,
    )
    return rows[0]["loc"] if rows else None


def _kind_home(world: "Neo4jWorld", kind: str) -> Optional[str]:
    """从 ensure_kind 脚本里猜再生地点；找不到则 None。"""
    for _name, script in list_scripted_items(world):
        for rule in script.get("on_tick") or []:
            if not isinstance(rule, dict):
                continue
            if rule.get("op") == "ensure_kind" and (rule.get("item_kind") or "").lower() == kind:
                at = (rule.get("at") or "").strip()
                if at:
                    return at
    return None
