"""作者写好的物品动作：可预期、可校验、玩家和规划器看到的是同一套。动作发明默认关闭。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

WORLDS = sorted((Path(__file__).resolve().parents[1] / "worlds").glob("*.json"))


@pytest.mark.parametrize("path", WORLDS, ids=lambda p: p.stem)
def test_every_authored_action_in_the_worlds_is_valid(path):
    from content import get_content
    from ephemeral_action_dsl import validate_action_spec

    world = json.loads(path.read_text(encoding="utf-8"))
    names = {x["name"] for key in ("locations", "persons", "items") for x in world.get(key) or []}
    archetypes = set(get_content().archetypes)
    for item in world.get("items") or []:
        for spec in (item.get("script") or {}).get("actions") or []:
            action, err = validate_action_spec(
                spec, host_item=item["name"], entity_names=names, archetypes=archetypes
            )
            assert action is not None, f"{path.stem}/{item['name']}: {err}"


@pytest.fixture
def camp(world, monkeypatch):
    from director import build_from_blueprint, load_world

    monkeypatch.delenv("AITOWN3_INVENT_ACTIONS", raising=False)
    build_from_blueprint(world, load_world("town_daily_life"), clear=True)
    return world


def _dial(world):
    rows = world.run_query("MATCH (i:Item {name: 'StoneDial'}) RETURN i.facing AS f, i.steps AS s, i.solved AS ok")
    return rows[0]["f"], rows[0]["s"], bool(rows[0]["ok"])


def test_player_sees_only_what_they_can_do_now(camp):
    from character_actions import get_character_actions

    here = get_character_actions(camp, "Observer")["context"]["interactables_here"]
    dial = next(i for i in here if i["name"] == "StoneDial")
    doable = sorted(a["label"] for a in dial["actions"] if a["available"])
    assert doable == ["Study the carvings", "Turn clockwise", "Turn counterclockwise"]


def test_the_dial_puzzle_can_be_solved_by_hand(camp):
    from registry import get_registry

    reg = get_registry()

    def turn(option):
        ok, msg = reg.execute(camp, "interact", c="Observer", target="StoneDial", option=option)
        assert ok, msg

    turn("turn_cw_from_north")
    turn("turn_ccw_from_east")
    assert _dial(camp) == ("north", 2, False)
    turn("turn_cw_from_north")
    assert _dial(camp) == ("east", 3, True)
    ok, reason = reg.get("interact").check(camp, c="Observer", target="StoneDial", option="turn_cw_from_east")
    assert not ok and "locked in place" in reason


def test_an_npc_can_plan_how_to_solve_the_dial(camp):
    from goal_planner import plan_for_goal

    planned = plan_for_goal(camp, "Ash", {
        "goal": "Achieve",
        "conditions": [{"op": "prop_eq", "entity": "StoneDial", "prop": "solved", "value": True}],
    })
    assert planned.success, planned.error
    turns = [args[2] for name, args in planned.plan if name == "interact"]
    assert len(turns) >= 3 and all(t.startswith("turn_") for t in turns)


def test_no_actions_are_invented_unless_enabled(camp, monkeypatch):
    import item_interact
    from item_interact import ensure_ephemeral_actions, resolve_interaction

    def boom(*a, **k):
        raise AssertionError("must not call the LLM to invent actions")

    monkeypatch.setattr(item_interact, "_llm_action_code", boom)
    camp.run_query(
        "MATCH (i:Item {name: 'Campfire'}) SET i.script = $s",
        s=json.dumps({"interactive": True, "behavior": "A fire you could poke."}),
    )
    assert ensure_ephemeral_actions(camp, "Ash", "Campfire") == []
    out = resolve_interaction(camp, "Ash", "Campfire", "auto")
    assert not out["ok"] and "nothing to do" in out["error"]
