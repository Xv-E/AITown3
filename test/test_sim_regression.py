"""多拍回归：营地模板世界（4 个 NPC）规则模式跑 N 拍，决策阶段并行。

只检查不变量（不报错、人人都在行动、并行路径真的被用到），不锁死具体计划，
规则 / 规划改动后仍应通过。需要 Neo4j；会清空并重建当前库。
"""

from __future__ import annotations

import collections

import pytest

TICKS = 40
DT = 2.0


@pytest.fixture
def camp(world):
    from director import build_from_blueprint, load_world

    build_from_blueprint(world, load_world("town_daily_life"), clear=True)
    return world


def _run(world, *, plan_workers: int):
    from agent_profile import AgentProfile
    from game_loop import GameLoop

    loop = GameLoop(world, verbose=False, plan_workers=plan_workers)
    loop.force_mock = True
    for name in world.list_entities("Person") or []:
        loop.add_agent(AgentProfile.from_world(world, name), force_mock=True)

    batches = []
    run_decisions = loop._run_decisions

    def spy(deciders, **kw):
        batches.append(len(deciders))
        return run_decisions(deciders, **kw)

    loop._run_decisions = spy
    done = collections.Counter()
    errors = []
    try:
        for i in range(TICKS):
            res = loop.tick(DT, auto_plan=True, max_decisions_per_tick=4)
            for a in res["agents"]:
                if a.get("error"):
                    errors.append((i, a["agent"], a["error"]))
                act = a.get("action") or {}
                if a.get("phase") not in ("completing", "executing") or not act:
                    continue
                if act.get("success"):
                    done[a["agent"]] += 1
                elif str(act.get("message") or "").startswith("Execution error"):
                    # 代码异常（不是前置条件不满足之类的正常失败）
                    errors.append((i, a["agent"], act.get("action"), act.get("message")))
    finally:
        loop.close()
    return loop, done, errors, batches


@pytest.mark.parametrize("plan_workers", [4, 1])
def test_camp_runs_clean(camp, plan_workers):
    loop, done, errors, batches = _run(camp, plan_workers=plan_workers)

    assert not errors, errors
    assert loop.sim_time == pytest.approx(TICKS * DT)
    npcs = {"Rowan", "Sage", "Ash", "Reed"}
    assert npcs <= set(done), f"every NPC should complete an action: {dict(done)}"
    assert "Observer" not in done, "the player must not auto-act"
    assert sum(done.values()) >= 10, dict(done)
    if plan_workers > 1:
        assert max(batches) > 1, "parallel decision path was never exercised"
