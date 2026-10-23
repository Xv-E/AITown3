"""
人物的「脑子里有什么」：记忆（可按类型筛、按意思搜）+ 知道的事（事实 / 流言、地点、
以为东西在哪、和谁有矛盾、对玩家的信任）。只读，给客户端 NPC 选项卡的「记忆 / 知道的事」两页。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional


def _short_time(ts: Any) -> str:
    text = str(ts or "")
    return text.replace("T", " ")[5:16] if len(text) >= 16 else text


def memories_view(memory, name: str, *, query: str = "", kind: str = "", limit: int = 80) -> Dict[str, Any]:
    if memory is None:
        return {"available": False, "total": 0, "types": {}, "memories": []}
    every = memory.list(name)
    types = Counter(str((m.get("metadata") or {}).get("type") or "other") for m in every)
    where = {"type": kind} if kind else None
    if query.strip():
        hits = memory.search(name, query.strip(), top_k=max(1, min(limit, 50)), where=where) or []
    else:
        hits = [m for m in every if not kind or (m.get("metadata") or {}).get("type") == kind]
        hits.sort(key=lambda m: str((m.get("metadata") or {}).get("timestamp") or ""), reverse=True)
        hits = hits[:limit]
    out = []
    for m in hits:
        meta = m.get("metadata") or {}
        out.append({
            "text": str(m.get("text") or ""),
            "type": str(meta.get("type") or "other"),
            "location": str(meta.get("location") or ""),
            "time": _short_time(meta.get("timestamp")),
        })
    return {"available": True, "total": len(every), "types": dict(types), "memories": out}


def knows_view(world, name: str) -> Dict[str, Any]:
    from narration import pretty

    facts: List[Dict[str, str]] = []
    for kind, rel, label in (("fact", "KNOWS_FACT", "Fact"), ("rumor", "BELIEVES", "Rumor")):
        rows = world.run_query(
            f"MATCH (:Person {{name: $n}})-[:{rel}]->(f:{label}) RETURN f.name AS name, coalesce(f.text, '') AS text ORDER BY name",
            n=name,
        ) or []
        facts += [{"kind": kind, "text": r.get("text") or r.get("name") or ""} for r in rows]

    places: List[str] = []
    seen: List[str] = []
    omniscient = False
    try:
        from knowledge import load_knowledge

        k = load_knowledge(world, name, _seed_missing=False)
        omniscient = k.omniscient
        places = sorted(pretty(p) for p in k.places)
        for item, (loc, holder) in sorted(k.items.items()):
            where = f"held by {pretty(holder)}" if holder else f"at {pretty(loc)}" if loc else "somewhere"
            seen.append(f"{pretty(item)} - {where}")
    except Exception:  # noqa: BLE001
        pass

    grudges: List[str] = []
    try:
        from conflicts import tensions_of

        for t in tensions_of(world, name):
            level = int(t.get("level") or 0)
            why = f" ({t['reason']})" if t.get("reason") else ""
            if t.get("dir") == "out":
                grudges.append(f"Annoyed with {pretty(t['other'])}, level {level}{why}")
            else:
                grudges.append(f"{pretty(t['other'])} is annoyed with them, level {level}{why}")
    except Exception:  # noqa: BLE001
        pass

    trust: Optional[int] = None
    player = ""
    rows = world.run_query(
        """
        MATCH (me:Person {name: $n})
        OPTIONAL MATCH (p:Person) WHERE coalesce(p.is_player, false) OR p.role = 'player'
        RETURN coalesce(me.trust_toward_player, 0) AS trust, collect(p.name)[0] AS player,
               coalesce(me.is_player, false) OR me.role = 'player' AS me_player
        """,
        n=name,
    ) or []
    if rows and not rows[0].get("me_player"):
        trust, player = int(rows[0].get("trust") or 0), str(rows[0].get("player") or "")
    relations: List[str] = []
    try:
        from relations import alliance_of, alliances_view, members, relations_of, word_for

        group = alliance_of(world, name)
        if group:
            mates = [pretty(m) for m in members(world, group) if m != name]
            relations.append(f"Member of {group} (with {', '.join(mates) or 'nobody else'})")
            for g in alliances_view(world):
                if g["name"] == group:
                    relations += [f"  {group} and {s['other']}: {s['word']} ({s['value']})" for s in g["stances"]]

        for r in relations_of(world, name):
            tags = f"  [{', '.join(r['tags'])}]" if r["tags"] else ""
            why = f" - {r['reason']}" if r.get("reason") else ""
            relations.append(f"{word_for(int(r['value'])).capitalize()} {pretty(r['other'])} ({int(r['value'])}){tags}{why}")
    except Exception:  # noqa: BLE001
        pass
    return {
        "relations": relations,
        "facts": facts,
        "places": places,
        "seen": seen,
        "omniscient": omniscient,
        "grudges": grudges,
        "trust": trust,
        "player": player,
    }


# 人物设定里给玩家看的字段（顺序即显示顺序）
PROFILE_FIELDS = (
    ("role", "Role"),
    ("personality", "Personality"),
    ("values", "Values"),
    ("background", "Background"),
    ("goal", "Long-term goal"),
    ("mood", "Mood"),
    ("speech_style", "Speech style"),
    ("secret", "Secret"),
)


def profile_view(world, name: str) -> Dict[str, Any]:
    """作者 / 导演给这个人写的设定，加上此刻的状态（在哪、血量、拿着什么）。"""
    from narration import pretty

    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})
        OPTIONAL MATCH (p)-[:LOCATED_AT]->(l:Location)
        OPTIONAL MATCH (p)-[:HAS_ITEM]->(i:Item)
        RETURN properties(p) AS props, l.name AS loc, collect(i.name) AS held
        """,
        n=name,
    ) or []
    if not rows:
        return {"fields": [], "status": []}
    props = dict(rows[0].get("props") or {})
    fields = []
    for key, label in PROFILE_FIELDS:
        value = props.get(key) if key != "goal" else (props.get("goal") or props.get("initial_goal"))
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        text = str(value or "").strip()
        if text:
            fields.append({"label": label, "value": text})
    try:
        from factions import membership_summary

        factions = membership_summary(world, name)
    except Exception:  # noqa: BLE001
        factions = ""
    if factions:
        fields.append({"label": "Factions", "value": factions})
    status = [f"At {pretty(rows[0].get('loc'))}" if rows[0].get("loc") else "Somewhere unknown"]
    if props.get("hp") is not None:
        status.append(f"HP {props.get('hp')}")
    if props.get("alive") is False:
        status.append("down")
    held = [pretty(h) for h in rows[0].get("held") or [] if h]
    status.append("Carrying " + ", ".join(held) if held else "Carrying nothing")
    from game_loop import normalize_brain

    player = bool(props.get("is_player")) or props.get("role") == "player"
    brain = "" if player else normalize_brain(props.get("brain"))
    return {"fields": fields, "status": status, "brain": brain}


def _hours(h: float) -> str:
    h = float(h) % 24
    return f"{int(h):02d}:{int(round((h - int(h)) * 60)) % 60:02d}"


def routine_view(world, name: str, loop: Any = None) -> Dict[str, Any]:
    """一天的日程：每段几点到几点、去哪做什么；现在是哪一段、今天做完了没有。"""
    from narration import pretty
    from routine import _in_slot, day_index, hour_of_day, load_person_routine

    slots = load_person_routine(world, name)
    state = loop.get_agent_state(name) if loop is not None else None
    sim_time = loop.sim_time if loop is not None else None
    hour = hour_of_day(sim_time) if sim_time is not None else None
    done = set(getattr(state, "routine_done", []) or [])
    today = day_index(sim_time) if sim_time is not None else None
    out = []
    for slot in slots:
        what = slot.activity or slot.action or slot.goal
        extra = []
        if slot.social:
            extra.append("chats with people around")
        if slot.once:
            extra.append("once a day")
        if slot.item_kind:
            extra.append(f"needs {slot.item_kind}")
        out.append({
            "hours": f"{_hours(slot.start)}-{_hours(slot.end)}",
            "where": pretty(slot.location),
            "what": what,
            "notes": ", ".join(extra),
            "now": hour is not None and _in_slot(hour, slot.start, slot.end),
            "done": bool(slot.once and today is not None and f"{today}:{slot.key}" in done),
        })
    held = getattr(state, "held_state", None) if state is not None else None
    return {"slots": out, "state": pretty(held) if held else ""}


def mind_view(world, memory, name: str, *, query: str = "", kind: str = "", limit: int = 80,
              loop: Any = None) -> Dict[str, Any]:
    return {
        "name": name,
        **memories_view(memory, name, query=query, kind=kind, limit=limit),
        "query": query,
        "kind": kind,
        "knows": knows_view(world, name),
        "profile": profile_view(world, name),
        "routine": routine_view(world, name, loop),
    }
