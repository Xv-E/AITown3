"""营地世界：规划并执行临时 interact 动作（砍橡树），走完整的 LLM 动作执行 + trace 路径（mock）。"""

from __future__ import annotations

import pytest


@pytest.fixture
def camp(world, monkeypatch):
    from director import build_from_blueprint, load_world
    import item_interact

    monkeypatch.setattr(item_interact, "FORCE_MOCK", True)
    build_from_blueprint(world, load_world("town_daily_life"), clear=True)
    return world


def test_rowan_plans_and_chops_oak(camp):
    from goal_planner import plan_for_goal
    from registry import get_registry

    reg = get_registry()
    planned = plan_for_goal(
        camp, "Rowan", {"goal": "Interact with", "target_entity": "OakTree", "interact_action": "chop"}
    )
    assert planned.success, planned.error
    assert planned.plan and planned.plan[-1][0] == "interact", planned.plan

    for name, args in planned.plan:
        action = reg.get(name)
        kwargs = {p.name: v for p, v in zip(action.parameters, args)}
        ok, msg = reg.execute(camp, name, **kwargs)
        assert ok, f"{name}{args}: {msg}"


def test_planner_chains_an_invented_action_to_feed_the_fire(camp):
    """没有现成木头：规划器自己用临时动作 chop 造出木头，再拿去添柴；GameLoop 执行到底。"""
    from action import get_property
    from agent_profile import AgentProfile
    from game_loop import GameLoop
    from goal_planner import plan_for_goal
    from item_interact import ensure_ephemeral_actions

    ensure_ephemeral_actions(camp, "Rowan", "OakTree", force_mock=True)
    camp.run_query("MATCH (i:Item) WHERE i.item_kind = 'wood' DETACH DELETE i")
    fuel = get_property(camp, "Campfire", "Item", "fuel")

    planned = plan_for_goal(camp, "Rowan", {
        "goal": "Achieve",
        "conditions": [{"op": "prop_gte", "entity": "Campfire", "prop": "fuel", "value": fuel + 10}],
    })
    assert planned.success, planned.error
    assert planned.engine.startswith("compiled:")
    assert ("interact", ["Rowan", "OakTree", "chop"]) in planned.plan

    loop = GameLoop(camp, verbose=False)
    loop.force_mock = True
    loop.add_agent(AgentProfile.from_world(camp, "Rowan"), force_mock=True)
    state = loop.get_agent_state("Rowan")
    state.current_plan, state.plan_index = list(planned.plan), 0
    for _ in planned.plan:
        loop.run_turn(auto_plan=False)
        assert state.last_action_result.success, state.last_action_result.message
    loop.close()
    assert get_property(camp, "Campfire", "Item", "fuel") >= fuel + 10


def _strip_oak(world) -> None:
    world.run_query("MATCH (i:Item {name: 'OakTree'}) SET i.logs = 0")


def test_object_action_requirements_are_checked_before_running(camp):
    from registry import get_registry

    _strip_oak(camp)
    ok, reason = get_registry().get("interact").check(camp, c="Rowan", target="OakTree", option="chop")
    assert not ok and reason.startswith("OakTree has no good branches left")


def test_rule_planner_rejects_an_action_that_cannot_run(camp):
    """条件不满足的动作在规划时就被否决，而不是被采纳后每拍预检失败、反复重规划。"""
    from goal_planner import plan_for_goal

    _strip_oak(camp)
    planned = plan_for_goal(
        camp, "Reed", {"goal": "Interact with", "target_entity": "OakTree", "interact_action": "chop"}
    )
    assert not planned.success
    assert "no good branches" in planned.error
