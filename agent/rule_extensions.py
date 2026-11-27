"""
规则的扩展（效果 {"op": "extend", "name": ...}）：规则做不了、只能交给引擎的事。

  new_group_story   新群体出现（分裂 / 结盟另立门户 / 外来者）：LLM 按世界里的人和事写名字、目标、来历、特性。
                    规则先给一版模板故事（story/2），LLM 写得合格就逐项替换；
                    写好后交回规则 founding/3，由规则把新群体写进世界。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from prolog_engine import atom
from rulebook import ask, extension

logger = logging.getLogger(__name__)

STORY_SYSTEM = """You write faction history for a small living RPG world.
A new faction has just emerged. Ground everything in the people, places and history given.
Its traits must come from its story: every trait needs a "why" that points at a concrete event or person.
Output ONLY valid JSON."""


@extension("new_group_story")
def new_group_story(world, effect: Dict[str, Any]) -> List[Any]:
    ctx = dict(effect.get("ctx") or {})
    base = ask(world, "S", f"once(story({atom(ctx)}, S))")
    if not base:
        return []
    story = _improve(world, ctx, dict(base[0]))
    story["name"] = _unique_name(world, str(story.get("name") or "NewFaction"))
    return ask(world, "E", f"founding({atom(ctx)}, {atom(story)}, E)")


def _improve(world, ctx: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    if os.getenv("FACTION_STORY_LLM", "1").strip().lower() in ("0", "false", "no"):
        return base
    try:
        from llm_router import invoke_primary, primary_available

        if not primary_available():
            return base
        raw = invoke_primary(_prompt(world, ctx), system=STORY_SYSTEM, max_tokens=500,
                             timeout=float(os.getenv("FACTION_STORY_TIMEOUT", "25") or 25))
    except Exception as e:  # noqa: BLE001
        logger.info("group story LLM failed: %s", e)
        return base
    data = _parse_json(raw if isinstance(raw, str) else str(raw))
    if not data:
        return base
    traits = dict(ask(world, "[T, X]", "trait_text(T, X)"))
    kinds = _kinds(world)
    groups = set(ask(world, "G", "group(G)"))
    out = dict(base)
    if data.get("name"):
        out["name"] = str(data["name"])
    if str(data.get("kind") or "") in kinds:
        out["kind"] = data["kind"]
    for k in ("goal", "description", "chronicle"):
        if str(data.get(k) or "").strip():
            out[k] = str(data[k]).strip()[:300]
    picked = [{"id": str(t.get("id")), "why": str(t.get("why") or "")[:160]}
              for t in data.get("traits") or [] if isinstance(t, dict) and t.get("id") in traits][:3]
    if picked:
        out["traits"] = picked
    rels = [{"with": r["with"], "stance": r["stance"], "reason": str(r.get("reason") or "")[:160]}
            for r in data.get("relations") or []
            if isinstance(r, dict) and r.get("with") in groups
            and r.get("stance") in ("allied", "friendly", "neutral", "rival", "hostile")]
    if rels or not base.get("relations"):
        out["relations"] = rels or base.get("relations") or []
    return out


def _kinds(world) -> List[str]:
    """LLM 可以选的种类：规则里写了默认特性、又不是野兽 / 临时结伙那种的。"""
    return sorted({str(k) for k in ask(world, "K", "kind_traits(K, Ts), atom(K), \\+ memberchk(informal, Ts)")})


def _prompt(world, ctx: Dict[str, Any]) -> str:
    def one(cypher: str, **params) -> Dict[str, Any]:
        rows = world.run_query(cypher, **params) or []
        return dict(rows[0]) if rows else {}

    premise = one("MATCH (m {name: 'WorldMeta'}) WHERE m.premise IS NOT NULL RETURN m.premise AS p").get("p") or ""
    lines = [f"World: {premise}", f"What happened: {ctx.get('type')} at {ctx.get('location') or '(somewhere)'}"]
    for p in ctx.get("founders") or []:
        prof = one(
            "MATCH (p:Person {name: $p}) RETURN coalesce(p.personality, '') AS personality, "
            "coalesce(p.background, '') AS background, coalesce(p.desire, '') AS desire, coalesce(p.fear, '') AS fear",
            p=p,
        )
        lines.append(f"Founder {p}: {prof.get('personality')}; {prof.get('background')}; "
                     f"longs for {prof.get('desire') or '?'}; fears {prof.get('fear') or '?'}")
    parent = ctx.get("parent")
    if parent:
        card = ask(world, "C", f"group_card('', {atom(parent)}, C)")
        traits = "; ".join(t["id"] for t in (card[0]["traits"] if card else [])) or "none"
        lines.append(f"They broke away from {parent} (traits: {traits}).")
        for e in _events(world, parent, 3):
            lines.append(f"  {parent} history, day {e.get('day')}: {e.get('text')}")
    if ctx.get("from"):
        lines.append(f"The founders came from: {', '.join(ctx['from'])}.")
    loc = ctx.get("location")
    if loc:
        d = one("MATCH (l:Location {name: $l}) RETURN coalesce(l.description, '') AS d", l=loc)
        if d:
            lines.append(f"Place {loc}: {d.get('d')}")
    lines.append("Factions that exist: " + ", ".join(sorted(str(g) for g in ask(world, "G", "group(G)"))))
    for e in _events(world, None, 5):
        lines.append(f"Recent, day {e.get('day')}: {e.get('text')}")
    lines.append("Trait ids (pick 1-3): " + ", ".join(f"{k} = {v}" for k, v in ask(world, "[T, X]", "trait_text(T, X)")))
    lines.append(
        '{"name":"CamelCase unique name","kind":"' + "|".join(_kinds(world)) + '",'
        '"goal":"<=12 words","description":"origin story, <=50 words",'
        '"traits":[{"id":"...","why":"<=20 words"}],'
        '"relations":[{"with":"existing faction","stance":"allied|friendly|neutral|rival|hostile","reason":"..."}],'
        '"chronicle":"one sentence for the history books"}'
    )
    lines[-1] = "JSON: " + lines[-1]
    return "\n".join(lines)


def _events(world, group: Optional[str], limit: int) -> List[Dict[str, Any]]:
    if group:
        q = ("MATCH (:Faction {name: $g})-[:IN_EVENT]->(e:FactionEvent) "
             "RETURN e.day AS day, e.text AS text ORDER BY e.t DESC LIMIT $n")
    else:
        q = "MATCH (e:FactionEvent) RETURN e.day AS day, e.text AS text ORDER BY e.t DESC LIMIT $n"
    return [dict(r) for r in world.run_query(q, g=group, n=limit) or []]


def _unique_name(world, base: str) -> str:
    base = re.sub(r"[^0-9A-Za-z]+", "", base) or "NewFaction"
    taken = {r["n"] for r in world.run_query("MATCH (n) WHERE n.name IS NOT NULL RETURN n.name AS n") or []}
    name, i = base, 2
    while name in taken:
        name, i = f"{base}{i}", i + 1
    return name


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I)
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(text[i:j + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
