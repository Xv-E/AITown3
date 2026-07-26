"""
PDDL 寻路测试脚本

流程：
  1. 清空 + 构建世界
  2. 选若干个 (角色, 起点, 终点) 场景，逐个：
       - 生成 domain + problem PDDL
       - 调 solver 求解
       - 打印 plan
       - 通过 ActionRegistry 把 plan 真实写回 Neo4j
       - 校验 Neo4j 中角色已位于目标位置

世界邻接（来自 test_build_neo4j）：
    TownSquare - Tavern, Forest, Castle, Market
    Forest - Castle
    Tavern - Market

预期最短路径：
    Alice    : TownSquare -> Castle      (1 步)
    Bob      : Tavern     -> Forest      (2 步, 经 TownSquare)
    Merchant : Market     -> Castle      (2 步, 经 TownSquare)

运行（仓库根目录）：
    python AITown3/test/test_planner_pathfinding.py
    python AITown3/test/test_planner_pathfinding.py --print-pddl   # 打印生成的 PDDL
    python AITown3/test/test_planner_pathfinding.py --engine bfs   # 强制使用 BFS 兜底
"""

import argparse
import os
import sys
from typing import List, Tuple

# 把所有需要的子目录加进 sys.path（精确到子目录，避开 PyPI neo4j 包冲突）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AITOWN3 = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for sub in ("neo4j", "actions", "planner", "test"):
    p = os.path.join(_AITOWN3, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from neo4j_world import Neo4jWorld  # noqa: E402
from pddl import build_pathfinding_pddl  # noqa: E402
from solver import solve_pddl, execute_plan  # noqa: E402

import test_build_neo4j as builder  # noqa: E402


# (角色, 起点, 终点, 期望步数下限)
SCENARIOS: List[Tuple[str, str, str, int]] = [
    ("Alice",    "TownSquare", "Castle", 1),
    ("Bob",      "Tavern",     "Forest", 2),
    ("Merchant", "Market",     "Castle", 2),
]


def _hr(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def _expect(ok: bool, msg: str) -> bool:
    flag = "OK  " if ok else "FAIL"
    print(f"  [{flag}] {msg}")
    return ok


def setup_world(world: Neo4jWorld) -> None:
    _hr("Setup: clear + build world")
    world.clear_all()
    builder.build_world(world)


def get_current_location(world: Neo4jWorld, person: str) -> str:
    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS l",
        n=person,
    )
    return rows[0]["l"] if rows else ""


def run_scenario(
    world: Neo4jWorld,
    character: str,
    start: str,
    goal: str,
    expected_min_steps: int,
    print_pddl: bool,
    engines: List[str],
) -> bool:
    _hr(f"Scenario: {character} : {start} -> {goal}")
    all_ok = True

    cur = get_current_location(world, character)
    all_ok &= _expect(cur == start, f"起点正确（当前 {cur}, 期望 {start}）")
    if cur != start:
        return False

    domain_str, problem_str = build_pathfinding_pddl(world, character, goal)
    if print_pddl:
        print("\n--- DOMAIN ---")
        print(domain_str)
        print("--- PROBLEM ---")
        print(problem_str)

    result = solve_pddl(domain_str, problem_str, engines=engines)
    print(f"\n[engine] {result['engine']}")
    if not result["success"]:
        print(f"[error]  {result['error']}")
        return False

    print("[plan]")
    for i, (name, args) in enumerate(result["plan"], 1):
        print(f"  {i}. {name}({', '.join(args)})")

    all_ok &= _expect(
        len(result["plan"]) >= expected_min_steps,
        f"步数 {len(result['plan'])} >= 期望最少 {expected_min_steps}",
    )

    print("\n[execute]")
    ok_exec, logs = execute_plan(world, result["plan"])
    for line in logs:
        print("  " + line)
    all_ok &= _expect(ok_exec, "全部步骤成功执行")

    final = get_current_location(world, character)
    all_ok &= _expect(final == goal, f"最终位置={final}，目标={goal}")
    return all_ok


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AITown3 PDDL 寻路测试")
    parser.add_argument("--print-pddl", action="store_true",
                        help="打印生成的 domain / problem PDDL")
    parser.add_argument(
        "--engine",
        choices=["auto", "unified_planning", "bfs"],
        default="auto",
        help="求解引擎；auto 表示先试 unified-planning 再回退 BFS",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    engines = ["unified_planning", "bfs"] if args.engine == "auto" else [args.engine]

    try:
        world = Neo4jWorld.get_instance()
    except Exception as e:
        print(f"无法连接 Neo4j: {e}")
        return 1

    try:
        setup_world(world)

        all_ok = True
        for character, start, goal, exp_min in SCENARIOS:
            ok = run_scenario(world, character, start, goal, exp_min,
                              print_pddl=args.print_pddl, engines=engines)
            all_ok &= ok
            if not ok:
                print(f"\n场景 {character}: {start}->{goal} 失败，停止后续场景。")
                break

        _hr("Summary")
        print("All passed." if all_ok else "Some scenarios failed.")
        print("\n保留 Neo4j 数据，可在 Browser 中查看：")
        print("  MATCH (n)-[r]-(m) RETURN n, r, m")
        return 0 if all_ok else 1
    finally:
        world.close()


if __name__ == "__main__":
    sys.exit(main())
