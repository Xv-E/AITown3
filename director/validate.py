"""
蓝图可玩性校验与确定性机制修补。

LLM 多步规划之后，用硬规则保证：有玩家、地图连通、任务引用可解析、
门钥等交互能编译出至少一条可用 use_on。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .blueprint import EventBlueprint, ItemSpec, PersonSpec, _clean_name


ALLOWED_OPS = frozenset({"set_prop", "link", "unlink", "delete_entity"})
ALLOWED_PRECOND = frozenset({
    "prop_eq", "prop_neq", "has_item", "person_at", "item_at", "has_rel", "not_has_rel",
})
ALLOWED_SECRET = frozenset({"share_gossip", "guarded", "lie", "open"})


def _loc_names(bp: EventBlueprint) -> Set[str]:
    return {x.name for x in bp.locations}


def _person_names(bp: EventBlueprint) -> Set[str]:
    return {x.name for x in bp.persons}


def _item_names(bp: EventBlueprint) -> Set[str]:
    return {x.name for x in bp.items}


def _entity_names(bp: EventBlueprint) -> Set[str]:
    return _loc_names(bp) | _person_names(bp) | _item_names(bp)


def _connected_components(bp: EventBlueprint) -> List[Set[str]]:
    locs = _loc_names(bp)
    if not locs:
        return []
    adj: Dict[str, Set[str]] = {n: set() for n in locs}
    for e in bp.adjacencies:
        if e.a in adj and e.b in adj:
            adj[e.a].add(e.b)
            adj[e.b].add(e.a)
    seen: Set[str] = set()
    comps: List[Set[str]] = []
    for start in locs:
        if start in seen:
            continue
        stack = [start]
        comp: Set[str] = set()
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            comp.add(n)
            stack.extend(adj.get(n, ()))
        comps.append(comp)
    return comps


def _count_interactions_light(bp: EventBlueprint) -> int:
    by_item = {i.name: i for i in bp.items}
    by_person = {p.name: p for p in bp.persons}
    n = 0
    for tool in bp.items:
        for eff in tool.effects_on or []:
            if not isinstance(eff, dict):
                continue
            target = eff.get("target") or ""
            accepts: List[str] = []
            if target in by_item:
                accepts = list(by_item[target].accepts_from or [])
                if not accepts and by_item[target].key_item:
                    accepts = [by_item[target].key_item]
            elif target in by_person:
                accepts = list(by_person[target].accepts_from or [])
            if tool.name in accepts:
                n += 1
    return n


def count_compiled_interactions(bp: EventBlueprint) -> int:
    try:
        import os
        import sys

        actions_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "actions")
        )
        if actions_path not in sys.path:
            sys.path.insert(0, actions_path)
        from world_interactions import compile_interactions_from_blueprint

        return len(compile_interactions_from_blueprint(bp))
    except Exception:
        return _count_interactions_light(bp)


def validate_blueprint(bp: EventBlueprint) -> List[str]:
    """返回问题列表；空列表表示通过硬门槛。"""
    issues: List[str] = []
    if not bp.locations:
        issues.append("missing_locations")
    if not bp.persons:
        issues.append("missing_persons")
    players = [p for p in bp.persons if (p.role or "").lower() == "player"]
    if len(players) == 0:
        issues.append("no_player")
    elif len(players) > 1:
        issues.append("multiple_players")

    comps = _connected_components(bp)
    if len(comps) > 1:
        issues.append("map_disconnected")

    known = _entity_names(bp)
    for q in bp.quests:
        for inv in q.involves or []:
            if inv and inv not in known:
                issues.append(f"dangling_involves:{inv}")
        if q.giver and q.giver not in _person_names(bp):
            issues.append(f"bad_giver:{q.giver}")

    doors = [i for i in bp.items if (i.item_kind or "").lower() == "door"]
    if doors and count_compiled_interactions(bp) < 1:
        issues.append("door_without_interaction")
    if bp.quests and not bp.items:
        issues.append("quest_without_items")
    if bp.quests and count_compiled_interactions(bp) < 1:
        # 信息/对话关可无 use_on；仅当存在 door/key 或明确 tool 物品时才硬失败
        tools = [
            i for i in bp.items
            if (i.item_kind or "").lower() in ("key", "tool")
            or (i.effects_on or [])
        ]
        if tools or doors:
            issues.append("unplayable_interactions")

    npcs = [p for p in bp.persons if (p.role or "").lower() != "player"]
    if not npcs:
        issues.append("no_npcs")
    thin = [
        p.name for p in bp.persons
        if (p.role or "").lower() != "player"
        and not ((p.personality or "").strip() and (p.background or "").strip())
    ]
    if thin:
        issues.append("thin_npc_profiles")

    return issues


def _default_profile(role: str, name: str, quest_objective: str) -> Dict[str, Any]:
    role = (role or "npc").lower()
    if role == "player":
        return {
            "personality": "curious, persistent",
            "background": "A traveler drawn into local trouble.",
            "goal": quest_objective or "Resolve the main quest",
            "values": "curiosity,duty",
            "mood": "determined",
            "speech_style": "plain and brief",
            "secret_policy": "open",
            "trust_toward_player": 0,
            "power_level": 10,
        }
    if role == "antagonist":
        return {
            "personality": "suspicious, guarded",
            "background": f"{name} obstructs outsiders near the secret.",
            "goal": "Keep outsiders away",
            "values": "order,control",
            "mood": "wary",
            "speech_style": "clipped and formal",
            "secret_policy": "guarded",
            "trust_toward_player": 0,
            "power_level": 14,
        }
    return {
        "personality": "observant, practical",
        "background": f"{name} lives and works in town.",
        "goal": "Get through the day",
        "values": "hospitality,honesty",
        "mood": "neutral",
        "speech_style": "warm and plain",
        "secret_policy": "share_gossip",
        "trust_toward_player": 3,
        "power_level": 10,
    }


def ensure_cast_quality(
    bp: EventBlueprint,
    *,
    puzzle_kind: str = "",
) -> EventBlueprint:
    """
    确定性补全 NPC/玩家人设与社交：
    - 填空 personality/background/goal/speech_style/secret_policy/trust
    - 玩家与每位 NPC 至少一条 KNOWS
    - talk_info：保证有一条事实只被 NPC 知道
    """
    locs = _loc_names(bp)
    default_loc = next(iter(locs), "TownSquare")
    quest_obj = ""
    if bp.quests:
        quest_obj = (bp.quests[0].objective or bp.quests[0].description or "").strip()

    persons: List[PersonSpec] = []
    for p in bp.persons:
        role = (p.role or "npc").lower()
        defaults = _default_profile(role, p.name, quest_obj)
        located = p.located_at if p.located_at in locs else default_loc
        works = p.works_at if p.works_at in locs else None
        secret = (p.secret_policy or "").strip().lower()
        if secret not in ALLOWED_SECRET:
            secret = defaults["secret_policy"]
        trust = int(p.trust_toward_player or 0)
        if role == "antagonist" and trust > 1:
            trust = 0
        elif role == "npc" and trust == 0 and secret == "share_gossip":
            trust = int(defaults["trust_toward_player"])
        persons.append(
            PersonSpec(
                name=p.name,
                located_at=located,
                personality=(p.personality or "").strip() or defaults["personality"],
                background=(p.background or "").strip() or defaults["background"],
                parent_goal=(p.parent_goal or "").strip(),
                goal=(p.goal or "").strip() or defaults["goal"],
                values=(p.values or "").strip() or defaults["values"],
                mood=(p.mood or "").strip() or defaults["mood"],
                hp=int(p.hp or 100),
                power_level=int(p.power_level or defaults["power_level"]),
                works_at=works,
                role=role,
                speech_style=(p.speech_style or "").strip() or defaults["speech_style"],
                secret_policy=secret,
                trust_toward_player=trust,
                accepts_from=list(p.accepts_from or []),
                routine=[dict(s) for s in (getattr(p, "routine", None) or []) if isinstance(s, dict)],
            )
        )

    # talk_info / deliver / use_on_person：若无非玩家，补一个线人
    puzzle = (puzzle_kind or "").strip().lower()
    if puzzle in ("talk_info", "use_on_person", "deliver"):
        if not any((p.role or "").lower() != "player" for p in persons):
            defaults = _default_profile("npc", "TownInformant", quest_obj)
            persons.append(
                PersonSpec(
                    name="TownInformant",
                    located_at=default_loc,
                    personality=defaults["personality"],
                    background="Knows a useful lead about the quest.",
                    goal="Trade gossip carefully",
                    values=defaults["values"],
                    mood=defaults["mood"],
                    hp=90,
                    power_level=8,
                    role="npc",
                    speech_style=defaults["speech_style"],
                    secret_policy="share_gossip",
                    trust_toward_player=4,
                )
            )

    person_names = {p.name for p in persons}
    player = next((p.name for p in persons if p.role == "player"), None)

    # social: player KNOWS each other person
    social = list(bp.social)
    existing = {(a, r, b) for a, r, b in social}
    if player:
        for p in persons:
            if p.name == player:
                continue
            link = (player, "KNOWS", p.name)
            rev = (p.name, "KNOWS", player)
            if link not in existing and rev not in existing:
                social.append(link)
                existing.add(link)

    # facts: talk_info 需要至少一条「NPC 知道、玩家不知道」
    facts = list(bp.facts)
    if puzzle == "talk_info" and player:
        npcs = [p.name for p in persons if p.role != "player"]
        has_hidden = False
        for f in facts:
            known = set(f.known_by or [])
            if known and player not in known and known & set(npcs):
                has_hidden = True
                break
        if not has_hidden and npcs:
            from .blueprint import FactSpec

            facts.append(
                FactSpec(
                    name="HiddenLead",
                    text=(quest_obj or bp.premise or "A useful lead exists.")[:80],
                    secret_level=1,
                    known_by=[npcs[0]],
                )
            )

    # 清理 known_by / believed_by 悬空人名
    cleaned_facts = []
    for f in facts:
        known = [x for x in (f.known_by or []) if x in person_names]
        cleaned_facts.append(
            type(f)(
                name=f.name,
                text=f.text,
                secret_level=f.secret_level,
                known_by=known or ([player] if player else []),
            )
        )
    cleaned_rumors = []
    for r in bp.rumors:
        believed = [x for x in (r.believed_by or []) if x in person_names]
        about = r.about if (r.about in person_names or r.about in _item_names(bp) or r.about in locs) else None
        cleaned_rumors.append(
            type(r)(
                name=r.name,
                text=r.text,
                about=about,
                believed_by=believed,
            )
        )

    return EventBlueprint(
        title=bp.title,
        premise=bp.premise,
        locations=list(bp.locations),
        adjacencies=list(bp.adjacencies),
        persons=persons,
        items=list(bp.items),
        quests=list(bp.quests),
        facts=cleaned_facts,
        rumors=cleaned_rumors,
        social=social,
        source=bp.source,
    )


def _default_key_door_effect(
    *,
    key_name: str,
    door_name: str,
    from_loc: str,
    to_loc: str,
    consume: bool = True,
) -> Dict[str, Any]:
    ops: List[Dict[str, Any]] = [
        {
            "op": "set_prop",
            "entity": door_name,
            "label": "Item",
            "prop": "locked",
            "value": False,
        }
    ]
    if from_loc and to_loc and from_loc != to_loc:
        ops.append(
            {
                "op": "link",
                "a": from_loc,
                "a_label": "Location",
                "b": to_loc,
                "b_label": "Location",
                "rel": "ADJACENT_TO",
                "bidirectional": True,
            }
        )
    return {
        "target": door_name,
        "consume": consume,
        "preconditions": [
            {
                "op": "prop_eq",
                "entity": door_name,
                "label": "Item",
                "prop": "locked",
                "value": True,
            },
            {"op": "has_item", "person": "@actor", "item": "@tool"},
            {"op": "person_at", "person": "@actor", "location": "@target_loc"},
            {"op": "item_at", "entity": door_name, "location": "@target_loc"},
        ],
        "ops": ops,
    }


def _find_goal_location(bp: EventBlueprint) -> Optional[str]:
    for loc in bp.locations:
        tags = (loc.tags or "").lower()
        if "goal" in tags or "secret" in tags:
            return loc.name
    # 任务 involves 里的地点
    loc_names = _loc_names(bp)
    for q in bp.quests:
        for inv in q.involves or []:
            if inv in loc_names:
                return inv
    return None


def _pick_key_for_door(bp: EventBlueprint, door: ItemSpec) -> Optional[ItemSpec]:
    accepts = list(door.accepts_from or [])
    if door.key_item and door.key_item not in accepts:
        accepts.append(door.key_item)
    by_name = {i.name: i for i in bp.items}
    for name in accepts:
        if name in by_name and name != door.name:
            return by_name[name]
    keys = [
        i for i in bp.items
        if i.name != door.name
        and (
            (i.item_kind or "").lower() == "key"
            or "key" in (i.name or "").lower()
            or (i.quest_flag or "").lower() == "main_macguffin"
        )
    ]
    return keys[0] if keys else None


def _sanitize_effects(effects: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in effects or []:
        if not isinstance(raw, dict):
            continue
        e = dict(raw)
        if e.get("target"):
            e["target"] = _clean_name(str(e["target"]))
        ops = []
        for op in e.get("ops") or []:
            if not isinstance(op, dict):
                continue
            if (op.get("op") or "") not in ALLOWED_OPS:
                continue
            ops.append(dict(op))
        pre = []
        for p in e.get("preconditions") or []:
            if not isinstance(p, dict):
                continue
            if (p.get("op") or "") not in ALLOWED_PRECOND:
                continue
            pre.append(dict(p))
        e["ops"] = ops
        e["preconditions"] = pre
        if "consume" in e and e["consume"] is not None:
            e["consume"] = bool(e["consume"])
        if e.get("target") and ops:
            out.append(e)
    return out


def ensure_playable_mechanics(bp: EventBlueprint) -> EventBlueprint:
    """
    确定性修补交互：
    - 清理非法 ops/preconditions
    - 互指 accepts_from ↔ effects_on.target
    - 对 locked door 自动补齐 key→door 解锁+连通
    """
    items = list(bp.items)
    by_name = {i.name: i for i in items}
    loc_names = _loc_names(bp)
    goal_loc = _find_goal_location(bp)

    # 1) sanitize effects
    for i, it in enumerate(items):
        cleaned = _sanitize_effects(list(it.effects_on or []))
        if cleaned != list(it.effects_on or []):
            items[i] = ItemSpec(
                name=it.name,
                value=it.value,
                located_at=it.located_at,
                held_by=it.held_by,
                owned_by=it.owned_by,
                quest_flag=it.quest_flag,
                description=it.description,
                item_kind=it.item_kind,
                locked=it.locked,
                key_item=it.key_item,
                connects_to=it.connects_to,
                portable=it.portable,
                effects_on=cleaned,
                accepts_from=list(it.accepts_from or []),
                stamped=bool(it.stamped),
            )
    by_name = {i.name: i for i in items}

    # 2) mutual accepts_from for existing effects
    for tool in list(items):
        for eff in tool.effects_on or []:
            target = (eff.get("target") or "") if isinstance(eff, dict) else ""
            if not target or target not in by_name:
                continue
            tgt = by_name[target]
            accepts = list(tgt.accepts_from or [])
            if tool.name not in accepts:
                accepts.append(tool.name)
                idx = next(i for i, x in enumerate(items) if x.name == target)
                items[idx] = ItemSpec(
                    name=tgt.name,
                    value=tgt.value,
                    located_at=tgt.located_at,
                    held_by=tgt.held_by,
                    owned_by=tgt.owned_by,
                    quest_flag=tgt.quest_flag,
                    description=tgt.description,
                    item_kind=tgt.item_kind,
                    locked=tgt.locked,
                    key_item=tgt.key_item,
                    connects_to=tgt.connects_to,
                    portable=tgt.portable,
                    effects_on=list(tgt.effects_on or []),
                    accepts_from=accepts,
                    stamped=bool(tgt.stamped),
                )
                by_name[target] = items[idx]

    # 3) door/key wiring
    for door in [i for i in items if (i.item_kind or "").lower() == "door"]:
        key = _pick_key_for_door(
            EventBlueprint(
                title=bp.title,
                premise=bp.premise,
                locations=list(bp.locations),
                adjacencies=list(bp.adjacencies),
                persons=list(bp.persons),
                items=items,
                quests=list(bp.quests),
                facts=list(bp.facts),
                rumors=list(bp.rumors),
                social=list(bp.social),
                source=bp.source,
            ),
            door,
        )
        if key is None:
            continue
        door_idx = next(i for i, x in enumerate(items) if x.name == door.name)
        key_idx = next(i for i, x in enumerate(items) if x.name == key.name)
        door_cur = items[door_idx]
        key_cur = items[key_idx]

        accepts = list(door_cur.accepts_from or [])
        if key_cur.name not in accepts:
            accepts.append(key_cur.name)
        kind = door_cur.item_kind or "door"
        locked = True if door_cur.locked or kind == "door" else door_cur.locked

        from_loc = door_cur.located_at or ""
        to_loc = door_cur.connects_to or ""
        if not to_loc and goal_loc and goal_loc != from_loc and goal_loc in loc_names:
            to_loc = goal_loc
        # locked adjacency with this key
        if not to_loc:
            for e in bp.adjacencies:
                if e.locked and e.key == key_cur.name:
                    if e.a == from_loc:
                        to_loc = e.b
                        break
                    if e.b == from_loc:
                        to_loc = e.a
                        break

        items[door_idx] = ItemSpec(
            name=door_cur.name,
            value=door_cur.value,
            located_at=door_cur.located_at,
            held_by=None,
            owned_by=door_cur.owned_by,
            quest_flag=door_cur.quest_flag,
            description=door_cur.description,
            item_kind="door",
            locked=locked,
            key_item=key_cur.name,
            connects_to=to_loc,
            portable=False,
            effects_on=list(door_cur.effects_on or []),
            accepts_from=accepts,
            stamped=bool(door_cur.stamped),
        )

        effects = list(key_cur.effects_on or [])
        has_target = any(
            isinstance(e, dict) and e.get("target") == door_cur.name for e in effects
        )
        if not has_target and from_loc:
            effects.append(
                _default_key_door_effect(
                    key_name=key_cur.name,
                    door_name=door_cur.name,
                    from_loc=from_loc,
                    to_loc=to_loc,
                )
            )
            items[key_idx] = ItemSpec(
                name=key_cur.name,
                value=key_cur.value,
                located_at=key_cur.located_at,
                held_by=key_cur.held_by,
                owned_by=key_cur.owned_by,
                quest_flag=key_cur.quest_flag or "main_macguffin",
                description=key_cur.description or f"Opens {door_cur.name}.",
                item_kind=key_cur.item_kind or "key",
                locked=key_cur.locked,
                key_item=key_cur.key_item,
                connects_to=key_cur.connects_to,
                portable=True if key_cur.portable is None else key_cur.portable,
                effects_on=effects,
                accepts_from=list(key_cur.accepts_from or []),
                stamped=bool(key_cur.stamped),
            )

    return EventBlueprint(
        title=bp.title,
        premise=bp.premise,
        locations=list(bp.locations),
        adjacencies=list(bp.adjacencies),
        persons=list(bp.persons),
        items=items,
        quests=list(bp.quests),
        facts=list(bp.facts),
        rumors=list(bp.rumors),
        social=list(bp.social),
        source=bp.source,
    )


def is_minimally_playable(bp: EventBlueprint) -> Tuple[bool, List[str]]:
    """硬门槛：地点、唯一玩家、有任务则有物品。交互问题尽量由 ensure 修补。"""
    issues = validate_blueprint(bp)
    hard = [
        x for x in issues
        if x in (
            "missing_locations",
            "missing_persons",
            "no_player",
            "quest_without_items",
            "door_without_interaction",
            "unplayable_interactions",
        )
        or x.startswith("dangling_involves:")
    ]
    return (len(hard) == 0, issues)
