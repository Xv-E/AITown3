"""
LLM Agent 决策循环端到端测试

流程：
  1. 构建 Neo4j 世界
  2. 创建 Agent + MultiStepDecider + GameLoop
  3. 运行 N 回合（THINK → PLAN → EXECUTE → EVALUATE）
  4. 校验 Neo4j 状态变化或 Chroma 记忆写入

运行（仓库根目录）：
    python AITown3/test/test_agent_loop.py
    python AITown3/test/test_agent_loop.py --turns 5 --npc Bob
    python AITown3/test/test_agent_loop.py --no-llm
    python AITown3/test/test_agent_loop.py --verbose
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from typing import List

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AITOWN3 = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for sub in ("neo4j", "actions", "planner", "agent", "utility", "memory", "test"):
    p = os.path.join(_AITOWN3, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from neo4j_world import Neo4jWorld  # noqa: E402
from agent_profile import AgentProfile  # noqa: E402
from game_loop import GameLoop  # noqa: E402
from multi_step_decider import MultiStepDecider  # noqa: E402
from chroma_memory import ChromaMemory  # noqa: E402

import test_build_neo4j as builder  # noqa: E402


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


def run_agent_loop(
    *,
    npc_name: str = "Alice",
    turns: int = 3,
    no_llm: bool = False,
    verbose: bool = False,
    chroma_path: str | None = None,
) -> int:
    ok_all = True

    try:
        world = Neo4jWorld.get_instance()
    except Exception as e:
        print(f"❌ Neo4j 连接失败: {e}")
        return 1

    setup_world(world)
    start_loc = _get_location(world, npc_name)

    if chroma_path is None:
        chroma_path = tempfile.mkdtemp(prefix="aitown3_agent_test_")
    ChromaMemory._instance = None  # type: ignore[attr-defined]
    memory = ChromaMemory.get_instance(persist_path=chroma_path)
    memory.clear(npc_name)

    agent = AgentProfile.from_world(world, npc_name)
    agent.parent_goal = "explore the town and meet people"
    decider = MultiStepDecider(world, memory=memory, force_mock=no_llm)

    game = GameLoop(
        world,
        memory=memory,
        max_turns=turns,
        verbose=verbose or True,
        plan_engines=["unified_planning", "bfs"],
    )
    game.add_agent(agent, decider=decider, force_mock=no_llm)

    _hr(f"Run: {turns} turns for {npc_name} (mock={no_llm})")
    result = game.run(max_turns=turns)

    end_loc = _get_location(world, npc_name)
    mem_count = memory.count(npc_name)

    ok_all &= _expect(result["total_turns"] == turns, f"完成 {turns} 回合")
    ok_all &= _expect(
        end_loc != start_loc or mem_count > 0,
        f"世界或记忆有变化 (loc {start_loc}->{end_loc}, memories={mem_count})",
    )

    _hr("Turn summary")
    for t in result["turns"]:
        for a in t.get("agents", []):
            name = a.get("agent", "?")
            goal = a.get("goal") or {}
            action = a.get("action") or {}
            print(
                f"  Turn {t['turn']} [{name}] "
                f"goal={goal.get('goal', '-')} "
                f"action={action.get('type', '-')} "
                f"ok={action.get('success', '-')}"
            )

    memory.close()
    try:
        world.close()
    except Exception:
        pass

    _hr("Result")
    print("  ALL PASS" if ok_all else "  SOME CHECKS FAILED")
    return 0 if ok_all else 1


def _get_location(world: Neo4jWorld, person: str) -> str:
    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS l",
        n=person,
    )
    return rows[0]["l"] if rows else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="AITown3 Agent loop test")
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--npc", default="Alice")
    parser.add_argument("--no-llm", action="store_true", help="强制 mock 决策")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--chroma-path", default=None)
    args = parser.parse_args()
    return run_agent_loop(
        npc_name=args.npc,
        turns=args.turns,
        no_llm=args.no_llm,
        verbose=args.verbose,
        chroma_path=args.chroma_path,
    )


if __name__ == "__main__":
    sys.exit(main())
