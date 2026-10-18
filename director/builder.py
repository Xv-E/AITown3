"""
导演构建器 — 将 EventBlueprint 物化为 Neo4j 实体与关系。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .blueprint import EventBlueprint

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

logger = logging.getLogger(__name__)


def _entity_label_for_name(world: "Neo4jWorld", name: str) -> Optional[str]:
    for label in ("Person", "Location", "Item", "Quest", "Fact", "Rumor"):
        try:
            if world.get_entity_by_name(name, label=label):
                return label
        except Exception:
            continue
    return None


def build_from_blueprint(
    world: "Neo4jWorld",
    blueprint: EventBlueprint,
    *,
    clear: bool = True,
) -> Dict[str, Any]:
    """
    按蓝图创建节点与关系。

    clear=True 时先清空全库（与现有 rebuild 行为一致）。
    """
    bp = blueprint.normalized()
    if clear:
        world.clear_all()
        logger.info("Director cleared world before build")

    # World meta
    try:
        world.add_entity(
            "WorldMeta",
            entity_type="Meta",
            title=bp.title,
            premise=bp.premise,
            source=bp.source,
            event_title=bp.title,
        )
    except Exception:
        # Meta 标签可选；失败不阻断
        logger.debug("WorldMeta node skipped", exc_info=True)

    # Locations
    for loc in bp.locations:
        props: Dict[str, Any] = {"description": loc.description}
        if loc.tags:
            props["tags"] = loc.tags
        if loc.map:
            props["map_x"], props["map_y"] = loc.map
        if loc.grid:
            props["grid_w"], props["grid_h"] = loc.grid
        if loc.indoor:
            props["indoor"] = True
        if loc.parent:
            props["parent"] = loc.parent
        if loc.area:
            props["area_x"], props["area_y"] = loc.area[0], loc.area[1]
        world.add_entity(loc.name, entity_type="Location", **props)

    for edge in bp.adjacencies:
        a, b = edge.a, edge.b
        if edge.locked and edge.key:
            world.link_entities(
                a, "Location", b, "Location", "LOCKED_ADJACENT_TO", key=edge.key
            )
            world.link_entities(
                b, "Location", a, "Location", "LOCKED_ADJACENT_TO", key=edge.key
            )
        else:
            world.link_entities(a, "Location", b, "Location", "ADJACENT_TO")
            world.link_entities(b, "Location", a, "Location", "ADJACENT_TO")

    # Persons
    for person in bp.persons:
        props = {
            "hp": person.hp,
            "power_level": person.power_level,
            "personality": person.personality,
            "background": person.background,
            "parent_goal": person.parent_goal,
            "goal": getattr(person, "goal", "") or "",
            "values": person.values,
            "mood": person.mood,
            "role": person.role,
            "is_player": person.role == "player",
            "speech_style": person.speech_style or "",
            "secret_policy": person.secret_policy or "share_gossip",
            "trust_toward_player": int(person.trust_toward_player or 0),
            "brain": getattr(person, "brain", "llm") or "llm",
            "coins": int(getattr(person, "coins", 0) or 0),
            "trader": bool(getattr(person, "trader", False)),
            "sells": list(getattr(person, "sells", []) or []),
            "shop_stock": int(getattr(person, "shop_stock", 1) or 1),
        }
        if getattr(person, "secret", ""):
            props["secret"] = person.secret
        if person.accepts_from:
            props["accepts_from"] = json.dumps(list(person.accepts_from), ensure_ascii=False)
        if getattr(person, "routine", None):
            props["routine"] = json.dumps(list(person.routine), ensure_ascii=False)
        world.add_entity(person.name, entity_type="Person", **props)
        world.link_entities(person.name, "Person", person.located_at, "Location", "LOCATED_AT")
        world.link_entities(person.name, "Person", person.located_at, "Location", "VISITED")
        if person.works_at:
            world.link_entities(person.name, "Person", person.works_at, "Location", "WORKS_AT")

    # Items
    for item in bp.items:
        props: Dict[str, Any] = {
            "value": item.value,
            "portable": True if item.portable is None else bool(item.portable),
        }
        if item.quest_flag:
            props["quest_flag"] = item.quest_flag
        if item.description:
            props["description"] = item.description
        if item.item_kind:
            props["item_kind"] = item.item_kind
        if item.locked:
            props["locked"] = True
        elif item.item_kind == "door":
            props["locked"] = bool(item.locked)
        if item.key_item:
            props["key_item"] = item.key_item
        if item.connects_to:
            props["connects_to"] = item.connects_to
        if item.item_kind == "door":
            props["portable"] = False
            props["locked"] = bool(item.locked)
        if item.effects_on:
            props["effects_on"] = json.dumps(item.effects_on, ensure_ascii=False)
        if item.accepts_from:
            props["accepts_from"] = json.dumps(list(item.accepts_from), ensure_ascii=False)
        if item.stamped:
            props["stamped"] = True
        elif (item.item_kind or "") == "document":
            props["stamped"] = bool(item.stamped)
        if item.fuel is not None:
            props["fuel"] = int(item.fuel)
        if item.lit is not None:
            props["lit"] = bool(item.lit)
        if getattr(item, "respawn_at", None):
            props["respawn_at"] = item.respawn_at
        if getattr(item, "script", None):
            props["script"] = json.dumps(item.script, ensure_ascii=False)
        if item.archetype:
            props["archetype"] = item.archetype
        if item.footprint:
            props["footprint_w"], props["footprint_h"] = item.footprint
        if item.pos:
            props["x"], props["y"] = item.pos
        world.add_entity(item.name, entity_type="Item", **props)
        # script.initial_state → 物品属性（facing/steps/solved 等）
        init = {}
        if isinstance(getattr(item, "script", None), dict):
            init = item.script.get("initial_state") or {}
        if isinstance(init, dict):
            for k, v in init.items():
                if k in ("name", "script", "effects_on", "accepts_from"):
                    continue
                try:
                    world.run_query(
                        f"MATCH (i:Item {{name: $n}}) SET i.`{k}` = $v",
                        n=item.name,
                        v=v,
                    )
                except Exception:
                    pass
        if item.held_by and item.item_kind != "door" and item.portable is not False:
            world.link_entities(item.held_by, "Person", item.name, "Item", "HAS_ITEM")
        elif item.located_at:
            world.link_entities(item.name, "Item", item.located_at, "Location", "LOCATED_AT")
        if item.owned_by:
            world.link_entities(item.owned_by, "Person", item.name, "Item", "OWNS")

    # Quests
    for quest in bp.quests:
        world.add_entity(
            quest.name,
            entity_type="Quest",
            description=quest.description,
            stage=quest.stage,
            state=quest.state,
            objective=quest.objective,
        )
        if quest.giver:
            world.link_entities(quest.giver, "Person", quest.name, "Quest", "GIVES_QUEST")
        for target in quest.involves:
            label = _entity_label_for_name(world, target)
            if label:
                world.link_entities(quest.name, "Quest", target, label, "INVOLVES")

    # Facts
    for fact in bp.facts:
        world.add_entity(
            fact.name,
            entity_type="Fact",
            text=fact.text,
            secret_level=fact.secret_level,
        )
        for person in fact.known_by:
            if world.get_entity_by_name(person, label="Person"):
                world.link_entities(person, "Person", fact.name, "Fact", "KNOWS_FACT")

    # Rumors
    for rumor in bp.rumors:
        props: Dict[str, Any] = {"text": rumor.text}
        world.add_entity(rumor.name, entity_type="Rumor", **props)
        if rumor.about and world.get_entity_by_name(rumor.about, label="Fact"):
            world.link_entities(rumor.name, "Rumor", rumor.about, "Fact", "ABOUT_FACT")
        for person in rumor.believed_by:
            if world.get_entity_by_name(person, label="Person"):
                world.link_entities(person, "Person", rumor.name, "Rumor", "BELIEVES")

    # Social
    item_names = {i.name for i in bp.items}
    for a, rel, b in bp.social:
        if b in item_names:
            world.link_entities(a, "Person", b, "Item", rel)
        else:
            world.link_entities(a, "Person", b, "Person", rel)
            if rel == "FRIENDS_WITH":
                world.link_entities(b, "Person", a, "Person", "FRIENDS_WITH")

    try:
        import sys
        from pathlib import Path

        actions_path = str(Path(__file__).resolve().parent.parent / "actions")
        if actions_path not in sys.path:
            sys.path.insert(0, actions_path)
        from spatial import layout_regions, place_people, place_unplaced, save_authored_terrain

        # 先把地点落到同一张地形上（手画的地图直接用，否则按连接关系生成），再摆物品、给人落脚点
        if bp.terrain:
            save_authored_terrain(world, bp.terrain)
        layout_regions(world)
        place_unplaced(world)
        place_people(world)
    except Exception as e:
        logger.warning("格子布局失败: %s", e)

    try:
        import sys
        from pathlib import Path

        agent_path = str(Path(__file__).resolve().parent.parent / "agent")
        if agent_path not in sys.path:
            sys.path.insert(0, agent_path)
        from knowledge import seed

        for person in bp.persons:
            seed(world, person.name, person.knows)
    except Exception as e:
        logger.warning("开局知识失败: %s", e)

    summary = {
        "ok": True,
        "title": bp.title,
        "premise": bp.premise,
        "source": bp.source,
        "counts": {
            "locations": len(bp.locations),
            "persons": len(bp.persons),
            "items": len(bp.items),
            "quests": len(bp.quests),
            "facts": len(bp.facts),
            "rumors": len(bp.rumors),
            "adjacencies": len(bp.adjacencies),
            "social": len(bp.social),
        },
        "blueprint": bp.to_dict(),
    }
    # 编译本世界临时交互（effects_on × accepts_from）
    try:
        import sys
        from pathlib import Path

        actions_path = str(Path(__file__).resolve().parent.parent / "actions")
        if actions_path not in sys.path:
            sys.path.insert(0, actions_path)
        from world_interactions import (
            clear_interactions,
            compile_interactions_from_blueprint,
            refresh_interactions_from_world,
            set_interactions,
        )

        clear_interactions()
        compiled = compile_interactions_from_blueprint(bp)
        if compiled:
            set_interactions(compiled)
        else:
            compiled = refresh_interactions_from_world(world)
        summary["counts"]["interactions"] = len(compiled)
    except Exception as e:
        logger.warning("编译世界交互失败: %s", e)
        summary["interactions_error"] = str(e)

    logger.info(
        "Director built event '%s' (%s locs, %s persons, %s items, %s quests)",
        bp.title,
        summary["counts"]["locations"],
        summary["counts"]["persons"],
        summary["counts"]["items"],
        summary["counts"]["quests"],
    )
    return summary


def plan_and_build(
    world: "Neo4jWorld",
    prompt: str,
    *,
    clear: bool = True,
    force_mock: bool = False,
    template_id: Optional[str] = None,
    world_id: Optional[str] = None,
    draft: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """规划 + 构建一站式入口。也可直接指定 world_id 加载模板世界。"""
    from .planner import plan_event
    from .world_loader import load_world

    wid = (world_id or template_id or "").strip() or None
    if wid and not (prompt or "").strip():
        blueprint = load_world(wid)
    else:
        blueprint = plan_event(
            prompt,
            force_mock=force_mock,
            template_id=template_id,
            world_id=world_id,
            draft=draft,
        )
    return build_from_blueprint(world, blueprint, clear=clear)
