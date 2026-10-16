"""计划冲突：别人拿走了我计划要用的东西 → 我的计划被打断、记矛盾、双方收到触发器。"""

from __future__ import annotations

import pytest

ITEM = "Berry1"
GROVE = "BerryGrove"


def _place(world, person: str, location: str) -> None:
    world.run_query(
        """
        MATCH (p:Person {name: $p})
        OPTIONAL MATCH (p)-[r:LOCATED_AT]->()
        DELETE r
        WITH DISTINCT p
        MATCH (l:Location {name: $l})
        MERGE (p)-[:LOCATED_AT]->(l)
        """,
        p=person, l=location,
    )


@pytest.fixture
def grove(world):
    from agent_profile import AgentProfile
    from director import build_from_blueprint, load_world
    from game_loop import GameLoop

    build_from_blueprint(world, load_world("town_daily_life"), clear=True)
    for who in ("Sage", "Reed"):
        _place(world, who, GROVE)
    loop = GameLoop(world, verbose=False)
    loop.force_mock = True
    for who in ("Sage", "Reed"):
        loop.add_agent(AgentProfile.from_world(world, who), force_mock=True)
    yield loop
    loop.close()


def _take(loop, who: str) -> None:
    state = loop.get_agent_state(who)
    state.current_plan = [("pick_up", [who, ITEM, GROVE])]
    state.plan_index = 0
    assert loop._start_next_step(state) is not None
    loop._complete_pending(state)
    assert state.last_action_result.success, state.last_action_result.message


def _tension(world, a: str, b: str) -> int:
    rows = world.run_query(
        "MATCH (:Person {name: $a})-[r:TENSION]->(:Person {name: $b}) RETURN r.level AS level",
        a=a, b=b,
    )
    return int(rows[0]["level"]) if rows else 0


def test_taking_an_item_interrupts_whoever_planned_to_use_it(grove):
    reed = grove.get_agent_state("Reed")
    reed.current_plan = [("wait", ["Reed"]), ("pick_up", ["Reed", ITEM, GROVE])]
    reed.plan_index = 0
    reed.status = "ready"

    _take(grove, "Sage")
    grove._flush_conflicts()

    assert reed.status == "failed"
    assert "Sage took Berry1" in (reed.replan_reason or "")
    assert any(t.kind == "conflict" and t.priority >= 0.8 for t in reed.pending_triggers)
    sage = grove.get_agent_state("Sage")
    assert any(t.kind == "rival" for t in sage.pending_triggers)
    assert _tension(grove.world, "Reed", "Sage") == 1
    assert grove.conflicts[-1]["culprit"] == "Sage" and grove.conflicts[-1]["loser"] == "Reed"


def test_failed_action_blames_current_holder(grove):
    reed = grove.get_agent_state("Reed")
    reed.current_plan = [("pick_up", ["Reed", ITEM, GROVE])]
    reed.plan_index = 0
    assert grove._start_next_step(reed) is not None  # Reed is already reaching for it...

    _take(grove, "Sage")  # ...but Sage finishes first
    grove._flush_conflicts()
    assert reed.pending_action is not None, "in-progress actions are not cancelled"

    grove._complete_pending(reed)
    assert not reed.last_action_result.success
    grove._flush_conflicts()

    assert [c["loser"] for c in grove.conflicts] == ["Reed"]
    assert _tension(grove.world, "Reed", "Sage") == 1


def test_same_conflict_is_not_counted_twice(grove):
    reed = grove.get_agent_state("Reed")
    reed.current_plan = [("pick_up", ["Reed", ITEM, GROVE])]
    reed.plan_index = 0
    _take(grove, "Sage")
    grove._flush_conflicts()
    grove._queue_failure_conflict(reed, "pick_up", ["Reed", ITEM, GROVE], "not here")
    grove._flush_conflicts()
    assert len(grove.conflicts) == 1
    assert _tension(grove.world, "Reed", "Sage") == 1


def test_grudge_shows_up_in_the_npc_prompt(grove):
    from prompt_builder import PromptBuilder

    reed = grove.get_agent_state("Reed")
    reed.current_plan = [("pick_up", ["Reed", ITEM, GROVE])]
    _take(grove, "Sage")
    grove._flush_conflicts()

    text = PromptBuilder(grove.world)._get_tensions_text(reed.agent)
    assert "Sage" in text and "Berry1" in text


def test_planning_mistakes_and_stale_plans_are_not_conflicts(grove):
    reed = grove.get_agent_state("Reed")
    _take(grove, "Sage")
    grove._flush_conflicts()

    # Reed's plan assumed he already held it: his own mistake.
    grove._queue_failure_conflict(reed, "give", ["Reed", ITEM, "Ash"], f"Reed is not holding {ITEM}")
    # Reed decided after Sage had already taken it: he planned on stale information.
    reed.last_decision_at = grove.sim_time + 1
    grove._queue_failure_conflict(reed, "pick_up", ["Reed", ITEM, GROVE], "not here")
    grove._flush_conflicts()

    assert grove.conflicts == []
    assert _tension(grove.world, "Reed", "Sage") == 0


def test_using_a_fixture_is_not_taking_it(grove):
    """砍过的树还在原地：别人在它上面失败不算被抢。"""
    reed = grove.get_agent_state("Reed")
    grove._touched["OakTree"] = ("Sage", "interact", grove.sim_time)
    grove._queue_failure_conflict(reed, "interact", ["Reed", "OakTree", "chop"], "some requirement failed")
    grove._flush_conflicts()
    assert grove.conflicts == []
