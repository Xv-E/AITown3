"""
建造蓝图 — 内容原型里带 "blueprint" 字段的就是可放置的蓝图（通常是工地）。

    "blueprint": {
      "label": "Workshop",
      "description": "...",
      "requires": [ 条件... ],     # ctx: @actor = 放置者, @here = 当前区域
      "zone_tags": ["build"],      # 可选：只有带这些 tag 的区域能放
      "order": 10
    }

引擎只负责：列出蓝图、检查条件和占地、在格子上生成物品。之后怎么建由内容（工地的 alerts / rule）决定。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld


def _blueprint_archetypes() -> List[Tuple[str, Dict[str, Any]]]:
    from content import _resolve_archetype, get_content

    out = []
    for name in sorted(get_content().archetypes):
        proto = _resolve_archetype(name, depth=0) or {}
        bp = proto.get("blueprint")
        if isinstance(bp, dict):
            out.append((name, proto))
    out.sort(key=lambda kv: (int(kv[1]["blueprint"].get("order", 100)), kv[0]))
    return out


def _zone_tags(world: "Neo4jWorld", location: str) -> List[str]:
    rows = world.run_query(
        "MATCH (l:Location {name: $n}) RETURN coalesce(l.tags, '') AS tags LIMIT 1", n=location
    ) or []
    raw = str(rows[0].get("tags") or "") if rows else ""
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def list_blueprints(world: "Neo4jWorld", actor: str, location: str) -> List[Dict[str, Any]]:
    from graph_ops import check_all
    import spatial

    tags = set(_zone_tags(world, location))
    out = []
    for name, proto in _blueprint_archetypes():
        bp = proto["blueprint"]
        w, h = spatial.footprint_of(proto)
        ok, reason = True, ""
        need_tags = [str(t).lower() for t in bp.get("zone_tags") or []]
        if need_tags and not tags.intersection(need_tags):
            ok, reason = False, f"Can only be built in: {', '.join(need_tags)}"
        if ok:
            ok, why = check_all(world, list(bp.get("requires") or []), {"actor": actor, "here": location})
            if not ok:
                reason = why
        out.append({
            "archetype": name,
            "label": str(bp.get("label") or proto.get("name") or name),
            "description": str(bp.get("description") or proto.get("description") or ""),
            "w": w,
            "h": h,
            "available": ok,
            "reason": reason,
        })
    return out


def place_blueprint(
    world: "Neo4jWorld",
    actor: str,
    archetype: str,
    x: int,
    y: int,
    *,
    location: Optional[str] = None,
) -> Dict[str, Any]:
    from action import get_person_location
    from graph_ops import apply_op
    import spatial

    loc = location or get_person_location(world, actor)
    if not loc:
        return {"success": False, "message": f"{actor} is nowhere"}
    entry = next((b for b in list_blueprints(world, actor, loc) if b["archetype"] == archetype), None)
    if entry is None:
        return {"success": False, "message": f"Unknown blueprint: {archetype}"}
    if not entry["available"]:
        return {"success": False, "message": f"Can't build {entry['label']}: {entry['reason']}"}
    ok, why = spatial.can_place(world, loc, int(x), int(y), entry["w"], entry["h"])
    if not ok:
        return {"success": False, "message": f"Can't build {entry['label']} here: {why}"}
    before = set(world.list_entities("Item") or [])
    apply_op(
        world,
        {"op": "spawn_item", "archetype": archetype, "located_at": loc, "x": int(x), "y": int(y)},
        {"actor": actor, "here": loc},
    )
    created = sorted(set(world.list_entities("Item") or []) - before)
    try:
        from world_interactions import refresh_interactions_from_world

        refresh_interactions_from_world(world)
    except Exception:
        pass
    return {
        "success": True,
        "message": f"{actor} marked out a {entry['label']} in {loc}.",
        "item": created[0] if created else None,
        "location": loc,
    }
