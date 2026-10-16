"""
计划冲突 — 谁抢走了我要的东西，以及人物之间由此积累的矛盾（TENSION）。

只看图状态：物品现在在谁手里 / 是否已被用掉；不含具体玩法。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

# 冲突给输家的触发器优先级：高于 INTERRUPT_PRIORITY，会打断 / 促成重新决策
CONFLICT_PRIORITY = 0.85
# 给赢家的：只作为下次决策的背景
RIVAL_PRIORITY = 0.5
MAX_TENSION = 10


def item_names(world: "Neo4jWorld", names: Iterable[str]) -> Set[str]:
    """names 中哪些是图里的 Item。"""
    wanted = sorted({str(n) for n in names if n and not str(n).startswith("kind:")})
    if not wanted:
        return set()
    rows = world.run_query(
        "MATCH (i:Item) WHERE i.name IN $names RETURN i.name AS name", names=wanted
    ) or []
    return {r["name"] for r in rows if r.get("name")}


def item_holder(world: "Neo4jWorld", item: str) -> Optional[str]:
    """物品现在被谁拿着；没人拿 / 物品已不存在都返回 None。"""
    rows = world.run_query(
        "MATCH (h:Person)-[:HAS_ITEM]->(:Item {name: $i}) RETURN h.name AS holder LIMIT 1",
        i=item,
    ) or []
    return rows[0]["holder"] if rows else None


def item_exists(world: "Neo4jWorld", item: str) -> bool:
    return bool(world.run_query("MATCH (i:Item {name: $i}) RETURN 1 AS ok LIMIT 1", i=item))


def add_tension(
    world: "Neo4jWorld",
    who: str,
    toward: str,
    *,
    reason: str,
    at: float,
    delta: int = 1,
) -> int:
    """who 对 toward 的不满 +delta（封顶 MAX_TENSION），返回新等级。"""
    rows = world.run_query(
        """
        MATCH (a:Person {name: $a}), (b:Person {name: $b})
        MERGE (a)-[r:TENSION]->(b)
        SET r.level = CASE WHEN coalesce(r.level, 0) + $d > $cap THEN $cap
                           ELSE coalesce(r.level, 0) + $d END,
            r.reason = $reason, r.at = $at
        RETURN r.level AS level
        """,
        a=who, b=toward, d=int(delta), cap=MAX_TENSION, reason=reason[:200], at=float(at),
    ) or []
    return int(rows[0]["level"]) if rows else 0


def tensions_of(world: "Neo4jWorld", name: str) -> List[Dict[str, Any]]:
    """name 与他人之间的矛盾（双向），等级高的在前。"""
    rows = world.run_query(
        """
        MATCH (a:Person {name: $n})-[r:TENSION]->(b:Person)
        RETURN 'out' AS dir, b.name AS other, r.level AS level, r.reason AS reason
        UNION ALL
        MATCH (b:Person)-[r:TENSION]->(a:Person {name: $n})
        RETURN 'in' AS dir, b.name AS other, r.level AS level, r.reason AS reason
        """,
        n=name,
    ) or []
    out = [r for r in rows if r.get("other") and int(r.get("level") or 0) > 0]
    return sorted(out, key=lambda r: -int(r.get("level") or 0))


def all_tensions(world: "Neo4jWorld", limit: int = 8) -> List[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (a:Person)-[r:TENSION]->(b:Person)
        WHERE coalesce(r.level, 0) > 0
        RETURN a.name AS who, b.name AS toward, r.level AS level, r.reason AS reason
        ORDER BY r.level DESC LIMIT $k
        """,
        k=int(limit),
    ) or []
    return list(rows)


def describe_tensions(world: "Neo4jWorld", name: str, limit: int = 4) -> str:
    lines = []
    for r in tensions_of(world, name)[:limit]:
        level = int(r.get("level") or 0)
        mood = "furious with" if level >= 5 else "annoyed with" if level >= 2 else "a bit sore at"
        if r["dir"] == "out":
            lines.append(f"- You are {mood} {r['other']}: {r.get('reason') or ''}".rstrip(": "))
        else:
            lines.append(f"- {r['other']} has a grudge against you ({r.get('reason') or 'unknown'})")
    return "\n".join(lines)
