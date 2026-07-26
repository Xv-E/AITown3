"""
动作空间测试

流程：
  1. 清空 + 构建世界（沿用 test_build_neo4j 中的常量）
  2. 列出注册表里的所有动作 + 打印 PDDL 块
  3. 串行执行一组动作，并在每步后断言图状态
  4. 验证 precondition 失败的负面用例
  5. 默认保留数据，方便在 Neo4j Browser 看结果

运行（仓库根目录）：
    python AITown3/test/test_actions.py
"""

import os
import sys
from typing import Tuple

# 把 neo4j/ 与 actions/ 都加到 sys.path（精确到子目录，避开 PyPI neo4j 包冲突）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AITOWN3 = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for sub in ("neo4j", "actions", "test"):
    p = os.path.join(_AITOWN3, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from neo4j_world import Neo4jWorld  # noqa: E402
from registry import get_registry  # noqa: E402

# 复用 build 脚本里的世界数据
import test_build_neo4j as builder  # noqa: E402


def _hr(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def _expect(ok: bool, msg: str) -> bool:
    flag = "OK     " if ok else "FAIL   "
    print(f"  [{flag}] {msg}")
    return ok


def run_action(world, name: str, **kwargs) -> Tuple[bool, str]:
    """执行动作并打印结果。"""
    reg = get_registry()
    ok, reason = reg.execute(world, name, **kwargs)
    flag = "OK   " if ok else "DENY "
    args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    print(f"  [{flag}] {name}({args}) -> {reason}")
    return ok, reason


def setup_world(world: Neo4jWorld) -> None:
    _hr("Setup: clear + build world")
    world.clear_all()
    builder.build_world(world)


def test_introspection() -> bool:
    _hr("Introspection: 注册表中的动作")
    reg = get_registry()
    print(f"已注册 {len(reg.names())} 个动作: {reg.names()}")

    print("\n--- 动作签名 ---")
    for action in reg.all():
        print(f"  {action.signature():<60} [{action.category}]")

    print("\n--- PDDL 描述（前 2 个） ---")
    for action in reg.all()[:2]:
        print(action.to_pddl())
        print()
    return len(reg.names()) == 8


def test_happy_path(world: Neo4jWorld) -> bool:
    _hr("Happy path: 一连串成功动作")
    reg = get_registry()
    all_ok = True

    # Alice 在 TownSquare，先去 Forest 把 Key 捡起来
    ok, _ = run_action(world, "move", c="Alice", **{"from": "TownSquare", "to": "Forest"})
    all_ok &= ok
    all_ok &= _expect(
        world.has_relationship("Alice", "Person", "Forest", "Location", "LOCATED_AT"),
        "Alice 已位于 Forest",
    )
    all_ok &= _expect(
        not world.has_relationship("Alice", "Person", "TownSquare", "Location", "LOCATED_AT"),
        "Alice 不再位于 TownSquare",
    )
    all_ok &= _expect(
        world.has_relationship("Alice", "Person", "Forest", "Location", "VISITED"),
        "Alice 留下 VISITED Forest 的记录",
    )

    ok, _ = run_action(world, "pick_up", c="Alice", i="Key", l="Forest")
    all_ok &= ok
    all_ok &= _expect(
        world.has_relationship("Alice", "Person", "Key", "Item", "HAS_ITEM"),
        "Alice 持有 Key",
    )
    all_ok &= _expect(
        not world.has_relationship("Key", "Item", "Forest", "Location", "LOCATED_AT"),
        "Key 不再位于 Forest",
    )

    # 走回 TownSquare 与 Bob 在 Tavern 不同位置，所以先到 Tavern
    ok, _ = run_action(world, "move", c="Alice", **{"from": "Forest", "to": "TownSquare"})
    all_ok &= ok
    ok, _ = run_action(world, "move", c="Alice", **{"from": "TownSquare", "to": "Tavern"})
    all_ok &= ok

    # 在 Tavern 与 Bob 对话（确认 KNOWS 双向）
    ok, _ = run_action(world, "talk", a="Alice", b="Bob", l="Tavern")
    all_ok &= ok
    all_ok &= _expect(
        world.has_relationship("Bob", "Person", "Alice", "Person", "KNOWS"),
        "Bob 也认识 Alice（KNOWS 双向）",
    )

    # 把 Apple 给 Bob
    ok, _ = run_action(world, "give", giver="Alice", i="Apple", receiver="Bob", l="Tavern")
    all_ok &= ok
    all_ok &= _expect(
        world.has_relationship("Bob", "Person", "Apple", "Item", "HAS_ITEM"),
        "Bob 现在持有 Apple",
    )
    all_ok &= _expect(
        not world.has_relationship("Alice", "Person", "Apple", "Item", "HAS_ITEM"),
        "Alice 不再持有 Apple",
    )

    # Alice 把 Key 放在 Tavern
    ok, _ = run_action(world, "drop", c="Alice", i="Key", l="Tavern")
    all_ok &= ok
    all_ok &= _expect(
        world.has_relationship("Key", "Item", "Tavern", "Location", "LOCATED_AT"),
        "Key 现在位于 Tavern",
    )

    return all_ok


def test_use_item(world: Neo4jWorld) -> bool:
    _hr("Use item: Merchant 用 Potion 自疗")
    all_ok = True

    # Merchant 当前 hp=85，喝下 value=60 的 Potion 应当变 145
    rows = world.run_query("MATCH (n:Person {name:'Merchant'}) RETURN n.hp AS hp")
    before = rows[0]["hp"]
    ok, _ = run_action(world, "use_item", c="Merchant", i="Potion")
    all_ok &= ok

    rows = world.run_query("MATCH (n:Person {name:'Merchant'}) RETURN n.hp AS hp")
    after = rows[0]["hp"]
    all_ok &= _expect(after == before + 60, f"Merchant.hp: {before} -> {after}（应 +60）")

    rows = world.run_query("MATCH (i:Item {name:'Potion'}) RETURN i")
    all_ok &= _expect(len(rows) == 0, "Potion 已被消耗（节点删除）")
    return all_ok


def test_combat(world: Neo4jWorld) -> bool:
    _hr("Combat: Guard 攻击 Elder（同在 Castle）")
    all_ok = True

    rows = world.run_query("MATCH (n:Person {name:'Elder'}) RETURN n.hp AS hp")
    before = rows[0]["hp"]
    ok, _ = run_action(world, "attack", attacker="Guard", target="Elder", l="Castle")
    all_ok &= ok

    rows = world.run_query("MATCH (n:Person {name:'Elder'}) RETURN n.hp AS hp")
    after = rows[0]["hp"]
    # Guard.power_level=15
    all_ok &= _expect(after == max(before - 15, 0), f"Elder.hp: {before} -> {after}（应 -15）")
    return all_ok


def test_negative_cases(world: Neo4jWorld) -> bool:
    _hr("Negative: precondition 应当拦截非法动作")
    all_ok = True

    # Bob 在 Tavern；他想直接到 Castle，但 Tavern 与 Castle 不相邻
    ok, reason = run_action(world, "move", c="Bob", **{"from": "Tavern", "to": "Castle"})
    all_ok &= _expect(not ok, f"非相邻移动被拒绝: {reason}")

    # Alice 在 Tavern，想攻击在 Castle 的 Guard
    ok, reason = run_action(world, "attack", attacker="Alice", target="Guard", l="Tavern")
    all_ok &= _expect(not ok, f"跨位置攻击被拒绝: {reason}")

    # Alice 没有持有 Sword（在 Guard 手里），想 drop 应被拒
    ok, reason = run_action(world, "drop", c="Alice", i="Sword", l="Tavern")
    all_ok &= _expect(not ok, f"丢弃未持有物品被拒绝: {reason}")

    # 自己攻击自己
    ok, reason = run_action(world, "attack", attacker="Bob", target="Bob", l="Tavern")
    all_ok &= _expect(not ok, f"攻击自己被拒绝: {reason}")

    return all_ok


def main() -> int:
    try:
        world = Neo4jWorld.get_instance()
    except Exception as e:
        print(f"无法连接 Neo4j: {e}")
        return 1

    try:
        results = []
        results.append(("introspection", test_introspection()))
        setup_world(world)
        results.append(("happy_path", test_happy_path(world)))
        results.append(("use_item", test_use_item(world)))
        results.append(("combat", test_combat(world)))
        results.append(("negative", test_negative_cases(world)))

        _hr("Summary")
        all_ok = True
        for name, ok in results:
            print(f"  [{'OK' if ok else 'FAIL'}] {name}")
            all_ok &= ok

        print("\n保留数据，可在 Neo4j Browser 中查看：")
        print("  MATCH (n)-[r]-(m) RETURN n, r, m")
        return 0 if all_ok else 1
    finally:
        world.close()


if __name__ == "__main__":
    sys.exit(main())
