"""
持续时钟 tick / 动作耗时 / 可行性 / 重规划 测试

运行（仓库根目录）：
    python AITown3/test/test_timed_tick.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AITOWN3 = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for sub in ("neo4j", "actions", "planner", "agent", "utility", "memory", "test"):
    p = os.path.join(_AITOWN3, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from neo4j_world import Neo4jWorld  # noqa: E402
from agent_profile import AgentProfile  # noqa: E402
from game_loop import GameLoop  # noqa: E402
from action_timing import get_action_duration  # noqa: E402
from goal_feasibility import (  # noqa: E402
    assess_goal,
    is_goal_satisfied,
    sanitize_goal_target,
)
from registry import get_registry  # noqa: E402
import test_build_neo4j as builder  # noqa: E402


def _expect(ok: bool, msg: str) -> bool:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {msg}")
    return ok


def _loc(world, name: str):
    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc",
        n=name,
    )
    return rows[0]["loc"] if rows else None


def test_durations() -> bool:
    print("\n=== action durations ===")
    ok = True
    ok &= _expect(get_action_duration("move") == 1.0, "move=1.0")
    ok &= _expect(get_action_duration("talk") == 4.0, "talk=4.0")
    ok &= _expect(get_action_duration("wait") == 1.0, "wait=1.0")
    ok &= _expect(get_action_duration("move", instant=True) == 0.0, "instant=0")
    ok &= _expect("wait" in get_registry().names(), "wait registered")
    return ok


def test_tick_deferred_apply(world: Neo4jWorld) -> bool:
    print("\n=== tick deferred apply ===")
    world.clear_all()
    builder.build_world(world)

    # 找一个有相邻地点的人
    persons = [p for p in (world.list_entities("Person") or []) if p]
    if not persons:
        return _expect(False, "no persons")
    name = persons[0]
    start = _loc(world, name)
    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location)-[:ADJACENT_TO]->(adj:Location)
        RETURN adj.name AS loc LIMIT 1
        """,
        n=name,
    )
    if not rows:
        return _expect(False, f"{name} has no adjacent")
    dest = rows[0]["loc"]

    loop = GameLoop(world, verbose=False)
    loop.force_mock = True
    agent = AgentProfile.from_world(world, name)
    loop.add_agent(agent, force_mock=True)
    state = loop.get_agent_state(name)
    assert state is not None
    state.current_plan = [("move", [name, start, dest])]
    state.plan_index = 0
    state.decision_cooldown_until = 9999  # 禁止自动决策

    loop.tick(0.5, auto_plan=False, instant=False)
    mid = _loc(world, name)
    ok = _expect(mid == start, f"after 0.5s still at {start} (got {mid})")
    ok &= _expect(state.pending_action is not None, "pending_action set")
    ok &= _expect(state.status == "acting", f"status=acting (got {state.status})")

    # 走路要多久看站的位置（人散开站，不都在正中间）：推进到这个动作完成之后
    remaining = state.pending_action.completes_at - loop.sim_time if state.pending_action else 0.0
    loop.tick(max(2.0, remaining + 0.1), auto_plan=False, instant=False)
    end = _loc(world, name)
    ok &= _expect(end == dest, f"once the walk is done, at {dest} (got {end})")
    ok &= _expect(state.pending_action is None, "pending cleared")
    return ok


def test_async_agents(world: Neo4jWorld) -> bool:
    print("\n=== async agents ===")
    world.clear_all()
    builder.build_world(world)
    persons = [p for p in (world.list_entities("Person") or []) if p][:2]
    if len(persons) < 2:
        return _expect(False, "need 2 persons")

    a, b = persons[0], persons[1]
    loop = GameLoop(world, verbose=False)
    loop.force_mock = True
    for n in (a, b):
        loop.add_agent(AgentProfile.from_world(world, n), force_mock=True)

    sa, sb = loop.get_agent_state(a), loop.get_agent_state(b)
    assert sa and sb
    sa.current_plan = [("wait", [a]), ("wait", [a])]
    sa.plan_index = 0
    sb.current_plan = [("wait", [b])]
    sb.plan_index = 0
    sa.decision_cooldown_until = 9999
    sb.decision_cooldown_until = 9999

    loop.tick(0.5, auto_plan=False)
    ok = _expect(sa.pending_action is not None, "A started wait")
    ok &= _expect(sb.pending_action is not None, "B started wait")

    loop.tick(1.0, auto_plan=False)
    # wait duration=1.0; started at 0, completes at 1.0; sim_time after first 0.5 + 1.0 = 1.5
    ok &= _expect(sb.pending_action is None, "B finished single wait")
    ok &= _expect(
        sa.pending_action is not None or (sa.plan_index >= 1),
        "A progressed (still acting or next step)",
    )
    return ok


def test_feasibility(world: Neo4jWorld) -> bool:
    print("\n=== goal feasibility ===")
    world.clear_all()
    builder.build_world(world)
    persons = world.list_entities("Person") or []
    name = persons[0]
    ok = True

    t, err = sanitize_goal_target(world, name, "Gain information", name)
    ok &= _expect(t is None and err is not None, "reject self talk target")

    loc = _loc(world, name)
    satisfied = is_goal_satisfied(world, name, "Go to location", loc)
    ok &= _expect(satisfied, "already at location is satisfied")

    assessed = assess_goal(
        world,
        name,
        {"goal": "Go to location", "target_entity": loc, "priority": 0.9, "confidence": 0.9},
    )
    ok &= _expect(assessed.ok and assessed.already_satisfied, "assess marks satisfied")

    bad = assess_goal(
        world,
        name,
        {"goal": "Collect item", "target_entity": "NoSuchItemXYZ", "priority": 0.5, "confidence": 0.5},
    )
    ok &= _expect(not bad.ok, "unknown item rejected")
    return ok


def test_instant_compat(world: Neo4jWorld) -> bool:
    print("\n=== run_turn instant compat ===")
    world.clear_all()
    builder.build_world(world)
    persons = world.list_entities("Person") or []
    name = persons[0]
    start = _loc(world, name)
    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location)-[:ADJACENT_TO]->(adj:Location)
        RETURN adj.name AS loc LIMIT 1
        """,
        n=name,
    )
    if not rows:
        return _expect(False, "no adjacent")
    dest = rows[0]["loc"]

    loop = GameLoop(world, verbose=False)
    loop.force_mock = True
    loop.add_agent(AgentProfile.from_world(world, name), force_mock=True)
    state = loop.get_agent_state(name)
    assert state
    state.current_plan = [("move", [name, start, dest])]
    state.plan_index = 0
    state.decision_cooldown_until = 9999

    loop.run_turn(auto_plan=False)
    end = _loc(world, name)
    return _expect(end == dest, f"instant turn moved to {dest} (got {end})")


def main() -> int:
    try:
        world = Neo4jWorld.get_instance()
    except Exception as e:
        print(f"Neo4j unavailable: {e}")
        return 1

    results = [
        test_durations(),
        test_tick_deferred_apply(world),
        test_async_agents(world),
        test_feasibility(world),
        test_instant_compat(world),
    ]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
