"""
根据角色人设（personality / background / values）做规则化目标选择。

LLM 不可用时的回退；不变的动机写在人设里，不单独依赖 parent_goal。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent_profile import AgentProfile
    from neo4j_world import Neo4jWorld


def _text_hints(*parts: Optional[str]) -> str:
    return " ".join(p.lower() for p in parts if p)


def _goal_priorities(agent: "AgentProfile") -> List[str]:
    """根据人设（personality / background / values）排出目标类型优先级。"""
    blob = _text_hints(
        agent.personality,
        agent.background,
        ",".join(agent.values) if agent.values else "",
        agent.parent_goal,  # 兼容旧世界字段，新世界应为空
    )

    scores: Dict[str, int] = {
        "unlock": 0,
        "explore": 0,
        "social": 0,
        "collect": 0,
        "combat": 0,
    }

    explore_kw = ("explore", "探索", "visit", "unknown", "mystery", "秘密", "角落")
    social_kw = ("meet", "talk", "social", "friend", "information", "交谈", "结识", "信息")
    collect_kw = ("collect", "item", "gather", "pick", "收集", "物品", "钥匙", "装备", "key")
    unlock_kw = ("unlock", "door", "vault", "开门", "开锁", "门", "金库", "use it on")
    combat_kw = ("defeat", "fight", "enemy", "attack", "guard", "击败", "战斗", "敌人")

    for kw in explore_kw:
        if kw in blob:
            scores["explore"] += 2
    for kw in social_kw:
        if kw in blob:
            scores["social"] += 2
    for kw in collect_kw:
        if kw in blob:
            scores["collect"] += 2
    for kw in unlock_kw:
        if kw in blob:
            scores["unlock"] += 2
    for kw in combat_kw:
        if kw in blob:
            scores["combat"] += 2

    personality = (agent.personality or "").lower()
    if any(w in personality for w in ("curious", "explorer", "好奇", "冒险")):
        scores["explore"] += 3
    if any(w in personality for w in ("friendly", "social", "友善", "外向")):
        scores["social"] += 3
    if any(w in personality for w in ("greedy", "collector", "贪心", "囤积")):
        scores["collect"] += 3
    if any(w in personality for w in ("aggressive", "warrior", "brave", "好战", "勇敢")):
        scores["combat"] += 3
    if any(w in personality for w in ("focused", "practical", "practical", "专注")):
        scores["unlock"] += 2
        scores["collect"] += 1

    ordered = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
    if scores[ordered[0]] == 0:
        return ["unlock", "collect", "explore", "social", "combat"]
    return ordered


def _pick_explore(world: "Neo4jWorld", character: str, *, prefer_unvisited: bool) -> Optional[Dict[str, Any]]:
    locations = world.list_entities("Location") or []
    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1",
        n=character,
    )
    cur = rows[0]["loc"] if rows else None
    rows2 = world.run_query(
        "MATCH (p:Person {name: $n})-[:VISITED]->(l:Location) RETURN l.name AS loc",
        n=character,
    )
    visited = {r["loc"] for r in rows2}
    unvisited = [loc for loc in locations if loc not in visited and loc != cur]
    pool = unvisited if prefer_unvisited and unvisited else [loc for loc in locations if loc != cur]
    if not pool:
        return None
    goal = "Explore unknown location" if prefer_unvisited and unvisited else "Go to location"
    return {
        "goal": goal,
        "target_entity": pool[0],
        "reason": f"Based on profile: {'explore unknown area' if goal.startswith('Explore') else 'go to location'} {pool[0]}",
        "priority": 0.9,
        "confidence": 0.75,
    }


def _pick_social(world: "Neo4jWorld", character: str) -> Optional[Dict[str, Any]]:
    persons = [p for p in (world.list_entities("Person") or []) if p != character]
    if not persons:
        return None
    return {
        "goal": "Gain information",
        "target_entity": persons[0],
        "reason": f"Based on profile: talk to {persons[0]} for information",
        "priority": 0.8,
        "confidence": 0.7,
    }


def _pick_collect(world: "Neo4jWorld", character: str) -> Optional[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(:Location)
        WHERE NOT (:Person)-[:HAS_ITEM]->(i)
          AND coalesce(i.item_kind, '') <> 'door'
          AND coalesce(i.portable, true) <> false
        RETURN i.name AS name ORDER BY name
        """
    )
    pickable = [r["name"] for r in rows if r.get("name")]
    if not pickable:
        return None
    return {
        "goal": "Collect item",
        "target_entity": pickable[0],
        "reason": f"Based on profile: collect item {pickable[0]}",
        "priority": 0.75,
        "confidence": 0.65,
    }


def _pick_unlock(world: "Neo4jWorld", character: str) -> Optional[Dict[str, Any]]:
    """持有匹配工具时优先 Use item on；否则先去捡工具（来自世界临时交互）。"""
    try:
        from world_interactions import list_interactions
    except Exception:
        list_interactions = None  # type: ignore

    candidates: List[Dict[str, Any]] = []
    if list_interactions is not None:
        held_rows = world.run_query(
            """
            MATCH (p:Person {name: $c})-[:HAS_ITEM]->(i:Item)
            RETURN i.name AS n
            """,
            c=character,
        )
        held = {r["n"] for r in held_rows if r.get("n")}
        for inter in list_interactions():
            door = inter.target
            key = inter.tool
            rows = world.run_query(
                """
                MATCH (d:Item {name: $d})
                RETURN coalesce(d.locked, false) AS locked,
                       coalesce(d.item_kind, '') AS kind
                LIMIT 1
                """,
                d=door,
            )
            if not rows:
                continue
            if rows[0].get("kind") == "door" and not rows[0].get("locked"):
                continue
            candidates.append({"door": door, "key": key, "held": key in held})
    else:
        rows = world.run_query(
            """
            MATCH (d:Item)
            WHERE coalesce(d.item_kind, '') = 'door' AND coalesce(d.locked, false) = true
            OPTIONAL MATCH (p:Person {name: $c})-[:HAS_ITEM]->(k:Item {name: d.key_item})
            RETURN d.name AS door, d.key_item AS key, k.name AS held
            ORDER BY door
            """,
            c=character,
        )
        for r in rows:
            if r.get("door") and r.get("key"):
                candidates.append({
                    "door": r["door"],
                    "key": r["key"],
                    "held": bool(r.get("held")),
                })

    for r in candidates:
        if r.get("held"):
            return {
                "goal": "Use item on",
                "target_entity": r["door"],
                "reason": f"Based on profile: use {r['key']} on {r['door']}",
                "priority": 0.95,
                "confidence": 0.85,
            }
    for r in candidates:
        key = r.get("key")
        if not key:
            continue
        on_ground = world.run_query(
            """
            MATCH (i:Item {name: $k})-[:LOCATED_AT]->(:Location)
            WHERE NOT (:Person)-[:HAS_ITEM]->(i)
            RETURN i.name AS n LIMIT 1
            """,
            k=key,
        )
        if on_ground:
            return {
                "goal": "Collect item",
                "target_entity": key,
                "reason": f"Based on profile: collect {key} first",
                "priority": 0.92,
                "confidence": 0.8,
            }
    return None


def _pick_combat(world: "Neo4jWorld", character: str) -> Optional[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location)<-[:LOCATED_AT]-(e:Person)
        WHERE e.name <> $n AND coalesce(e.hp, 0) > 0
        RETURN e.name AS name, coalesce(e.power_level, 0) AS pl
        ORDER BY pl
        LIMIT 1
        """,
        n=character,
    )
    if rows and rows[0].get("name"):
        target = rows[0]["name"]
        return {
            "goal": "Defeat enemy",
            "target_entity": target,
            "reason": f"Based on profile: defeat enemy {target}",
            "priority": 0.85,
            "confidence": 0.6,
        }
    persons = [p for p in (world.list_entities("Person") or []) if p != character]
    if not persons:
        return None
    return {
        "goal": "Defeat enemy",
        "target_entity": persons[0],
        "reason": f"Based on profile: challenge {persons[0]}",
        "priority": 0.7,
        "confidence": 0.5,
    }


_PICKERS = {
    "unlock": _pick_unlock,
    "explore": lambda w, c: _pick_explore(w, c, prefer_unvisited=True),
    "social": _pick_social,
    "collect": _pick_collect,
    "combat": _pick_combat,
}


def profile_aware_pick_goal(world: "Neo4jWorld", agent: "AgentProfile") -> Dict[str, Any]:
    """根据 AgentProfile 设定选择下一个目标。"""
    # 锁门任务：有钥匙或可捡钥匙时始终优先两阶段链路
    unlock_first = _pick_unlock(world, agent.name)
    if unlock_first:
        return unlock_first
    for kind in _goal_priorities(agent):
        if kind == "unlock":
            continue
        picker = _PICKERS.get(kind)
        if picker is None:
            continue
        result = picker(world, agent.name)
        if result:
            return result

    locations = world.list_entities("Location") or []
    return {
        "goal": "Go to location",
        "target_entity": locations[0] if locations else None,
        "reason": "Based on profile: default exploration",
        "priority": 0.5,
        "confidence": 0.5,
    }
