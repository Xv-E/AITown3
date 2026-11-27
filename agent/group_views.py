"""
群体给界面看的数据：都由内容包规则给出（content/base/rules/groups.pl 的 group_card/3、membership_summary/2），
编年史直接读图里的 :FactionEvent。这里不含玩法。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from prolog_engine import atom
from rulebook import ask


def cards(world, viewer: str = "") -> List[Dict[str, Any]]:
    """每个群体一张卡片（viewer 在其中的身份、声望、能否加入；成员、地盘、立场、特性），按名字排。"""
    rows = ask(world, "C", f"group(G), group_card({atom(viewer)}, G, C)")
    out = sorted((dict(r) for r in rows if isinstance(r, dict)), key=lambda c: str(c.get("name")))
    for c in out:
        c["events"] = chronicle(world, 3, group=c["name"])
    return out


def chronicle(world, limit: int = 12, *, group: Optional[str] = None) -> List[Dict[str, Any]]:
    """最近的大事（新的在前）。"""
    if group:
        q = ("MATCH (:Faction {name: $g})-[:IN_EVENT]->(e:FactionEvent) "
             "RETURN e.day AS day, e.kind AS kind, e.text AS text, e.t AS t ORDER BY e.t DESC LIMIT $n")
    else:
        q = ("MATCH (e:FactionEvent) RETURN e.day AS day, e.kind AS kind, e.text AS text, e.t AS t, "
             "e.factions AS factions ORDER BY e.t DESC LIMIT $n")
    return [dict(r) for r in world.run_query(q, g=group, n=int(limit)) or []]


def summary(world, person: str) -> str:
    """一行：person 属于哪些群体。"""
    rows = ask(world, "T", f"membership_summary({atom(person)}, T)")
    return str(rows[0]) if rows else ""


def attitude(world, me: str, other: str) -> Dict[str, Any]:
    """me 因为群体关系对 other 的倾向：{"score", "reasons"}。"""
    rows = ask(world, "[S, R]", f"attitude({atom(me)}, {atom(other)}, S, R)")
    if not rows:
        return {"score": 0.0, "reasons": []}
    return {"score": float(rows[0][0]), "reasons": list(rows[0][1])}
