"""
人际关系：好感（可正可负、会回落）、结盟、组队。

  (:Person)-[:FEELS {value, reason, at}]->(:Person)   a 对 b 的好感，-10..10
  (:Person)-[:ALLY {since}]->(:Person)                盟友（双向各一条）
  Person.follows = <leader>                            跟着 leader 走（组队）
  (:Person)-[:MEMBER_OF]->(:Faction {kind: band})     结盟的人自然聚成一伙（群体，见 content/base/rules/groups.pl）
  (:Faction)-[:FACTION_REL {stance, value}]-(:Faction) 两伙之间的立场 = 两边成员互相好感的平均（每两小时重算）

NPC 对玩家的好感就是原来的 trust_toward_player（同一个数，不另记一份）。

会改变好感的事：动作之后的（送礼、买卖、偷窃、打人……）都是内容包规则（content/base/rules/），
经 feel op 走 adjust()；对话里的语气（LLM 给的 -1..+2）；东西被对方抢先拿走 -2。
好感随时间回落、矛盾消退：规则 tick/1（content/base/rules/time.pl）。
结盟 / 散伙 / 一伙人的成员与立场：规则（content/base/rules/group_life.pl）；这里只剩问规则的小工具和界面文字。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

MIN_FEEL, MAX_FEEL = -10, 10
TAKEN = -2
PARTY_MIN = 2       # 邀请同行时，对方对你的好感至少这么多（盟友总会答应）


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


def propose_alliance(world, me: str, other: str, *, at: float = 0.0) -> Tuple[bool, str]:
    """me 向 other 提出结盟：就是做一次内容包的 propose_alliance 动作（条件、效果、之后的事都在内容里）。"""
    from graph_ops import render_text
    from registry import get_registry

    if are_allies(world, me, other):
        return True, f"You and {other} are already allies."
    rows = world.run_query("MATCH (:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS l", n=me) or []
    params = {"c": me, "other": other, "l": rows[0]["l"] if rows else ""}
    ok, why = get_registry().execute(world, "propose_alliance", **params)
    if not ok:
        return False, why
    consequences(world, me, "propose_alliance", params, at=at)
    from content import get_content

    spec = get_content().actions.get("propose_alliance") or {}
    return True, render_text(str(spec.get("narrative") or f"{other} agrees: you are allies now."), params)


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

def after_action(world, actor: str, action: str, params: Dict[str, str], *, at: float = 0.0) -> Dict[str, Any]:
    from content import get_content

    cap = get_content().capability_of(action) or action
    if cap == "give":  # 送礼、买卖带来的好感见 content/base/rules/ownership.pl
        receiver = params.get("receiver") or ""
        from job_board import on_give

        if world is not None:
            on_give(world, actor, receiver, params.get("i") or "", at=at)  # 交的是他求的东西：委托完成、付酬劳
    if world is None:
        return {}
    return consequences(world, actor, action, params, at=at)  # 有人买 → 更抢手、上架……都是规则


def consequences(world, actor: str, action: str, params: Dict[str, str], *, at: float = 0.0) -> Dict[str, Any]:
    """任何动作做完之后的后果：全部由内容包规则给出（after/2，见 content/*/rules），引擎照着写图。
    返回规则的 report 合并结果，以及 notes（要告诉做的人的话）。"""
    from rulebook import after as rule_after

    return dict(rule_after(world, action, params, at=at) or {})


# ----- 一伙人（mood_bound 的群体，如朋友结的伙）-----

def alliance_of(world, name: str) -> str:
    from prolog_engine import atom
    from rulebook import ask

    rows = ask(world, "G", f"band_of({atom(name)}, G)")
    return str(rows[0]) if rows else ""


def members(world, alliance: str) -> List[str]:
    from prolog_engine import atom
    from rulebook import ask

    return sorted(str(p) for p in ask(world, "P", f"direct(P, {atom(alliance)})"))


def stance_word(value: int) -> str:
    """两伙人之间的立场说成词（阈值是规则 stance_word/2，见 content/base/rules/group_life.pl）。"""
    from prolog_engine import atom
    from rulebook import pure

    words = pure("W", f"stance_word({atom(int(value))}, W)")
    return str(words[0]) if words else "neutral"


def alliances_view(world) -> List[Dict[str, Any]]:
    """导演选项卡：每一伙人、成员、和别的伙的关系。"""
    from rulebook import ask

    rows = ask(world, "[G, Ms, Ss]",
               "group(G), has(G, mood_bound), findall(P, direct(P, G), Ms), "
               "findall([O, V, W], (group(O), O \\== G, has(O, mood_bound), group_rel(G, O, Ps), "
               "kv(Ps, value, 0, V), kv(Ps, stance, neutral, W)), Ss)")
    return [{"name": g, "members": sorted(ms),
             "stances": [{"other": o, "value": int(v), "word": w} for o, v, w in ss]}
            for g, ms, ss in sorted(rows)]


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
