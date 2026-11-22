"""
社会规则的 Python 入口：从 Neo4j 读当下的事实，交给 rules/social.pl 推理。

  hostility(world, a, b)            a 为什么把 b 当敌人（标签，"" = 不是）——combat.hostility 用它
  why_hostile(world, a, b)          同上，带原因细节和一句话解释（给提示词 / 界面）
  hostilities(world, pairs)         一次算很多对（只读一次图）
  is_theft(world, taker, item)      拿走算不算偷：算的话返回失主
  foresee_take(world, actor, item)  如果 actor 现在拿走 item：算不算偷、谁会因此敌视他

规则本身（谁算敌人、什么算偷）只写在 rules/social.pl；这里只负责搬运事实和翻译结果。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from prolog_engine import atom, fact, solve

# 原因（Prolog 函子）→ combat 一直在用的标签
LABELS = {
    "attacked_me": "attacked me",
    "fighting": "fighting",
    "faction": "faction",
    "criminal": "criminal",
    "hatred": "hatred",
    "war": "war",
}


def _params() -> Dict[str, float]:
    """Python 里别处也在用的常量，以事实的形式交给规则（只有一份定义）。"""
    from combat import COMBAT_MEMORY
    from ownership import THEFT_VICTIM, THEFT_WITNESS
    from relations import HOSTILE

    return {
        "combat_memory": float(COMBAT_MEMORY),
        "alliance_hostile": int(HOSTILE),
        "theft_victim": int(THEFT_VICTIM),
        "theft_witness": int(THEFT_WITNESS),
    }


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def social_facts(world, *, at: Optional[float] = None, items: Sequence[str] = (),
                 sees: Iterable[Tuple[str, str]] = ()) -> List[str]:
    """当下与敌对 / 偷窃有关的事实。items：要判断归属的物品；sees：(看的人, 被看的人)。"""
    facts: List[str] = []
    rows = world.run_query(
        """
        MATCH (p:Person)
        OPTIONAL MATCH (p)-[:MEMBER_OF]->(g:Alliance)
        RETURN p.name AS n, coalesce(p.dead, false) AS dead, coalesce(p.hp, 0) AS hp,
               coalesce(p.faction, '') AS faction, coalesce(p.guard, false) AS guard,
               coalesce(p.is_player, false) OR p.role = 'player' AS player,
               coalesce(p.trust_toward_player, 0) AS trust,
               p.attacked_by AS attacked_by, p.attacked_at AS attacked_at,
               p.fighting AS fighting, p.fighting_at AS fighting_at,
               collect(g.name)[0] AS alliance
        """
    ) or []
    for r in rows:
        n = r.get("n")
        if not n:
            continue
        facts.append(fact("person", n))
        if not r.get("dead") and int(r.get("hp") or 0) > 0:
            facts.append(fact("alive", n))
        if r.get("player"):
            facts.append(fact("player", n))
        if r.get("guard"):
            facts.append(fact("guard", n))
        facts.append(fact("faction", n, str(r.get("faction") or "")))
        facts.append(fact("trust", n, int(r.get("trust") or 0)))
        if r.get("alliance"):
            facts.append(fact("in_alliance", n, r["alliance"]))
        t = _num(r.get("attacked_at"))
        if r.get("attacked_by") and t is not None:
            facts.append(fact("attacked_by", n, r["attacked_by"], t))
        t = _num(r.get("fighting_at"))
        if r.get("fighting") and t is not None:
            facts.append(fact("fighting", n, r["fighting"], t))

    for r in world.run_query(
        "MATCH (a:Person)-[r:FEELS]->(b:Person) RETURN a.name AS a, b.name AS b, coalesce(r.value, 0) AS v"
    ) or []:
        facts.append(fact("feels", r["a"], r["b"], int(r["v"] or 0)))
    for r in world.run_query("MATCH (a:Person)-[:ALLY]->(b:Person) RETURN a.name AS a, b.name AS b") or []:
        facts.append(fact("ally", r["a"], r["b"]))
    for r in world.run_query(
        "MATCH (a:Alliance)-[r:STANCE]->(b:Alliance) RETURN a.name AS a, b.name AS b, coalesce(r.value, 0) AS v"
    ) or []:
        facts.append(fact("stance", r["a"], r["b"], int(r["v"] or 0)))
    for r in world.run_query(
        """
        MATCH (o:Person)-[r:WITNESSED]->(c:Person)
        RETURN o.name AS o, c.name AS c, coalesce(r.crime, '') AS crime,
               coalesce(r.victim, '') AS victim, coalesce(r.item, '') AS item
        """
    ) or []:
        facts.append(fact("witnessed", r["o"], r["c"], r["crime"], r["victim"], r["item"]))
    if items:
        for r in world.run_query(
            "MATCH (p:Person)-[:OWNS]->(i:Item) WHERE i.name IN $items RETURN p.name AS p, i.name AS i",
            items=list(items),
        ) or []:
            facts.append(fact("owns", r["p"], r["i"]))
    for w, p in sees:
        facts.append(fact("sees", w, p))

    if at is None:
        from combat import now

        at = now(world)
    facts.append(fact("now", float(at)))
    facts += [fact("param", k, v) for k, v in _params().items()]
    return facts


# ---------------------------------------------------------------------
# 敌对
# ---------------------------------------------------------------------

def _reason(kind: str, args: List[Any]) -> Dict[str, Any]:
    return {"label": LABELS.get(kind, kind), "kind": kind, "args": list(args)}


def explain(reason: Dict[str, Any], a: str, b: str) -> str:
    """把原因说成一句话（从 a 的角度说 b）。"""
    kind, args = reason.get("kind"), reason.get("args") or []
    if kind == "attacked_me":
        return f"{b} attacked {a} recently"
    if kind == "fighting":
        return f"{a} is in a fight with {b}"
    if kind == "faction":
        fa, fb = (list(args) + ["", ""])[:2]
        return f"{a} ({fa or 'no faction'}) and {b} ({fb or 'no faction'}) are on sides that fight on sight"
    if kind == "criminal":
        crime, victim, item = (list(args) + ["", "", ""])[:3]
        what = crime or "a crime"
        if victim and item:
            what += f" ({victim}'s {item})"
        elif victim:
            what += f" against {victim}"
        return f"{a} is a guard and saw {b} commit {what}"
    if kind == "hatred":
        return f"{a} hates {b} (feeling {args[0] if args else '?'})"
    if kind == "war":
        ga, gb = (list(args) + ["", ""])[:2]
        return f"{a}'s alliance {ga} is hostile to {b}'s alliance {gb}"
    return f"{a} is hostile to {b}"


def hostilities(world, pairs: Iterable[Tuple[str, str]], *, at: Optional[float] = None
                ) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """{(a, b): 原因}，只含敌对的那些对；原因 = {label, kind, args, text}。"""
    pairs = [(a, b) for a, b in pairs if a and b and a != b]
    if not pairs:
        return {}
    plist = ", ".join(f"[{atom(a)}, {atom(b)}]" for a, b in pairs)
    rows = solve(
        social_facts(world, at=at),
        f"findall([A, B, K, Args], (member([A, B], [{plist}]), hostility(A, B, W), W =.. [K|Args]), L)",
    )
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for a, b, kind, args in (rows[0]["L"] if rows else []):
        r = _reason(str(kind), args)
        r["text"] = explain(r, str(a), str(b))
        out[(str(a), str(b))] = r
    return out


def why_hostile(world, a: str, b: str, *, at: Optional[float] = None) -> Optional[Dict[str, Any]]:
    return hostilities(world, [(a, b)], at=at).get((a, b))


def hostility(world, a: str, b: str, *, at: Optional[float] = None) -> str:
    r = why_hostile(world, a, b, at=at)
    return r["label"] if r else ""


# ---------------------------------------------------------------------
# 偷窃
# ---------------------------------------------------------------------

def is_theft(world, taker: str, item: str) -> str:
    if not taker or not item:
        return ""
    rows = solve(social_facts(world, at=0.0, items=[item]),
                 f"theft({atom(taker)}, {atom(item)}, Owner)")
    return str(rows[0]["Owner"]) if rows else ""


def foresee_take(world, actor: str, item: str, *, at: Optional[float] = None,
                 watchers: Optional[List[str]] = None) -> Dict[str, Any]:
    """如果 actor 现在拿走 item 会怎样：{"theft", "victim", "turns_hostile": [{who, label, text}]}。
    watchers：看得见 actor 的人（默认按 ownership.witnesses 现算）。"""
    if watchers is None:
        from ownership import witnesses

        watchers = witnesses(world, actor)
    rows = solve(
        social_facts(world, at=at, items=[item], sees=[(w, actor) for w in watchers]),
        f"findall(C, (consequence(take({atom(actor)}, {atom(item)}), E), "
        f"(E = theft(V) -> C = [theft, V] ; E = turns_hostile(W, Why), Why =.. [K|Args], C = [W, K, Args])), L)",
    )
    out: Dict[str, Any] = {"theft": False, "victim": "", "turns_hostile": []}
    for c in (rows[0]["L"] if rows else []):
        if len(c) == 2 and str(c[0]) == "theft":
            out["theft"], out["victim"] = True, str(c[1])
        elif len(c) == 3:
            who = str(c[0])
            r = _reason(str(c[1]), c[2])
            out["turns_hostile"].append({"who": who, "label": r["label"],
                                         "text": f"{who} would turn hostile ({explain(r, who, actor)})"})
    return out
