"""
Neo4j 世界构建测试脚本

仅依赖 `AITown3/neo4j/neo4j_world.py` 中的 Neo4jWorld API，
从零搭建一个小型 AI Town 知识图谱。

节点：
    Locations: TownSquare, Tavern, Forest, Castle, Market
    Persons:   Alice, Bob, Guard, Merchant, Elder
    Items:     Sword, Apple, Key, Book, Potion

关系：
    Location -[:ADJACENT_TO]- Location          (双向，物理相邻)
    Person   -[:LOCATED_AT]-> Location          (当前所在地)
    Person   -[:WORKS_AT]->   Location          (工作地点)
    Person   -[:VISITED]->    Location          (曾经去过)
    Person   -[:HAS_ITEM]->   Item              (临时持有)
    Person   -[:OWNS]->       Item              (永久所有权)
    Item     -[:LOCATED_AT]-> Location          (无主物品的位置)
    Person   -[:KNOWS]->      Person            (相识)
    Person   -[:FRIENDS_WITH]- Person           (朋友，双向)
    Person   -[:LIKES]->     Item               (喜欢的物品)
    Person   -[:LIKES]->     Person             (好感)

默认行为：
  - 开始前清空整个数据库 (clear_all)
  - 构建完成后保留数据，方便在 Neo4j Browser 里查看

运行（仓库根目录）：
    python AITown3/test/test_build_neo4j.py
    python AITown3/test/test_build_neo4j.py --no-reset   # 跳过开头的清空
    python AITown3/test/test_build_neo4j.py --cleanup    # 验证后再删除测试节点
"""

import argparse
import os
import sys
from typing import List, Tuple, Dict

# 把 `AITown3/neo4j/` 加进 sys.path，避免本地 neo4j 目录覆盖 PyPI 的 neo4j 包
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_NEO4J_DIR = os.path.abspath(os.path.join(_THIS_DIR, "..", "neo4j"))
if _NEO4J_DIR not in sys.path:
    sys.path.insert(0, _NEO4J_DIR)

from neo4j_world import Neo4jWorld  # noqa: E402


# ---------- 节点定义 ----------

# (name, description)
LOCATIONS: List[Tuple[str, str]] = [
    ("TownSquare", "The bustling center of the town."),
    ("Tavern", "A cozy tavern with a fireplace."),
    ("Forest", "A dense forest east of the town."),
    ("Castle", "An old stone castle on the hill."),
    ("Market", "A noisy open-air market full of stalls."),
]

# (name, hp, power_level, located_at)
PERSONS: List[Tuple[str, int, int, str]] = [
    ("Alice",    100, 12, "TownSquare"),
    ("Bob",       90,  8, "Tavern"),
    ("Guard",    120, 15, "Castle"),
    ("Merchant",  85,  6, "Market"),
    ("Elder",     70,  4, "Castle"),
]

# 角色设定 — 写入 Neo4j，供自主规划读取
CHARACTER_PROFILES: Dict[str, Dict[str, str]] = {
    "Alice": {
        "personality": "curious, brave, loves exploring and uncovering secrets",
        "background": "A young adventurer who came to town seeking ancient mysteries in the forest and castle.",
        "parent_goal": "explore every corner of the town and uncover hidden mysteries",
        "values": "curiosity,honor,friendship",
        "mood": "excited",
    },
    "Bob": {
        "personality": "friendly, talkative, enjoys gossip and helping travelers",
        "background": "Tavern keeper who knows everyone's stories and rumors in town.",
        "parent_goal": "meet people, share rumors, and keep the tavern lively",
        "values": "friendship,hospitality,honesty",
        "mood": "cheerful",
    },
    "Guard": {
        "personality": "disciplined, protective, suspicious of strangers",
        "background": "Castle guard sworn to protect the Elder and maintain order.",
        "parent_goal": "patrol the castle and defeat any threat to the town",
        "values": "duty,honor,courage",
        "mood": "alert",
    },
    "Merchant": {
        "personality": "shrewd, greedy, always looking for valuable items",
        "background": "Market trader who collects rare goods from adventurers.",
        "parent_goal": "collect valuable items and grow wealth at the market",
        "values": "wealth,pragmatism,negotiation",
        "mood": "calculating",
    },
    "Elder": {
        "personality": "wise, calm, speaks in riddles about the town's history",
        "background": "Old scholar living in the castle, keeper of forgotten lore.",
        "parent_goal": "gain information from visitors and preserve ancient knowledge",
        "values": "wisdom,patience,knowledge",
        "mood": "contemplative",
    },
}

# (name, value)
ITEMS: List[Tuple[str, int]] = [
    ("Sword", 150),
    ("Apple",   5),
    ("Key",    80),
    ("Book",   30),
    ("Potion", 60),
]


# ---------- 关系定义 ----------

# 双向相邻
ADJACENCIES: List[Tuple[str, str]] = [
    ("TownSquare", "Tavern"),
    ("TownSquare", "Forest"),
    ("TownSquare", "Castle"),
    ("TownSquare", "Market"),
    ("Forest",     "Castle"),
    ("Tavern",     "Market"),
]

# Person -[:WORKS_AT]-> Location
WORKS_AT: List[Tuple[str, str]] = [
    ("Bob",      "Tavern"),
    ("Guard",    "Castle"),
    ("Merchant", "Market"),
    ("Elder",    "Castle"),
]

# Person -[:VISITED]-> Location
VISITED: List[Tuple[str, str]] = [
    ("Alice", "Forest"),
    ("Alice", "Tavern"),
    ("Alice", "Market"),
    ("Bob",   "Forest"),
]

# Person -[:HAS_ITEM]-> Item（临时持有）
HAS_ITEM: List[Tuple[str, str]] = [
    ("Alice", "Apple"),
    ("Guard", "Sword"),
]

# Person -[:OWNS]-> Item（永久所有权）
OWNS: List[Tuple[str, str]] = [
    ("Alice",    "Book"),
    ("Guard",    "Sword"),
    ("Merchant", "Potion"),
]

# Item -[:LOCATED_AT]-> Location（无主物品）
ITEMS_AT_LOCATION: List[Tuple[str, str]] = [
    ("Key", "Forest"),
]

# 单向认识 Person -[:KNOWS]-> Person
KNOWS: List[Tuple[str, str]] = [
    ("Alice", "Bob"),
    ("Alice", "Guard"),
    ("Alice", "Merchant"),
    ("Bob",   "Alice"),
    ("Guard", "Elder"),
]

# 双向朋友 Person -[:FRIENDS_WITH]- Person
FRIENDS: List[Tuple[str, str]] = [
    ("Alice", "Bob"),
    ("Guard", "Elder"),
]

# Person -[:LIKES]-> Person/Item
LIKES_PERSON: List[Tuple[str, str]] = [
    ("Alice", "Bob"),
    ("Bob",   "Alice"),
]
LIKES_ITEM: List[Tuple[str, str]] = [
    ("Alice", "Book"),
    ("Bob",   "Apple"),
    ("Guard", "Sword"),
]


def _hr(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def build_world(world: Neo4jWorld) -> None:
    _hr("Step 1: 创建位置 (Location)")
    for name, desc in LOCATIONS:
        world.add_entity(name, entity_type="Location", description=desc)

    _hr("Step 2: 位置相邻 (ADJACENT_TO，双向)")
    for a, b in ADJACENCIES:
        world.link_entities(a, "Location", b, "Location", "ADJACENT_TO")
        world.link_entities(b, "Location", a, "Location", "ADJACENT_TO")

    _hr("Step 3: 创建 NPC (Person) 并设置当前位置")
    for name, hp, power, loc in PERSONS:
        profile = CHARACTER_PROFILES.get(name, {})
        world.add_entity(
            name,
            entity_type="Person",
            hp=hp,
            power_level=power,
            **profile,
        )
        world.link_entities(name, "Person", loc, "Location", "LOCATED_AT")

    _hr("Step 4: 工作地点 (WORKS_AT)")
    for person, loc in WORKS_AT:
        world.link_entities(person, "Person", loc, "Location", "WORKS_AT")

    _hr("Step 5: 拜访过的地点 (VISITED)")
    for person, loc in VISITED:
        world.link_entities(person, "Person", loc, "Location", "VISITED")

    _hr("Step 6: 创建物品 (Item)")
    for name, value in ITEMS:
        world.add_entity(name, entity_type="Item", value=value)

    _hr("Step 7: 物品归属 (HAS_ITEM 临时持有 / OWNS 永久所有)")
    for person, item in HAS_ITEM:
        world.link_entities(person, "Person", item, "Item", "HAS_ITEM")
    for person, item in OWNS:
        world.link_entities(person, "Person", item, "Item", "OWNS")
    for item, loc in ITEMS_AT_LOCATION:
        world.link_entities(item, "Item", loc, "Location", "LOCATED_AT")

    _hr("Step 8: 社交关系 (KNOWS / FRIENDS_WITH)")
    for a, b in KNOWS:
        world.link_entities(a, "Person", b, "Person", "KNOWS")
    for a, b in FRIENDS:
        world.link_entities(a, "Person", b, "Person", "FRIENDS_WITH")
        world.link_entities(b, "Person", a, "Person", "FRIENDS_WITH")

    _hr("Step 9: 喜好 (LIKES)")
    for a, b in LIKES_PERSON:
        world.link_entities(a, "Person", b, "Person", "LIKES")
    for a, b in LIKES_ITEM:
        world.link_entities(a, "Person", b, "Item", "LIKES")


def verify_world(world: Neo4jWorld) -> bool:
    _hr("Verify: 节点统计")

    locations = world.list_entities("Location")
    persons = world.list_entities("Person")
    items = world.list_entities("Item")

    print(f"Locations ({len(locations)}): {locations}")
    print(f"Persons   ({len(persons)}): {persons}")
    print(f"Items     ({len(items)}): {items}")

    ok = (
        len(locations) >= len(LOCATIONS)
        and len(persons) >= len(PERSONS)
        and len(items) >= len(ITEMS)
    )
    if not ok:
        print("节点数量不足，构建可能失败")
        return False

    _hr("Verify: 关系断言")
    checks = [
        ("TownSquare", "Location", "Tavern", "Location", "ADJACENT_TO"),
        ("Alice", "Person", "TownSquare", "Location", "LOCATED_AT"),
        ("Bob", "Person", "Tavern", "Location", "WORKS_AT"),
        ("Alice", "Person", "Forest", "Location", "VISITED"),
        ("Guard", "Person", "Sword", "Item", "HAS_ITEM"),
        ("Alice", "Person", "Book", "Item", "OWNS"),
        ("Key", "Item", "Forest", "Location", "LOCATED_AT"),
        ("Alice", "Person", "Bob", "Person", "KNOWS"),
        ("Alice", "Person", "Bob", "Person", "FRIENDS_WITH"),
        ("Alice", "Person", "Book", "Item", "LIKES"),
    ]
    for a, la, b, lb, rel in checks:
        present = world.has_relationship(a, la, b, lb, rel)
        flag = "OK     " if present else "MISSING"
        print(f"  [{flag}] ({a}:{la}) -[{rel}]- ({b}:{lb})")
        if not present:
            ok = False

    _hr("Verify: 自然语言关系文本（以 Alice 为中心）")
    print(world.get_entity_relationships_text("Alice", label="Person"))

    _hr("Verify: Alice 的实体上下文（属性 + 关系）")
    print(world.get_entity_context_text("Alice", label="Person"))

    return ok


def cleanup(world: Neo4jWorld) -> None:
    _hr("Cleanup: 删除本测试创建的所有节点")

    for name, *_ in PERSONS:
        try:
            world.delete_entity(name, label="Person")
        except Exception as e:
            print(f"  delete Person({name}) 失败: {e}")
    for name, *_ in ITEMS:
        try:
            world.delete_entity(name, label="Item")
        except Exception as e:
            print(f"  delete Item({name}) 失败: {e}")
    for name, _ in LOCATIONS:
        try:
            world.delete_entity(name, label="Location")
        except Exception as e:
            print(f"  delete Location({name}) 失败: {e}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建并验证 AITown3 测试用 Neo4j 世界")
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="跳过开头的全库清空（默认会先 clear_all）",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="验证完成后删除本测试创建的节点（默认保留）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        world = Neo4jWorld.get_instance()
    except Exception as e:
        print(f"无法连接 Neo4j: {e}")
        return 1

    try:
        if not args.no_reset:
            print("WARNING: 默认会清空整个数据库的所有节点和关系（使用 --no-reset 跳过）")
            world.clear_all()

        build_world(world)
        ok = verify_world(world)

        if args.cleanup:
            cleanup(world)
        else:
            print("\n默认保留数据。可在 Neo4j Browser 中查询：")
            print("  MATCH (n)-[r]-(m) RETURN n, r, m")

        if ok:
            print("\nBuild + Verify 成功。")
            return 0
        print("\n验证未全部通过。")
        return 1
    finally:
        world.close()


if __name__ == "__main__":
    sys.exit(main())
