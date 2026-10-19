"""
人际关系：好感（可正可负、会回落）、结盟、组队。

  (:Person)-[:FEELS {value, reason, at}]->(:Person)   a 对 b 的好感，-10..10
  (:Person)-[:ALLY {since}]->(:Person)                盟友（双向各一条）
  Person.follows = <leader>                            跟着 leader 走（组队）
  (:Person)-[:MEMBER_OF]->(:Alliance {name, founded})  联盟：结盟的人自然聚成一伙
  (:Alliance)-[:STANCE {value}]->(:Alliance)           联盟之间的关系，-10..10，
                                                       = 两边成员互相好感的平均（每两小时重算）

NPC 对玩家的好感就是原来的 trust_toward_player（同一个数，不另记一份）。

会改变好感的事：送东西 +3、做成一笔买卖 +1、对话里的语气（LLM 给的 -1..+2）、
东西被对方抢先拿走 -2。每过两个游戏小时，所有好感朝 0 回落 1 点（盟友不低于 ALLY_FLOOR），
矛盾（TENSION）也降 1 级。互相好感都 ≥ AUTO_ALLY 的两人自动结盟；好感跌破 0 的盟约破裂。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

MIN_FEEL, MAX_FEEL = -10, 10
GIFT = 3
TRADE = 1
TAKEN = -2
ALLY_MIN = 4        # 提出结盟时，对方对你的好感至少这么多
AUTO_ALLY = 6       # 互相好感都到这么高就自然成了盟友
ALLY_FLOOR = 3      # 盟友之间的好感不会回落到这以下
PARTY_MIN = 2       # 邀请同行时，对方对你的好感至少这么多（盟友总会答应）
DECAY_EVERY = 25.0  # 模拟秒（= 两个游戏小时）
FRIENDLY, RIVAL, HOSTILE = 4, -2, -5   # 联盟之间：友好 / 对立 / 敌对（敌对就不做买卖）


def _is_player(world, name: str) -> bool:
    rows = world.run_query(
        "MATCH (p:Person {name: $n}) RETURN coalesce(p.is_player, false) OR p.role = 'player' AS pl", n=name
    ) or []
    return bool(rows and rows[0].get("pl"))


def feeling(world, a: str, b: str) -> int:
    """a 对 b 的好感。"""
    if _is_player(world, b) and not _is_player(world, a):
        from npc_knowledge import get_trust

        return get_trust(world, a)
    rows = world.run_query(
        "MATCH (:Person {name: $a})-[r:FEELS]->(:Person {name: $b}) RETURN coalesce(r.value, 0) AS v", a=a, b=b
    ) or []
    return int(rows[0]["v"]) if rows else 0


def adjust(world, a: str, b: str, delta: int, *, reason: str = "", at: float = 0.0) -> int:
    """a 对 b 的好感 +delta；返回新值。"""
    if not a or not b or a == b or not delta:
        return feeling(world, a, b) if a and b and a != b else 0
    if _is_player(world, a):
        return 0  # 玩家的感受由玩家自己决定
    if _is_player(world, b):
        from npc_knowledge import adjust_trust

        return adjust_trust(world, a, int(delta))
    rows = world.run_query(
        """
        MATCH (x:Person {name: $a}), (y:Person {name: $b})
        MERGE (x)-[r:FEELS]->(y)
        SET r.value = CASE
              WHEN coalesce(r.value, 0) + $d > $hi THEN $hi
              WHEN coalesce(r.value, 0) + $d < $lo THEN $lo
              ELSE coalesce(r.value, 0) + $d END,
            r.reason = CASE WHEN $why = '' THEN r.reason ELSE $why END,
            r.at = $at
        RETURN r.value AS v
        """,
        a=a, b=b, d=int(delta), lo=MIN_FEEL, hi=MAX_FEEL, why=reason[:160], at=float(at),
    ) or []
    return int(rows[0]["v"]) if rows else 0


def allies_of(world, name: str) -> List[str]:
    rows = world.run_query(
        "MATCH (:Person {name: $n})-[:ALLY]->(o:Person) RETURN o.name AS o ORDER BY o", n=name
    ) or []
    return [r["o"] for r in rows]


def are_allies(world, a: str, b: str) -> bool:
    return b in allies_of(world, a)


def _link_allies(world, a: str, b: str, at: float) -> None:
    world.run_query(
        """
        MATCH (x:Person {name: $a}), (y:Person {name: $b})
        MERGE (x)-[r1:ALLY]->(y) ON CREATE SET r1.since = $at
        MERGE (y)-[r2:ALLY]->(x) ON CREATE SET r2.since = $at
        """,
        a=a, b=b, at=float(at),
    )


def propose_alliance(world, me: str, other: str, *, at: float = 0.0) -> Tuple[bool, str]:
    """me 向 other 提出结盟：other 对 me 的好感够高就答应。"""
    if me == other:
        return False, "You can't ally with yourself"
    if are_allies(world, me, other):
        return True, f"You and {other} are already allies."
    f = feeling(world, other, me)
    if f < ALLY_MIN:
        return False, f"{other} doesn't trust you enough yet (feeling {f}, needs {ALLY_MIN})."
    _link_allies(world, me, other, at)
    adjust(world, other, me, 1, reason="became allies", at=at)
    group = join_together(world, me, other, at=at)
    return True, f"{other} agrees: you are allies now ({group})."


def break_alliance(world, a: str, b: str) -> None:
    world.run_query(
        "MATCH (:Person {name: $a})-[r:ALLY]-(:Person {name: $b}) DELETE r", a=a, b=b
    )


# ----- 组队 -----

def leader_of(world, name: str) -> str:
    rows = world.run_query("MATCH (p:Person {name: $n}) RETURN coalesce(p.follows, '') AS f", n=name) or []
    return str(rows[0]["f"]) if rows else ""


def party_of(world, leader: str) -> List[str]:
    rows = world.run_query(
        "MATCH (p:Person) WHERE p.follows = $l RETURN p.name AS n ORDER BY n", l=leader
    ) or []
    return [r["n"] for r in rows]


def invite(world, leader: str, other: str) -> Tuple[bool, str]:
    """邀请 other 同行：盟友总会来；否则看他对你的好感。"""
    if leader == other:
        return False, "You are already with yourself"
    if leader_of(world, other) == leader:
        return True, f"{other} is already with you."
    f = feeling(world, other, leader)
    if not are_allies(world, leader, other) and f < PARTY_MIN:
        return False, f"{other} would rather not (feeling {f}, needs {PARTY_MIN} or being allies)."
    world.run_query("MATCH (p:Person {name: $n}) SET p.follows = $l", n=other, l=leader)
    return True, f"{other} joins you and will follow you around."


def leave(world, member: str) -> str:
    leader = leader_of(world, member)
    world.run_query("MATCH (p:Person {name: $n}) REMOVE p.follows", n=member)
    return f"{member} leaves {leader or 'the party'} and goes back to their own day."


# ----- 随时间回落；够亲近的自然结盟 -----

def decay(world, *, at: float = 0.0) -> Dict[str, int]:
    """好感朝 0 回落 1 点（盟友不低于 ALLY_FLOOR），矛盾降 1 级；结盟 / 散伙。"""
    world.run_query(
        """
        MATCH (a:Person)-[r:FEELS]->(b:Person)
        OPTIONAL MATCH (a)-[al:ALLY]->(b)
        WITH r, al, coalesce(r.value, 0) AS v
        SET r.value = CASE
              WHEN v > 0 AND (al IS NULL OR v > $floor) THEN v - 1
              WHEN v < 0 THEN v + 1
              ELSE v END
        """,
        floor=ALLY_FLOOR,
    )
    world.run_query(
        """
        MATCH (:Person)-[t:TENSION]->(:Person) WHERE coalesce(t.level, 0) > 0
        SET t.level = t.level - 1
        """
    )
    formed_rows = world.run_query(
        """
        MATCH (a:Person)-[r1:FEELS]->(b:Person)-[r2:FEELS]->(a)
        WHERE a.name < b.name AND r1.value >= $auto AND r2.value >= $auto AND NOT (a)-[:ALLY]->(b)
        MERGE (a)-[x:ALLY]->(b) ON CREATE SET x.since = $at
        MERGE (b)-[y:ALLY]->(a) ON CREATE SET y.since = $at
        RETURN a.name AS a, b.name AS b
        """,
        auto=AUTO_ALLY, at=float(at),
    ) or []
    for r in formed_rows:
        join_together(world, r["a"], r["b"], at=at)
    broken = world.run_query(
        """
        MATCH (a:Person)-[al:ALLY]->(b:Person)
        OPTIONAL MATCH (a)-[r:FEELS]->(b)
        WITH a, b, al, r
        WHERE r IS NOT NULL AND r.value < 0
        DELETE al
        RETURN a.name AS a, b.name AS b
        """
    ) or []
    update_alliances(world)
    return {"formed": len(formed_rows), "broken": len(broken)}


# ----- 给决策 prompt / 客户端看的 -----

def relations_of(world, name: str) -> List[Dict[str, Any]]:
    """name 对别人的好感（以及别人对 name 的），带上盟友 / 同行。"""
    out_rows = world.run_query(
        """
        MATCH (:Person {name: $n})-[r:FEELS]->(o:Person)
        RETURN o.name AS other, coalesce(r.value, 0) AS value, coalesce(r.reason, '') AS reason
        """,
        n=name,
    ) or []
    allies = set(allies_of(world, name))
    leader = leader_of(world, name)
    followers = set(party_of(world, name))
    seen = {r["other"] for r in out_rows}
    rows = [dict(r) for r in out_rows] + [{"other": a, "value": 0, "reason": ""} for a in allies - seen]
    out = []
    for r in rows:
        tags = []
        if r["other"] in allies:
            tags.append("ally")
        if r["other"] == leader:
            tags.append("following them")
        if r["other"] in followers:
            tags.append("follows you")
        out.append({**r, "tags": tags})
    if leader and leader not in {r["other"] for r in out}:
        out.append({"other": leader, "value": feeling(world, name, leader), "reason": "", "tags": ["following them"]})
    out.sort(key=lambda r: (-len(r["tags"]), -abs(int(r["value"]))))
    return out


def word_for(value: int) -> str:
    if value >= 7:
        return "really likes"
    if value >= 3:
        return "likes"
    if value >= 1:
        return "is warming to"
    if value <= -6:
        return "can't stand"
    if value <= -3:
        return "dislikes"
    if value <= -1:
        return "is wary of"
    return "is neutral toward"


def you_feel(value: int) -> str:
    """同 word_for，第二人称（写进 NPC 自己的 prompt）。"""
    if value >= 7:
        return "really like"
    if value >= 3:
        return "like"
    if value >= 1:
        return "are warming to"
    if value <= -6:
        return "can't stand"
    if value <= -3:
        return "dislike"
    if value <= -1:
        return "are wary of"
    return "feel neutral about"


def describe(world, name: str, limit: int = 6) -> str:
    """决策 prompt 用：你喜欢谁、讨厌谁、谁是盟友、你在跟着谁。"""
    lines = []
    for r in relations_of(world, name)[:limit]:
        tags = f" [{', '.join(r['tags'])}]" if r["tags"] else ""
        why = f" ({r['reason']})" if r.get("reason") else ""
        lines.append(f"- You {you_feel(int(r['value']))} {r['other']}{tags}{why}")
    leader = leader_of(world, name)
    if leader:
        lines.append(f"- You are travelling with {leader}: follow them and help with what they ask.")
    group = alliance_text(world, name)
    if group:
        lines.append(group)
    return "\n".join(lines)


# ----- 发生了什么 → 好感怎么变（玩家和 NPC 共用） -----

def after_action(world, actor: str, action: str, params: Dict[str, str], *, at: float = 0.0) -> None:
    from content import get_content

    cap = get_content().capability_of(action) or action
    if action in ("buy", "sell"):
        other = params.get("seller") or params.get("buyer") or ""
        adjust(world, other, actor, TRADE, reason="a fair deal", at=at)
        adjust(world, actor, other, TRADE, reason="a fair deal", at=at)
    elif cap == "give":
        receiver = params.get("receiver") or ""
        adjust(world, receiver, actor, GIFT, reason=f"gave me {params.get('i', 'something')}", at=at)
        from job_board import on_give

        if world is not None:
            on_give(world, actor, receiver, params.get("i") or "", at=at)  # 交的是他求的东西：委托完成、付酬劳
    elif action == "propose_alliance":
        other = params.get("other") or ""
        adjust(world, other, actor, 1, reason="became allies", at=at)
        join_together(world, actor, other, at=at)
    if action == "buy" and params.get("i") and world is not None:
        from economy import add_demand, kind_of

        add_demand(world, kind_of(world, params["i"]), 1.0)  # 有人买 → 这类东西更抢手
    if world is not None:
        consequences(world, actor, action, params, at=at)


def consequences(world, actor: str, action: str, params: Dict[str, str], *, at: float = 0.0) -> Dict[str, Any]:
    """任何动作做完之后：东西易手 / 认领 / 偷窃（ownership.py）；打人的后果（combat.py）。"""
    from content import get_content
    from ownership import after_action as ownership_after

    out = dict(ownership_after(world, actor, action, params, at=at) or {})
    if (get_content().capability_of(action) or action) == "attack" and params.get("target"):
        from combat import after_attack

        out.update(after_attack(world, actor, params["target"], at=at))
    return out


# ----- 联盟（一伙人）-----

def alliance_of(world, name: str) -> str:
    rows = world.run_query(
        "MATCH (:Person {name: $n})-[:MEMBER_OF]->(a:Alliance) RETURN a.name AS a LIMIT 1", n=name
    ) or []
    return str(rows[0]["a"]) if rows else ""


def members(world, alliance: str) -> List[str]:
    rows = world.run_query(
        "MATCH (p:Person)-[:MEMBER_OF]->(:Alliance {name: $a}) RETURN p.name AS n ORDER BY n", a=alliance
    ) or []
    return [r["n"] for r in rows]


def join_together(world, a: str, b: str, *, at: float = 0.0) -> str:
    """a、b 刚结盟：都没有联盟就新成立一个；一方有就把另一方拉进来；两边各有联盟就让两个联盟更亲近。"""
    ga, gb = alliance_of(world, a), alliance_of(world, b)
    if ga and gb:
        if ga != gb:
            set_stance(world, ga, gb, max(stance(world, ga, gb), FRIENDLY))
        return ga
    if ga or gb:
        group, newcomer = (ga, b) if ga else (gb, a)
        world.run_query(
            "MATCH (p:Person {name: $n}), (g:Alliance {name: $g}) MERGE (p)-[:MEMBER_OF]->(g)", n=newcomer, g=group
        )
        return group
    group = f"{a}'s Band"
    world.run_query(
        """
        MERGE (g:Alliance {name: $g}) ON CREATE SET g.founded = $at, g.founder = $a
        WITH g
        MATCH (x:Person {name: $a}), (y:Person {name: $b})
        MERGE (x)-[:MEMBER_OF]->(g)
        MERGE (y)-[:MEMBER_OF]->(g)
        """,
        g=group, a=a, b=b, at=float(at),
    )
    return group


def stance(world, a: str, b: str) -> int:
    rows = world.run_query(
        "MATCH (:Alliance {name: $a})-[r:STANCE]->(:Alliance {name: $b}) RETURN coalesce(r.value, 0) AS v", a=a, b=b
    ) or []
    return int(rows[0]["v"]) if rows else 0


def set_stance(world, a: str, b: str, value: int) -> None:
    value = max(MIN_FEEL, min(MAX_FEEL, int(value)))
    world.run_query(
        """
        MATCH (x:Alliance {name: $a}), (y:Alliance {name: $b})
        MERGE (x)-[r1:STANCE]->(y) SET r1.value = $v
        MERGE (y)-[r2:STANCE]->(x) SET r2.value = $v
        """,
        a=a, b=b, v=value,
    )


def stance_word(value: int) -> str:
    if value >= FRIENDLY:
        return "friendly"
    if value <= HOSTILE:
        return "hostile"
    if value <= RIVAL:
        return "rivals"
    return "neutral"


def hostile(world, a: str, b: str) -> bool:
    """a、b 属于互相敌对的两个联盟。"""
    ga, gb = alliance_of(world, a), alliance_of(world, b)
    return bool(ga and gb and ga != gb and stance(world, ga, gb) <= HOSTILE)


def update_alliances(world) -> None:
    """不再喜欢同伴的人退出；少于两人的联盟解散；联盟之间的关系 = 两边成员互相好感的平均。"""
    for g in [r["g"] for r in world.run_query("MATCH (g:Alliance) RETURN g.name AS g") or []]:
        people = members(world, g)
        for p in people:
            others = [o for o in people if o != p]
            if others and sum(feeling(world, p, o) for o in others) / len(others) < 0:
                world.run_query(
                    "MATCH (:Person {name: $p})-[m:MEMBER_OF]->(:Alliance {name: $g}) DELETE m", p=p, g=g
                )
        if len(members(world, g)) < 2:
            world.run_query("MATCH (g:Alliance {name: $g}) DETACH DELETE g", g=g)
    groups = {r["g"]: members(world, r["g"]) for r in world.run_query("MATCH (g:Alliance) RETURN g.name AS g") or []}
    names = sorted(groups)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            pairs = [(x, y) for x in groups[a] for y in groups[b]]
            if not pairs:
                continue
            mean = sum(feeling(world, x, y) + feeling(world, y, x) for x, y in pairs) / (2 * len(pairs))
            set_stance(world, a, b, round(mean))


def alliances_view(world) -> List[Dict[str, Any]]:
    """导演选项卡：每个联盟、成员、和别的联盟的关系。"""
    out = []
    names = [r["g"] for r in world.run_query("MATCH (g:Alliance) RETURN g.name AS g ORDER BY g") or []]
    for g in names:
        out.append({
            "name": g,
            "members": members(world, g),
            "stances": [
                {"other": o, "value": stance(world, g, o), "word": stance_word(stance(world, g, o))}
                for o in names if o != g
            ],
        })
    return out


def alliance_text(world, name: str) -> str:
    g = alliance_of(world, name)
    if not g:
        return ""
    mates = [m for m in members(world, g) if m != name]
    lines = [f"- You belong to {g} with {', '.join(mates) or 'nobody else yet'}: stand by them."]
    for s in alliances_view(world):
        if s["name"] != g:
            continue
        for st in s["stances"]:
            lines.append(f"- {g} and {st['other']} are {st['word']} ({st['value']}).")
    return "\n".join(lines)
