"""
物品归属与目击的读写小工具，以及引擎的感知 witnesses()（离得够近、没隔着墙）。

  (:Person)-[:OWNS]->(:Item)                     这件东西是他的（拿在谁手上、放在哪都不变）
  (:Person)-[:WITNESSED {crime, victim, item, at}]->(:Person)   亲眼看见谁干了坏事

东西怎么变成谁的、什么算偷、谁记下什么，都是内容包规则（content/base/rules/ownership.pl、social.pl）。
主人死了（combat.die）→ 他的东西都没主了。
"""

from __future__ import annotations

from typing import Any, Dict, List

def owner_of(world, item: str) -> str:
    rows = world.run_query(
        "MATCH (p:Person)-[:OWNS]->(:Item {name: $i}) RETURN p.name AS p LIMIT 1", i=item
    ) or []
    return str(rows[0]["p"]) if rows else ""


def set_owner(world, item: str, person: str) -> None:
    world.run_query("MATCH (:Person)-[r:OWNS]->(:Item {name: $i}) DELETE r", i=item)
    if person:
        world.run_query(
            "MATCH (p:Person {name: $p}), (i:Item {name: $i}) MERGE (p)-[:OWNS]->(i)", p=person, i=item
        )


def release_all(world, person: str) -> None:
    """人死了：他的东西都没主了。"""
    world.run_query("MATCH (:Person {name: $p})-[r:OWNS]->(:Item) DELETE r", p=person)


def witnesses(world, actor: str) -> List[str]:
    """看得见 actor 的人（不算他自己）。"""
    try:
        from spatial import people_near

        near = people_near(world, actor) or []
    except Exception:  # noqa: BLE001  没有地形的老世界：同一地点的人都看得见
        rows = world.run_query(
            """
            MATCH (:Person {name: $a})-[:LOCATED_AT]->(l:Location)<-[:LOCATED_AT]-(p:Person)
            WHERE p.name <> $a AND coalesce(p.dead, false) = false
            RETURN p.name AS n
            """,
            a=actor,
        ) or []
        near = [r["n"] for r in rows]
    return [n for n in near if n != actor]


def crimes_seen(world, observer: str, culprit: str) -> List[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (:Person {name: $o})-[r:WITNESSED]->(:Person {name: $c})
        RETURN r.crime AS crime, r.victim AS victim, r.item AS item, r.at AS at ORDER BY r.at
        """,
        o=observer, c=culprit,
    ) or []
    return [dict(r) for r in rows]


def item_owners(world, items: List[str]) -> Dict[str, str]:
    if not items:
        return {}
    rows = world.run_query(
        "MATCH (p:Person)-[:OWNS]->(i:Item) WHERE i.name IN $items RETURN i.name AS i, p.name AS p",
        items=list(items),
    ) or []
    return {r["i"]: r["p"] for r in rows}
