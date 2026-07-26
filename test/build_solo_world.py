"""
单角色最小测试世界 — 用于隔离测试一名角色的规划能力。

地图（4 地点）：
    TownSquare — Tavern
    TownSquare — Forest — Castle

默认角色 Alice @ TownSquare，已访问 Tavern，未访问 Forest/Castle。
地上物品 Key @ Forest。

运行（仓库根目录）：
    python AITown3/test/build_solo_world.py
    python AITown3/test/build_solo_world.py --character Bob --no-reset
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AITOWN3 = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for sub in ("neo4j",):
    p = os.path.join(_AITOWN3, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from neo4j_world import Neo4jWorld  # noqa: E402

DEFAULT_CHARACTER = "Alice"

LOCATIONS: List[Tuple[str, str]] = [
    ("TownSquare", "The center of the solo test town."),
    ("Tavern", "A quiet tavern for testing."),
    ("Forest", "A forest with a hidden key."),
    ("Castle", "A castle on the hill."),
]

ADJACENCIES: List[Tuple[str, str]] = [
    ("TownSquare", "Tavern"),
    ("TownSquare", "Forest"),
    ("Forest", "Castle"),
]

ITEMS: List[Tuple[str, int]] = [
    ("Key", 80),
    ("Apple", 5),
]

ITEMS_AT_LOCATION: List[Tuple[str, str]] = [
    ("Key", "Forest"),
]


def build_solo_world(
    world: Neo4jWorld,
    *,
    character: str = DEFAULT_CHARACTER,
    start_location: str = "TownSquare",
    hp: int = 100,
    power_level: int = 10,
) -> None:
    """构建仅含一名可玩角色 + 4 地点 + 2 物品的最小世界。"""
    for name, desc in LOCATIONS:
        world.add_entity(name, entity_type="Location", description=desc)

    for a, b in ADJACENCIES:
        world.link_entities(a, "Location", b, "Location", "ADJACENT_TO")
        world.link_entities(b, "Location", a, "Location", "ADJACENT_TO")

    world.add_entity(
        character,
        entity_type="Person",
        hp=hp,
        power_level=power_level,
        personality="curious, brave, determined to explore every unknown place",
        background=(
            f"{character} is a lone adventurer testing autonomous planning "
            "in a minimal town — driven to explore, collect, and survive."
        ),
        parent_goal="explore all unknown locations and collect the key in the forest",
        values="curiosity,courage,persistence",
        mood="focused",
    )
    world.link_entities(character, "Person", start_location, "Location", "LOCATED_AT")
    world.link_entities(character, "Person", "Tavern", "Location", "VISITED")

    for name, value in ITEMS:
        world.add_entity(name, entity_type="Item", value=value)

    world.link_entities(character, "Person", "Apple", "Item", "HAS_ITEM")
    for item, loc in ITEMS_AT_LOCATION:
        world.link_entities(item, "Item", loc, "Location", "LOCATED_AT")


def verify_solo_world(world: Neo4jWorld, character: str = DEFAULT_CHARACTER) -> bool:
    persons = world.list_entities("Person")
    if persons != [character]:
        print(f"  [FAIL] 期望仅 1 名角色 {character}，实际: {persons}")
        return False
    print(f"  [OK] 仅 1 名角色: {character}")

    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc",
        n=character,
    )
    loc = rows[0]["loc"] if rows else "?"
    print(f"  [OK] {character} 位于 {loc}")

    rows2 = world.run_query(
        "MATCH (i:Item {name: 'Key'})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc"
    )
    key_loc = rows2[0]["loc"] if rows2 else "?"
    print(f"  [OK] Key 位于 {key_loc}")

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="构建单角色规划测试世界")
    parser.add_argument("--character", default=DEFAULT_CHARACTER)
    parser.add_argument("--no-reset", action="store_true")
    args = parser.parse_args()

    try:
        world = Neo4jWorld.get_instance()
    except Exception as e:
        print(f"无法连接 Neo4j: {e}")
        return 1

    try:
        if not args.no_reset:
            print("清空数据库并构建单角色世界...")
            world.clear_all()
        build_solo_world(world, character=args.character)
        ok = verify_solo_world(world, character=args.character)
        print("\n可在 Neo4j Browser 查看：")
        print("  MATCH (n)-[r]-(m) RETURN n, r, m")
        print("\n运行规划测试：")
        print("  python AITown3/test/test_solo_planner.py")
        return 0 if ok else 1
    finally:
        world.close()


if __name__ == "__main__":
    sys.exit(main())
