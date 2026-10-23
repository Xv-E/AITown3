"""
Generate available actions and parameter options from character context.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from data_action import DataAction
from registry import get_registry


def _person_state(world, name: str) -> Optional[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (p:Person {name: $name})
        OPTIONAL MATCH (p)-[:LOCATED_AT]->(l:Location)
        OPTIONAL MATCH (p)-[:HAS_ITEM]->(i:Item)
        RETURN p.name AS name,
               coalesce(p.hp, 0) AS hp,
               coalesce(p.power_level, 0) AS power_level,
               coalesce(p.dead, false) AS dead,
               coalesce(p.is_player, false) AS is_player,
               coalesce(p.role, 'npc') AS role,
               l.name AS location,
               collect(DISTINCT i.name) AS held_items,
               p.personality AS personality,
               p.background AS background,
               p.parent_goal AS parent_goal,
               coalesce(p.goal, '') AS goal,
               p.values AS values,
               p.mood AS mood,
               coalesce(p.coins, 0) AS coins,
               coalesce(p.trader, false) AS trader,
               coalesce(p.follows, '') AS follows,
               [(p)-[:ALLY]->(o:Person) | o.name] AS allies
        """,
        name=name,
    )
    if not rows:
        return None
    r = rows[0]
    held = [x for x in (r.get("held_items") or []) if x]
    hp = int(r.get("hp") or 0)
    dead = bool(r.get("dead"))
    alive = hp > 0 and not dead
    role = str(r.get("role") or "npc")
    is_player = bool(r.get("is_player")) or role == "player"
    return {
        "name": r["name"],
        "hp": hp,
        "power_level": int(r.get("power_level") or 0),
        "dead": dead,
        "alive": alive,
        "is_player": is_player,
        "role": "player" if is_player else role,
        "location": r.get("location"),
        "held_items": held,
        "personality": r.get("personality") or "",
        "background": r.get("background") or "",
        "parent_goal": r.get("parent_goal") or "",
        "goal": r.get("goal") or "",
        "values": r.get("values") or "",
        "mood": r.get("mood") or "",
        "coins": int(float(r.get("coins") or 0)),
        "trader": bool(r.get("trader")),
        "follows": r.get("follows") or "",
        "allies": sorted(x for x in (r.get("allies") or []) if x),
    }


def _adjacent_locations(world, loc: str) -> List[str]:
    rows = world.run_query(
        """
        MATCH (l:Location {name: $loc})-[:ADJACENT_TO]->(n:Location)
        RETURN n.name AS name ORDER BY name
        """,
        loc=loc,
    )
    return [r["name"] for r in rows if r.get("name")]


def _locked_exits(world, loc: str) -> List[Dict[str, str]]:
    """Destinations behind locked doors at the current location (requires use_on to unlock)."""
    if not loc:
        return []
    out = []
    for door in _doors_at_location(world, loc):
        if not door.get("locked"):
            continue
        to = door.get("connects_to") or ""
        if not to:
            continue
        out.append({
            "to": to,
            "key": door.get("key_item") or "",
            "door": door.get("name") or "",
        })
    return out


def _people_at_location(world, loc: str, exclude: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (p:Person)-[:LOCATED_AT]->(l:Location {name: $loc})
        RETURN p.name AS name,
               coalesce(p.hp, 0) AS hp,
               coalesce(p.dead, false) AS dead,
               coalesce(p.power_level, 0) AS power_level,
               coalesce(p.is_player, false) AS is_player,
               coalesce(p.role, 'npc') AS role,
               coalesce(p.mood, '') AS mood,
               coalesce(p.personality, '') AS personality,
               coalesce(p.trust_toward_player, 0) AS trust_toward_player,
               coalesce(p.speech_style, '') AS speech_style,
               coalesce(p.secret_policy, 'share_gossip') AS secret_policy
        ORDER BY name
        """,
        loc=loc,
    )
    out = []
    for r in rows:
        n = r.get("name")
        if not n or n == exclude:
            continue
        hp = int(r.get("hp") or 0)
        dead = bool(r.get("dead"))
        role = str(r.get("role") or "npc")
        is_player = bool(r.get("is_player")) or role == "player"
        personality = (r.get("personality") or "")[:80]
        out.append({
            "name": n,
            "hp": hp,
            "power_level": int(r.get("power_level") or 0),
            "alive": hp > 0 and not dead,
            "is_player": is_player,
            "role": "player" if is_player else role,
            "mood": r.get("mood") or "",
            "personality": personality,
            "trust_toward_player": int(r.get("trust_toward_player") or 0),
            "speech_style": r.get("speech_style") or "",
            "secret_policy": r.get("secret_policy") or "share_gossip",
        })
    return out


def _enrich_item_interaction(item: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich key_item / connects_to from session interaction table (overrides graph properties)."""
    name = item.get("name") or ""
    if not name:
        return item
    try:
        from world_interactions import (
            destination_from_interaction,
            find_interaction,
            tools_for_target,
        )

        tools = tools_for_target(name)
        if tools:
            item = dict(item)
            item["key_item"] = tools[0]
            inter = find_interaction(tools[0], name)
            loc = item.get("_loc")  # optional
            if inter:
                # connects_to: other end of link; caller must pass loc when door side is ambiguous
                pair_dest = None
                if loc:
                    pair_dest = destination_from_interaction(inter, from_loc=loc)
                if not pair_dest:
                    from world_interactions import link_pair

                    pair = link_pair(inter)
                    if pair:
                        a, b = pair
                        # If from is unknown, keep existing connects_to or use b
                        pair_dest = item.get("connects_to") or b
                if pair_dest:
                    item["connects_to"] = pair_dest
    except Exception:
        pass
    return item


def _items_at_location(world, loc: str) -> List[Dict[str, Any]]:
    """Ground items not held by anyone (including doors and fixtures)."""
    rows = world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(l:Location {name: $loc})
        WHERE NOT (:Person)-[:HAS_ITEM]->(i)
        RETURN i.name AS name,
               coalesce(i.value, 0) AS value,
               coalesce(i.description, '') AS description,
               coalesce(i.quest_flag, '') AS quest_flag,
               coalesce(i.item_kind, '') AS item_kind,
               coalesce(i.locked, false) AS locked,
               coalesce(i.key_item, '') AS key_item,
               coalesce(i.connects_to, '') AS connects_to,
               coalesce(i.portable, true) AS portable,
               i.script AS script,
               i.x AS x, i.y AS y,
               coalesce(i.footprint_w, 1) AS w, coalesce(i.footprint_h, 1) AS h,
               coalesce(i.archetype, '') AS archetype,
               [k IN keys(i) WHERE k STARTS WITH 'need_' AND i[k] > 0 | substring(k, 5) + ' ' + toString(i[k])] AS needs,
               i.stock AS stock
        ORDER BY name
        """,
        loc=loc,
    )
    out = []
    for r in rows:
        if not r.get("name"):
            continue
        script_raw = r.get("script")
        script_obj = None
        if isinstance(script_raw, str) and script_raw.strip():
            try:
                import json

                script_obj = json.loads(script_raw)
            except Exception:
                script_obj = {"lore": script_raw}
        elif isinstance(script_raw, dict):
            script_obj = script_raw
        item = {
            "name": r["name"],
            "value": int(r.get("value") or 0),
            "needs": list(r.get("needs") or []),
            "stock": int(float(r["stock"])) if r.get("stock") is not None else None,
            "description": r.get("description") or "",
            "quest_flag": r.get("quest_flag") or "",
            "item_kind": r.get("item_kind") or "",
            "locked": bool(r.get("locked")),
            "key_item": r.get("key_item") or "",
            "connects_to": r.get("connects_to") or "",
            "portable": False if r.get("portable") is False else True,
            "script": script_obj,
            "x": r.get("x"),
            "y": r.get("y"),
            "w": int(r.get("w") or 1),
            "h": int(r.get("h") or 1),
            "archetype": r.get("archetype") or "",
            "_loc": loc,
        }
        out.append(_enrich_item_interaction(item))
    return out



def _item_names_at_location(world, loc: str) -> List[str]:
    return [x["name"] for x in _items_at_location(world, loc)]


def _pickable_names_at_location(world, loc: str) -> List[str]:
    return [
        x["name"]
        for x in _items_at_location(world, loc)
        if x.get("item_kind") != "door" and x.get("portable", True)
    ]


def _doors_at_location(world, loc: str) -> List[Dict[str, Any]]:
    return [
        x for x in _items_at_location(world, loc)
        if (x.get("item_kind") or "").lower() == "door"
    ]


def _is_interactable_item(item: Dict[str, Any]) -> bool:
    """Doors / fixtures / devices with interactive scripts."""
    if (item.get("item_kind") or "").lower() == "door":
        return True
    if item.get("portable") is False:
        return True
    script = item.get("script")
    if isinstance(script, str) and script.strip():
        try:
            import json

            script = json.loads(script)
        except Exception:
            script = {"lore": script}
    if isinstance(script, dict):
        if script.get("interactive") or script.get("behavior"):
            return True
    return False


def _split_ground_items(
    items: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split into (pickable items, interactables)."""
    pickable: List[Dict[str, Any]] = []
    interactables: List[Dict[str, Any]] = []
    for it in items:
        if _is_interactable_item(it):
            interactables.append(it)
        else:
            pickable.append(it)
    return pickable, interactables


def _field(
    name: str,
    typ: str,
    *,
    value: str = "",
    options: Optional[List[str]] = None,
    readonly: bool = False,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "name": name,
        "type": typ,
        "label": label or name,
        "value": value,
        "options": options or [],
        "readonly": readonly,
    }


def _build_action_entries(world, person: str, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    reg = get_registry()
    loc = state.get("location")
    held = state.get("held_items") or []
    alive = state.get("alive", True)
    adjacent = _adjacent_locations(world, loc) if loc else []
    people_here = _people_at_location(world, loc, exclude=person) if loc else []
    alive_here = [p["name"] for p in people_here if p["alive"]]
    items_here = _pickable_names_at_location(world, loc) if loc else []
    doors_here = _doors_at_location(world, loc) if loc else []
    locked_doors = [
        d for d in doors_here
        if d.get("locked") and d.get("key_item")
    ]
    # Session interactions: any target (item/person) at current location × held tools
    interact_matches: List[Dict[str, Any]] = []
    catalog: Dict[str, Any] = {}
    try:
        from world_interactions import (
            destination_from_interaction,
            list_interactions,
            use_on_catalog,
        )

        catalog = use_on_catalog()
        ground_names = {x["name"] for x in (_items_at_location(world, loc) if loc else [])}
        people_names = set(alive_here)
        present = ground_names | people_names
        for inter in list_interactions():
            if inter.target not in present:
                continue
            if inter.tool not in held:
                continue
            to = destination_from_interaction(inter, from_loc=loc) if loc else None
            if not to:
                to = loc or ""
            interact_matches.append({
                "name": inter.target,
                "key_item": inter.tool,
                "connects_to": to,
                "consume": inter.consumes_tool,
            })
    except Exception:
        pass
    if not interact_matches:
        interact_matches = [
            d for d in locked_doors if d.get("key_item") in held
        ]
    # All clickable targets at current location (for UI target selection)
    target_opts = sorted(
        {x["name"] for x in (_items_at_location(world, loc) if loc else [])}
        | set(alive_here)
    )
    entries: List[Dict[str, Any]] = []
    sources: Dict[str, List[str]] = {
        "self": [person],
        "here": [loc] if loc else [],
        "adjacent": adjacent,
        "held": list(held),
        "ground": items_here,
        "people_here": alive_here,
    }

    for action in reg.all():
        fields: List[Dict[str, Any]] = []
        available = False
        defaults: Dict[str, str] = {}

        if isinstance(action, DataAction):
            available, defaults, fields = _data_action_form(action, sources) if alive else (False, {}, [])

        elif action.name == "use_on":
            # Has inventory + someone/something nearby: can "use on target"; feasibility from world interaction table
            matching = interact_matches
            if alive and loc and held and target_opts:
                available = True
                if matching:
                    hit = matching[0]
                    tool = hit["key_item"]
                    tgt = hit["name"]
                    to = hit.get("connects_to") or loc
                    tool_opts = list({m["key_item"] for m in matching})
                    tgt_opts = list({m["name"] for m in matching}) or target_opts
                else:
                    tool = held[0]
                    tgt = target_opts[0]
                    to = loc
                    tool_opts = list(held)
                    tgt_opts = target_opts
                defaults = {
                    "c": person,
                    "i": tool,
                    "target": tgt,
                    "from": loc,
                    "to": to,
                }
                fields = [
                    _field("c", "Person", value=person, readonly=True, label="Character"),
                    _field("i", "Item", value=tool, options=tool_opts, label="Tool"),
                    _field("target", "Item", value=tgt, options=tgt_opts, label="Target"),
                    _field("from", "Location", value=loc, readonly=True, label="Current location"),
                    _field("to", "Location", value=to, readonly=True, label="Ref location"),
                ]

        elif action.name == "interact":
            scripted = []
            _, interactables_here = _split_ground_items(
                _items_at_location(world, loc) if loc else []
            )
            for it in interactables_here:
                sc = it.get("script") if isinstance(it, dict) else None
                if isinstance(sc, dict) and (sc.get("interactive") or sc.get("behavior")):
                    scripted.append(it["name"])
                elif (it.get("item_kind") or "").lower() in ("puzzle", "mechanism", "tree"):
                    scripted.append(it["name"])
            if alive and loc and scripted:
                available = True
                defaults = {"c": person, "target": scripted[0], "option": "auto"}
                fields = [
                    _field("c", "Person", value=person, readonly=True, label="Character"),
                    _field("target", "Item", value=scripted[0], options=scripted, label="Device"),
                    _field(
                        "option",
                        "Item",
                        value="auto",
                        options=["auto"],
                        label="Choice (auto=LLM)",
                    ),
                ]

        if available:
            # Mark whether the first default combination is feasible via check
            feasible, reason = action.check(world, **defaults)
            entries.append({
                "name": action.name,
                "description": action.description,
                "category": action.category,
                "available": True,
                "feasible": feasible,
                "feasible_hint": reason if not feasible else "Feasible",
                "defaults": defaults,
                "fields": fields,
            })

    # Also list unavailable actions (grayed out) so users understand constraints
    available_names = {e["name"] for e in entries}
    for action in reg.all():
        if action.name not in available_names:
            if isinstance(action, DataAction):
                reason = _data_action_missing(action, sources, loc=loc, alive=alive)
            else:
                reason = _unavailable_reason(action.name, loc, held, alive)
            entries.append({
                "name": action.name,
                "description": action.description,
                "category": action.category,
                "available": False,
                "feasible": False,
                "feasible_hint": reason,
                "defaults": {},
                "fields": [],
            })

    # Available actions first
    entries.sort(key=lambda x: (not x["available"], x["name"]))
    return entries


_SOURCE_MISSING = {
    "here": "Character has no current location",
    "adjacent": "No adjacent locations to go to",
    "held": "Not holding any items",
    "ground": "No pickable items at current location",
    "people_here": "No other living characters at this location",
}


def _param_source(action: DataAction, index: int, name: str) -> str:
    return action.param_sources.get(name) or ("self" if index == 0 else "")


def _data_action_form(
    action: DataAction, sources: Dict[str, List[str]]
) -> Tuple[bool, Dict[str, str], List[Dict[str, Any]]]:
    """按参数的 source（self / here / adjacent / held / ground / people_here）填表单。"""
    defaults: Dict[str, str] = {}
    fields: List[Dict[str, Any]] = []
    for idx, p in enumerate(action.parameters):
        src = _param_source(action, idx, p.name)
        opts = sources.get(src) or []
        if not opts:
            return False, {}, []
        fixed = src in ("self", "here")
        defaults[p.name] = opts[0]
        fields.append(_field(
            p.name, p.type, value=opts[0], options=None if fixed else opts, readonly=fixed,
        ))
    return True, defaults, fields


def _data_action_missing(
    action: DataAction, sources: Dict[str, List[str]], *, loc: Optional[str], alive: bool
) -> str:
    if not alive:
        return "Character is dead"
    if not loc:
        return _SOURCE_MISSING["here"]
    for idx, p in enumerate(action.parameters):
        src = _param_source(action, idx, p.name)
        if not sources.get(src):
            return _SOURCE_MISSING.get(src) or f"No value available for '{p.name}'"
    return "Not available"


def _unavailable_reason(action_name: str, loc: Optional[str], held: List[str], alive: bool) -> str:
    if not loc:
        return "Character has no current location"
    if action_name == "use_on":
        if not held:
            return "Not holding any items"
        if not alive:
            return "Character is dead"
        return "No selectable targets at this location"
    return "Not available"


def list_characters(world) -> List[Dict[str, Any]]:
    names = world.list_entities("Person")
    out = []
    for name in names:
        state = _person_state(world, name)
        if state:
            out.append({
                "name": state["name"],
                "location": state["location"],
                "hp": state["hp"],
                "power_level": state["power_level"],
                "alive": state["alive"],
                "held_items": state["held_items"],
                "is_player": state["is_player"],
                "role": state["role"],
            })
    return out


def get_player(world) -> Optional[Dict[str, Any]]:
    """Return is_player / role=player character; first match if multiple."""
    rows = world.run_query(
        """
        MATCH (p:Person)
        WHERE coalesce(p.is_player, false) = true OR coalesce(p.role, '') = 'player'
        RETURN p.name AS name
        ORDER BY p.name
        LIMIT 1
        """
    )
    if rows and rows[0].get("name"):
        return _person_state(world, rows[0]["name"])
    # Fallback: character named Traveler
    for name in world.list_entities("Person") or []:
        if name == "Traveler":
            return _person_state(world, name)
    return None


def get_location_info(world, loc: str) -> Optional[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (l:Location {name: $loc})
        RETURN l.name AS name,
               coalesce(l.description, '') AS description,
               coalesce(l.tags, '') AS tags,
               coalesce(l.indoor, false) AS indoor,
               coalesce(l.parent, '') AS parent
        """,
        loc=loc,
    )
    if not rows:
        return None
    r = rows[0]
    from spatial import zone_grid

    grid_w, grid_h = zone_grid(world, loc)
    return {
        "name": r["name"],
        "description": r.get("description") or "",
        "tags": r.get("tags") or "",
        "grid_w": grid_w,
        "grid_h": grid_h,
        "indoor": bool(r.get("indoor")),
        "parent": r.get("parent") or "",
    }


def list_locations_graph(world) -> Dict[str, Any]:
    """All locations + adjacency edges for room map layout.

    Each location returns separately:
      - persons: characters present
      - items: pickable ground items
      - interactables: doors and other interactables
    """
    loc_rows = world.run_query(
        """
        MATCH (l:Location)
        OPTIONAL MATCH (p:Person)-[:LOCATED_AT]->(l)
        OPTIONAL MATCH (l)-[:ADJACENT_TO]->(adj:Location)
        RETURN l.name AS name,
               coalesce(l.description, '') AS description,
               coalesce(l.tags, '') AS tags,
               l.map_x AS map_x, l.map_y AS map_y, l.controlled_by AS controlled_by,
               collect(DISTINCT p.name) AS persons,
               collect(DISTINCT adj.name) AS adjacent
        ORDER BY name
        """
    )
    edge_rows = world.run_query(
        """
        MATCH (a:Location)-[:ADJACENT_TO]->(b:Location)
        WHERE a.name < b.name
        RETURN a.name AS a, b.name AS b
        ORDER BY a, b
        """
    )
    locations = []
    for r in loc_rows:
        name = r.get("name")
        if not name:
            continue
        persons = [x for x in (r.get("persons") or []) if x]
        adjacent = [x for x in (r.get("adjacent") or []) if x]
        pickable, interactables = _split_ground_items(_items_at_location(world, name))
        locations.append({
            "name": name,
            "description": r.get("description") or "",
            "tags": r.get("tags") or "",
            "map_x": r.get("map_x"),
            "map_y": r.get("map_y"),
            "controlled_by": r.get("controlled_by") or "",
            "persons": persons,
            "items": [x["name"] for x in pickable],
            "interactables": [
                {
                    "name": x["name"],
                    "item_kind": x.get("item_kind") or "",
                    "locked": bool(x.get("locked")),
                    "key_item": x.get("key_item") or "",
                    "connects_to": x.get("connects_to") or "",
                }
                for x in interactables
            ],
            "adjacent": adjacent,
        })
    edges = [{"a": r["a"], "b": r["b"]} for r in edge_rows if r.get("a") and r.get("b")]
    return {"locations": locations, "edges": edges}


def _with_item_actions(world, person: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """可交互物品附上它的动作（作者写的 / 已有的），以及此刻对 person 能不能做。"""
    try:
        from ephemeral_action_dsl import load_actions_from_item
        from item_interact import action_options

        options = action_options(world, person, item["name"], load_actions_from_item(world, item["name"]))
    except Exception:
        options = []
    return {**item, "actions": [{k: o[k] for k in ("id", "label", "available", "reason")} for o in options]}


def get_character_actions(world, person: str) -> Dict[str, Any]:
    state = _person_state(world, person)
    if state is None:
        return {"found": False, "name": person}

    loc = state.get("location")
    people_here = _people_at_location(world, loc, exclude=person) if loc else []
    all_ground = _items_at_location(world, loc) if loc else []
    pickable, interactables = _split_ground_items(all_ground)
    interactables = [_with_item_actions(world, person, it) for it in interactables]
    adjacent = _adjacent_locations(world, loc) if loc else []
    locked = _locked_exits(world, loc) if loc else []

    actions = _build_action_entries(world, person, state)
    available_count = sum(1 for a in actions if a["available"])

    return {
        "found": True,
        "character": state,
        "context": {
            "adjacent_locations": adjacent,
            "locked_exits": locked,
            "people_here": people_here,
            "items_here": [x["name"] for x in pickable],
            "items_here_detail": pickable,
            "interactables_here": interactables,
            # Compatibility: all ground entities (including doors)
            "ground_all_detail": all_ground,
        },
        "actions": actions,
        "available_action_count": available_count,
    }


def world_view(world, viewer: Optional[str]) -> Dict[str, Any]:
    """无缝世界的全貌（世界格子坐标）：地形、各地点的地、地上的东西、所有人站在哪。"""
    from spatial import person_cell, region_areas, region_shapes, shape_rows, terrain_of
    from rpg_content import gather_options, gather_node_kinds

    terrain = terrain_of(world)
    areas = region_areas(world)
    node_kinds = gather_node_kinds()
    try:
        from spatial import place_doors

        place_doors(world, areas)  # 门要在门洞里（老存档 / 手摆的世界也对得上）
    except Exception:  # noqa: BLE001
        pass
    shapes = region_shapes(world, areas)
    # 谁控制这块地（势力；地图按它上色）
    controlled = {
        r["n"]: r["c"]
        for r in world.run_query("MATCH (l:Location) WHERE l.controlled_by IS NOT NULL RETURN l.name AS n, l.controlled_by AS c") or []
    }
    regions = []
    items: List[Dict[str, Any]] = []
    owners = {
        r["i"]: r["p"]
        for r in world.run_query("MATCH (p:Person)-[:OWNS]->(i:Item) RETURN i.name AS i, p.name AS p") or []
    }
    for name, (ax, ay, aw, ah) in sorted(areas.items()):
        info = get_location_info(world, name) or {}
        mid = terrain.anchor(name) if terrain else None
        regions.append({
            "name": name, "x": ax, "y": ay, "w": aw, "h": ah,
            # 这个地点自己的地：每行一串 '#'（地）/ '.'（不是：墙、别的地点、嵌在里面的房子）
            "rows": shape_rows(shapes.get(name, set()), (ax, ay, aw, ah)),
            "description": info.get("description") or "",
            "tags": info.get("tags") or "",
            "indoor": bool(terrain and name in terrain.indoor),
            "parent": (terrain.parents.get(name, "") if terrain else ""),
            "controlled_by": controlled.get(name) or "",
            # 标签 / 走过去的落脚处：地点中间的一格
            "cx": mid[0] if mid else None,
            "cy": mid[1] if mid else None,
        })
        for it in _items_at_location(world, name):
            if it.get("x") is None or it.get("y") is None:
                continue
            entry = {k: it.get(k) for k in ("name", "item_kind", "portable", "locked", "description", "w", "h")}
            # 工地还缺什么（"3 log, 2 stone"）、采集点还剩多少
            entry["needs"] = ", ".join(it.get("needs") or [])
            if it.get("stock") is not None:
                entry["stock"] = int(float(it.get("stock") or 0))
            entry.update(region=name, x=ax + int(it["x"]), y=ay + int(it["y"]))
            entry["owner"] = owners.get(it["name"], "")  # 谁的东西（"" = 没主，谁捡归谁）
            if _is_interactable_item(it) and viewer:
                entry["actions"] = _with_item_actions(world, viewer, it)["actions"]
            if viewer and str(it.get("item_kind") or "").lower() in node_kinds:
                # 采集点：采集动作（砍、采、钓……）和作者写的动作并列
                entry["actions"] = list(entry.get("actions") or []) + gather_options(world, viewer, it["name"])
            items.append(entry)
    persons = []
    from combat import hostility

    for c in list_characters(world):
        cell = person_cell(world, c["name"])
        # 这个人为什么把看的人当敌人（"" = 不是敌人）
        hostile = hostility(world, c["name"], viewer) if viewer and viewer != c["name"] else ""
        persons.append({**c, "x": cell[0] if cell else None, "y": cell[1] if cell else None, "hostile": hostile})
    if terrain is not None:
        width, height = terrain.width, terrain.height
        # 整张地形的格子种类：'.' 室外地 ',' 室内地板 '+' 门洞 '#' 墙 '%' 篱笆 '~' 水 ' ' 荒野
        tiles = terrain.kind_rows()
        decor = list(terrain.decor)
    else:
        width = max((x + w for x, _, w, _ in areas.values()), default=0)
        height = max((y + h for _, y, _, h in areas.values()), default=0)
        tiles = []
        decor = []
    # decor：装饰层（土路、石板、麦田……只管好看），和 tiles 一样每行一串
    return {"width": width, "height": height, "regions": regions, "items": items, "persons": persons,
            "tiles": tiles, "decor": decor}


def get_play_state(world) -> Dict[str, Any]:
    """Fetch full play state in one call."""
    player = get_player(world)
    if player is None:
        return {
            "found": False,
            "message": "No player character in the current world. Load a template world, or create an event with the Director.",
            "map": list_locations_graph(world),
        }

    name = player["name"]
    loc = player.get("location")
    actions_data = get_character_actions(world, name)
    context = actions_data.get("context") or {}
    loc_info = get_location_info(world, loc) if loc else None
    quests = []
    try:
        qrows = world.run_query(
            """
            MATCH (q:Quest)
            RETURN q.name AS name,
                   coalesce(q.description, '') AS description,
                   coalesce(q.stage, '') AS stage,
                   coalesce(q.state, '') AS state,
                   coalesce(q.objective, '') AS objective
            ORDER BY name
            """
        )
        quests = [
            {
                "name": r["name"],
                "description": r.get("description") or "",
                "stage": r.get("stage") or "",
                "state": r.get("state") or "",
                "objective": r.get("objective") or "",
            }
            for r in qrows
            if r.get("name")
        ]
    except Exception:
        quests = []

    known_clues = _player_known_clues(world, name)
    try:
        from factions import player_faction_summary

        faction_rows = player_faction_summary(world, name)
    except Exception:
        faction_rows = []
    try:
        from faction_sim import chronicle

        faction_chronicle = chronicle(world, 8)
    except Exception:
        faction_chronicle = []
    try:
        from street_economy import street_summary

        economy = street_summary(world, name)
    except Exception:
        economy = {}

    return {
        "found": True,
        "player": player,
        "current_location": loc_info or {"name": loc, "description": "", "tags": ""},
        "adjacent": context.get("adjacent_locations") or [],
        "locked_exits": context.get("locked_exits") or [],
        "here": {
            "persons": context.get("people_here") or [],
            "items": context.get("items_here_detail")
            or [{"name": n} for n in (context.get("items_here") or [])],
            "interactables": context.get("interactables_here") or [],
        },
        "inventory": player.get("held_items") or [],
        "use_on_by_target": _use_on_catalog_safe(),
        "known_clues": known_clues,
        "available_actions": [
            a for a in (actions_data.get("actions") or []) if a.get("available")
        ],
        "all_actions": actions_data.get("actions") or [],
        "quests": quests,
        "factions": faction_rows,
        "faction_chronicle": faction_chronicle,
        "economy": economy,
        "map": list_locations_graph(world),
        "world": world_view(world, name),
        "simulation": _play_simulation_snapshot(world),
        "characters": list_characters(world),
    }


def _player_known_clues(world, person: str) -> List[Dict[str, Any]]:
    """Known Facts and believed Rumors for the player, for the clues panel."""
    out: List[Dict[str, Any]] = []
    try:
        facts = world.run_query(
            """
            MATCH (p:Person {name: $n})-[:KNOWS_FACT]->(f:Fact)
            RETURN f.name AS name, coalesce(f.text, '') AS text,
                   coalesce(f.secret_level, 0) AS secret_level
            ORDER BY name
            """,
            n=person,
        ) or []
        for r in facts:
            if not r.get("name"):
                continue
            out.append({
                "kind": "fact",
                "name": r["name"],
                "text": r.get("text") or "",
                "secret_level": int(r.get("secret_level") or 0),
            })
        rumors = world.run_query(
            """
            MATCH (p:Person {name: $n})-[:BELIEVES]->(r:Rumor)
            RETURN r.name AS name, coalesce(r.text, '') AS text
            ORDER BY name
            """,
            n=person,
        ) or []
        for r in rumors:
            if not r.get("name"):
                continue
            out.append({
                "kind": "rumor",
                "name": r["name"],
                "text": r.get("text") or "",
                "secret_level": 0,
            })
    except Exception:
        pass
    return out


def _use_on_catalog_safe() -> Dict[str, Any]:
    try:
        from world_interactions import use_on_catalog

        return use_on_catalog()
    except Exception:
        return {}


def _play_simulation_snapshot(world) -> Dict[str, Any]:
    """NPC activity summary for the play page."""
    try:
        from world_simulator import get_world_simulator

        sim = get_world_simulator()
        return {
            "sim_time": sim.sim_time,
            "tick": sim.tick_count,
            "clock": _clock_safe(sim.sim_time),
            "npc_activity": sim.npc_activity_summary(world),
            "chronicle": sim.chronicle(limit=12),
            "story": sim.story(),
            "conflicts": sim.conflicts(limit=8),
            "conversations": sim.conversations(limit=8),
            "mutters": sim.mutters(limit=8),
        }
    except Exception:
        return {"sim_time": 0, "tick": 0, "clock": _clock_safe(0.0), "npc_activity": []}


def _clock_safe(sim_time: float) -> Dict[str, Any]:
    try:
        from routine import clock_info

        return clock_info(float(sim_time or 0.0))
    except Exception:
        return {}
