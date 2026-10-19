"""
物品归属：谁的东西、怎么易手、拿别人的东西就是偷。

  (:Person)-[:OWNS]->(:Item)                     这件东西是他的（拿在谁手上、放在哪都不变）
  (:Person)-[:WITNESSED {crime, victim, item, at}]->(:Person)   亲眼看见谁干了坏事

怎么变成谁的：
  · 捡起没主的东西、采集、做出来、一开始就拿着 → 归拿着的人（claim_held 一扫就有）
  · 送人 / 卖掉 / 买来 → 归新主人（前提是原来就是给的人的；偷来的东西转手还是原主的）
  · 主人死了 → 他的东西都没主了，谁捡归谁

偷：拿走（take 能力）一件还活着的别人的东西，而且主人不是你的盟友。
看见的人（离得够近、没隔着墙，见 spatial.people_near）记下这件事；失主对小偷好感 -5，旁观者 -2。
守卫看见了会把小偷当敌人（见 combat.hostility）。把东西还给失主（give）好感会回来一些。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

THEFT_VICTIM = -5
THEFT_WITNESS = -2
RETURNED = 3


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


def claim_held(world, person: str) -> List[str]:
    """手上没主的东西归他（采集、做出来、捡到的）。返回新认领的。"""
    rows = world.run_query(
        """
        MATCH (p:Person {name: $p})-[:HAS_ITEM]->(i:Item)
        WHERE NOT (:Person)-[:OWNS]->(i)
        MERGE (p)-[:OWNS]->(i)
        RETURN i.name AS i
        """,
        p=person,
    ) or []
    return [r["i"] for r in rows]


def release_all(world, person: str) -> None:
    """人死了：他的东西都没主了。"""
    world.run_query("MATCH (:Person {name: $p})-[r:OWNS]->(:Item) DELETE r", p=person)


def _alive(world, name: str) -> bool:
    rows = world.run_query(
        "MATCH (p:Person {name: $n}) RETURN coalesce(p.dead, false) = false AND coalesce(p.hp, 0) > 0 AS ok",
        n=name,
    ) or []
    return bool(rows and rows[0].get("ok"))


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


def record_crime(world, culprit: str, crime: str, *, victim: str = "", item: str = "",
                 seen_by: Optional[List[str]] = None, at: float = 0.0) -> List[str]:
    """记下谁看见了这件坏事；返回看见的人。"""
    seen = list(seen_by) if seen_by is not None else witnesses(world, culprit)
    for w in seen:
        world.run_query(
            """
            MATCH (w:Person {name: $w}), (c:Person {name: $c})
            CREATE (w)-[:WITNESSED {crime: $crime, victim: $victim, item: $item, at: $at}]->(c)
            """,
            w=w, c=culprit, crime=crime, victim=victim, item=item, at=float(at),
        )
    if seen:
        world.run_query(
            "MATCH (c:Person {name: $c}) SET c.crimes = coalesce(c.crimes, 0) + 1", c=culprit
        )
    return seen


def crimes_seen(world, observer: str, culprit: str) -> List[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (:Person {name: $o})-[r:WITNESSED]->(:Person {name: $c})
        RETURN r.crime AS crime, r.victim AS victim, r.item AS item, r.at AS at ORDER BY r.at
        """,
        o=observer, c=culprit,
    ) or []
    return [dict(r) for r in rows]


def is_theft(world, taker: str, item: str) -> str:
    """taker 拿走 item 算不算偷：算的话返回失主。"""
    owner = owner_of(world, item)
    if not owner or owner == taker or not _alive(world, owner):
        return ""
    try:
        from relations import are_allies

        if are_allies(world, owner, taker):
            return ""
    except Exception:  # noqa: BLE001
        pass
    return owner


def after_action(world, actor: str, action: str, params: Dict[str, str], *, at: float = 0.0) -> Dict[str, Any]:
    """动作做完之后：东西易手、认领、偷窃（归属只在这里改，所以这时读到的还是动作前的主人）。"""
    from content import get_content
    from relations import adjust

    out: Dict[str, Any] = {}
    cap = get_content().capability_of(action) or action
    item = params.get("i") or ""
    before = owner_of(world, item) if item else ""
    if cap == "take" and item:
        victim = before if before and before != actor and _alive(world, before) else ""
        if victim:
            try:
                from relations import are_allies

                if are_allies(world, victim, actor):
                    victim = ""
            except Exception:  # noqa: BLE001
                pass
        if victim:
            seen = record_crime(world, actor, "theft", victim=victim, item=item, at=at)
            if victim in seen:
                adjust(world, victim, actor, THEFT_VICTIM, reason=f"stole my {item}", at=at)
            for w in seen:
                if w != victim:
                    adjust(world, w, actor, THEFT_WITNESS, reason=f"saw them steal {victim}'s {item}", at=at)
            out = {"theft": True, "victim": victim, "witnesses": seen}
    elif cap == "give" and item:
        receiver = params.get("receiver") or ""
        if before == actor:
            set_owner(world, item, receiver)
        elif before and before == receiver:
            adjust(world, receiver, actor, RETURNED, reason=f"gave my {item} back", at=at)
    elif action in ("buy", "sell") and item:
        buyer = actor if action == "buy" else params.get("buyer") or ""
        if buyer:
            set_owner(world, item, buyer)
    claim_held(world, actor)
    return out


def item_owners(world, items: List[str]) -> Dict[str, str]:
    if not items:
        return {}
    rows = world.run_query(
        "MATCH (p:Person)-[:OWNS]->(i:Item) WHERE i.name IN $items RETURN i.name AS i, p.name AS p",
        items=list(items),
    ) or []
    return {r["i"]: r["p"] for r in rows}
