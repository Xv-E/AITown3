"""
单角色规划能力测试

流程：
  1. 构建单角色最小世界（build_solo_world）
  2. 对多个目标调用 plan_for_goal，打印计划
  3. 可选：执行计划并校验 Neo4j 状态

场景：
  - Go to location → Castle（2 步：经 Forest）
  - Go to location → Tavern（1 步，已访问）
  - Explore unknown location → Forest（1 步）
  - Collect item → Key（移动到 Forest + pick_up）

运行（仓库根目录）：
    python AITown3/test/test_solo_planner.py
    python AITown3/test/test_solo_planner.py --character Alice --execute
    python AITown3/test/test_solo_planner.py --engine bfs
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AITOWN3 = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for sub in ("neo4j", "actions", "planner", "agent", "test"):
    p = os.path.join(_AITOWN3, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from neo4j_world import Neo4jWorld  # noqa: E402
from goal_planner import plan_for_goal  # noqa: E402
from solver import execute_plan  # noqa: E402

import build_solo_world as solo  # noqa: E402

# (goal, target_entity, 描述, 执行后期望校验)
SCENARIOS: List[Tuple[str, str, str, Dict[str, Any]]] = [
    (
        "Go to location",
        "Tavern",
        "相邻 1 步到达",
        {"check": "at", "location": "Tavern"},
    ),
    (
        "Explore unknown location",
        "Forest",
        "探索未访问地点",
        {"check": "at", "location": "Forest"},
    ),
    (
        "Go to location",
        "Castle",
        "经 Forest 2 步到达",
        {"check": "at", "location": "Castle"},
    ),
    (
        "Collect item",
        "Key",
        "寻路到 Forest 并拾取 Key",
        {"check": "has_item", "item": "Key"},
    ),
]


def _hr(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def _expect(ok: bool, msg: str) -> bool:
    print(f"  [{'OK' if ok else 'FAIL'}] {msg}")
    return ok


def setup_world(world: Neo4jWorld, character: str) -> None:
    _hr("Setup: 单角色世界")
    world.clear_all()
    solo.build_solo_world(world, character=character)
    solo.verify_solo_world(world, character=character)


def _get_location(world: Neo4jWorld, person: str) -> str:
    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS l",
        n=person,
    )
    return rows[0]["l"] if rows else ""


def _has_item(world: Neo4jWorld, person: str, item: str) -> bool:
    return world.has_relationship(person, "Person", item, "Item", "HAS_ITEM")


def _verify_expectation(
    world: Neo4jWorld,
    character: str,
    expectation: Dict[str, Any],
) -> bool:
    check = expectation.get("check")
    if check == "at":
        loc = _get_location(world, character)
        return loc == expectation.get("location")
    if check == "has_item":
        return _has_item(world, character, expectation.get("item", ""))
    return True


def run_scenario(
    world: Neo4jWorld,
    character: str,
    goal: str,
    target: str,
    desc: str,
    expectation: Dict[str, Any],
    *,
    engines: List[str],
    execute: bool,
) -> bool:
    _hr(f"场景: {goal} → {target} ({desc})")
    print(f"  起点: {_get_location(world, character)}")

    goal_data = {
        "goal": goal,
        "target_entity": target,
        "reason": f"测试: {desc}",
        "priority": 0.9,
        "confidence": 0.9,
    }

    result = plan_for_goal(world, character, goal_data, engines=engines)
    if not result.success:
        return _expect(False, f"规划失败: {result.error} (engine={result.engine})")

    plan = result.plan
    _expect(True, f"规划成功 engine={result.engine}，共 {len(plan)} 步")
    for i, (action, args) in enumerate(plan, 1):
        print(f"    {i}. {action}({', '.join(args)})")

    if not execute:
        return True

    if not plan:
        return _expect(_verify_expectation(world, character, expectation), "目标已满足")

    ok, logs = execute_plan(world, plan)
    for line in logs:
        print(f"    {line}")
    if not ok:
        return _expect(False, "计划执行失败")

    passed = _verify_expectation(world, character, expectation)
    if expectation.get("check") == "at":
        msg = f"执行后位于 {expectation['location']}（实际 {_get_location(world, character)}）"
    elif expectation.get("check") == "has_item":
        msg = f"执行后持有 {expectation['item']}"
    else:
        msg = "执行后状态符合预期"
    return _expect(passed, msg)


def main() -> int:
    parser = argparse.ArgumentParser(description="单角色规划能力测试")
    parser.add_argument("--character", default=solo.DEFAULT_CHARACTER)
    parser.add_argument("--execute", action="store_true", help="规划后真实执行并校验")
    parser.add_argument(
        "--engine",
        choices=["auto", "unified_planning", "bfs"],
        default="auto",
    )
    args = parser.parse_args()

    engines = ["unified_planning", "bfs"] if args.engine == "auto" else [args.engine]

    try:
        world = Neo4jWorld.get_instance()
    except Exception as e:
        print(f"无法连接 Neo4j: {e}")
        return 1

    all_ok = True
    try:
        for goal, target, desc, expectation in SCENARIOS:
            setup_world(world, args.character)
            if not run_scenario(
                world,
                args.character,
                goal,
                target,
                desc,
                expectation,
                engines=engines,
                execute=args.execute,
            ):
                all_ok = False
                if args.execute:
                    print("\n执行模式：场景失败，停止后续场景。")
                    break

        _hr("Summary")
        print("All passed." if all_ok else "Some scenarios failed.")
        return 0 if all_ok else 1
    finally:
        world.close()


if __name__ == "__main__":
    sys.exit(main())
