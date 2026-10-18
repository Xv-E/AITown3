"""
把验证过的任务线写进世界：只增不删（不清空世界），新区域贴着已有地图排好。
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .spec import QuestlinePatch, item_props


def apply_questline(world, patch: QuestlinePatch) -> Dict[str, Any]:
    from spatial import layout_regions, place_people, place_unplaced

    for r in patch.regions:
        world.add_entity(r.name, entity_type="Location", description=r.description, tags=r.tags,
                         grid_w=int(r.w), grid_h=int(r.h))
        world.link_entities(r.name, "Location", r.next_to, "Location", "ADJACENT_TO")
        world.link_entities(r.next_to, "Location", r.name, "Location", "ADJACENT_TO")

    for p in patch.persons:
        world.add_entity(
            p.name, entity_type="Person",
            hp=100, power_level=10, role="npc", is_player=False,
            personality=p.personality, background=p.background, goal=p.goal, mood=p.mood,
            secret=p.secret, speech_style=p.speech_style,
            secret_policy="share_gossip", trust_toward_player=0, brain="llm",  # 故事里的人用大模型想事情
            coins=5, trader=False,
            **({"routine": json.dumps(p.routine, ensure_ascii=False)} if p.routine else {}),
        )
        world.link_entities(p.name, "Person", p.located_at, "Location", "LOCATED_AT")
        world.link_entities(p.name, "Person", p.located_at, "Location", "VISITED")

    for u in patch.person_updates:
        fields = {k: v for k, v in (("goal", u.goal), ("secret", u.secret), ("background", u.background)) if v}
        if fields:
            world.run_query("MATCH (p:Person {name: $n}) SET p += $f", n=u.name, f=fields)

    for it in patch.items:
        world.add_entity(it.name, entity_type="Item", **item_props(it))
        if it.held_by:
            world.link_entities(it.held_by, "Person", it.name, "Item", "HAS_ITEM")
        else:
            world.link_entities(it.name, "Item", it.located_at, "Location", "LOCATED_AT")

    for c in patch.clues:
        label, rel = ("Fact", "KNOWS_FACT") if c.kind == "fact" else ("Rumor", "BELIEVES")
        world.add_entity(c.name, entity_type=label, text=c.text, secret_level=1)
        for who in c.known_by:
            world.link_entities(who, "Person", c.name, label, rel)

    # 新区域落到地图上，再摆物品、给新人落脚点（已有的都不动）
    layout_regions(world)
    place_unplaced(world)
    place_people(world)

    name = patch.quest_name()
    first = patch.stages[0]
    world.add_entity(
        name, entity_type="Quest",
        title=patch.title, description=patch.premise, hook=patch.hook,
        stage=first.title, objective=first.objective, state="active",
        questline=True, current=0,
        stages=json.dumps([s.__dict__ for s in patch.stages], ensure_ascii=False),
    )
    if first.giver:
        world.link_entities(first.giver, "Person", name, "Quest", "GIVES_QUEST")
    return {
        "quest": name,
        "title": patch.title,
        "added": {
            "regions": [r.name for r in patch.regions],
            "persons": [p.name for p in patch.persons],
            "items": [i.name for i in patch.items],
            "clues": [c.name for c in patch.clues],
        },
        "first_objective": first.objective,
    }
