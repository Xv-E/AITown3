"""
WorldSimulator 测试 — sync_agents、auto_plan 双模式。
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AITOWN3 = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for sub in ("neo4j", "actions", "planner", "agent", "utility", "memory", "test", "web"):
    p = os.path.join(_AITOWN3, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from neo4j_world import Neo4jWorld  # noqa: E402
import test_build_neo4j as builder  # noqa: E402
from world_simulator import WorldSimulator  # noqa: E402


def _expect(ok: bool, msg: str) -> bool:
    print(f"  [{'OK' if ok else 'FAIL'}] {msg}")
    return ok


def main() -> int:
    ok_all = True

    try:
        world = Neo4jWorld.get_instance()
    except Exception as e:
        print(f"无法连接 Neo4j: {e}")
        return 1

    try:
        world.clear_all()
        builder.build_world(world)

        sim = WorldSimulator()
        sim.reset()

        sim.sync_agents(world, force_mock=True)
        ok_all &= _expect(
            sim._game_loop is not None and len(sim._game_loop.agents) >= 5,
            f"sync_agents 同步 {len(sim._game_loop.agents) if sim._game_loop else 0} 个角色",
        )

        result = sim.run_turn(world, auto_plan=False)
        skipped = sum(1 for a in result.get("agents", []) if a.get("skipped"))
        ok_all &= _expect(skipped >= 5, "auto_plan=False 无计划时全部跳过")

        sim.inject_plan(
            "Alice",
            {"goal": "Go to location", "target_entity": "Tavern", "reason": "test"},
            [("move", ["Alice", "TownSquare", "Tavern"])],
            engine="test",
        )
        step = sim.run_turn(world, characters=["Alice"], auto_plan=False)
        agents = step.get("agents") or []
        ok_all &= _expect(len(agents) == 1, "单角色回合")
        ok_all &= _expect(not agents[0].get("skipped"), "Alice 执行了一步")
        ok_all &= _expect(agents[0].get("success"), "Alice move 成功")

        alice_plan = sim.get_plan_dict(world, "Alice")
        ok_all &= _expect(
            alice_plan.get("status") in ("ready", "completed"),
            f"Alice 计划状态: {alice_plan.get('status')}",
        )

        auto = sim.run_turn(world, auto_plan=True, use_llm=False)
        ok_all &= _expect(auto.get("turn", 0) >= 3, "auto_plan=True 多角色自动回合")
        acted = [a for a in auto.get("agents", []) if not a.get("skipped")]
        ok_all &= _expect(len(acted) > 0, "自动回合有角色行动")

        sim.reset()
        ok_all &= _expect(sim.turn_count == 0, "reset 清空回合计数")
    finally:
        try:
            world.close()
        except Exception:
            pass

    print("\nALL PASS" if ok_all else "\nSOME CHECKS FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
