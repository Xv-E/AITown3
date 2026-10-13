"""人物知识演示（需要 Neo4j，会清空数据库）：开局知识 → 信念过时 → 对话里听来的消息。

用法:  python test/run_knowledge_demo.py

  Camp ── Forest ── DeepWoods
   │
  Quarry

Kai 和 Oren 在 Camp。Kai 只知道 Camp 附近（Forest 里的 Log1）；Oren 知道 DeepWoods 里有 Log2。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("", "neo4j", "actions", "planner", "agent", "utility", "memory", "director", "web"):
    p = str(ROOT / sub) if sub else str(ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)

WORLD = {
    "title": "Knowledge Demo",
    "premise": "Two settlers look for firewood.",
    "locations": [
        {"name": "Camp", "description": "A small camp."},
        {"name": "Forest", "description": "Pines near the camp."},
        {"name": "DeepWoods", "description": "Old woods beyond the forest."},
        {"name": "Quarry", "description": "Rocks."},
    ],
    "adjacencies": [
        {"a": "Camp", "b": "Forest"},
        {"a": "Forest", "b": "DeepWoods"},
        {"a": "Camp", "b": "Quarry"},
    ],
    "persons": [
        {"name": "Kai", "located_at": "Camp"},
        {"name": "Oren", "located_at": "Camp", "knows": {"items": ["Log2"]}},
    ],
    "items": [
        {"name": "Log1", "located_at": "Forest", "archetype": "log"},
        {"name": "Log2", "located_at": "DeepWoods", "archetype": "log"},
    ],
}


def main() -> None:
    import logging

    logging.disable(logging.WARNING)
    from director import build_from_blueprint
    from director.blueprint import EventBlueprint
    from goal_planner import plan_for_goal
    from knowledge import current_location, describe_sighting, load_knowledge, observe, share_from_dialogue
    from neo4j_world import Neo4jWorld
    from registry import get_registry

    world = Neo4jWorld()
    build_from_blueprint(world, EventBlueprint.from_dict(WORLD), clear=True)
    reg = get_registry()
    goal = {"goal": "Collect item", "target_entity": "kind:log"}

    def show(who: str) -> None:
        k = load_knowledge(world, who)
        items = ", ".join(f"{i}@{h or l}" for i, (l, h) in sorted(k.items.items())) or "-"
        print(f"   {who} knows places {sorted(k.places)}; items {items}")

    def plan(title: str):
        r = plan_for_goal(world, "Kai", goal)
        print(f"\n[{title}] Kai plans 'Collect kind:log':",
              r.plan if r.success else f"FAILED: {r.error}")
        return r.plan if r.success else []

    def act(steps) -> None:
        for name, args in steps:
            a = reg.get(name)
            ok, msg = reg.execute(world, name, **{p.name: v for p, v in zip(a.parameters, args)})
            loc = current_location(world, "Kai")
            new, gone, kinds = observe(world, "Kai", loc, t=1.0, places=True)
            seen = describe_sighting(loc, new, gone, kinds)
            print(f"   {name}{tuple(args)} -> {'ok' if ok else 'FAILED: ' + msg}"
                  + (f"   | observes: {seen}" if seen else ""))
            if not ok:
                return

    print("1) 开局知识")
    show("Kai")
    show("Oren")

    print("\n2) 信念过时：Kai 还没出门，有人把 Log1 搬去了 Quarry")
    world.run_query(
        """
        MATCH (:Item {name: 'Log1'})-[r:LOCATED_AT]->() DELETE r
        WITH 1 AS _ MATCH (i:Item {name: 'Log1'}), (l:Location {name: 'Quarry'})
        MERGE (i)-[:LOCATED_AT]->(l)
        """
    )
    act(plan("仍以为 Log1 在 Forest"))
    show("Kai")
    plan("到现场发现不在了，重新规划")

    print("\n3) Oren 告诉 Kai：\"There are logs out in the DeepWoods.\"")
    lines = [{"speaker": "Oren", "text": "There are logs out in the DeepWoods."}]
    print("   learned:", share_from_dialogue(world, lines, "Oren", "Kai"))
    show("Kai")
    act(plan("听说之后"))
    print("\n   Kai now holds:", [r["i"] for r in world.run_query(
        "MATCH (:Person {name: 'Kai'})-[:HAS_ITEM]->(i) RETURN i.name AS i")])


if __name__ == "__main__":
    main()
