"""Storyteller：LLM 输出的校验（纯函数）+ 模板模式下一拍剧情真的作用到世界。"""

from __future__ import annotations

import json

import pytest

from storyteller import MAX_BEATS, Storyteller, parse_plan

DIGEST = {
    "people": [
        {"name": "Reed", "player": False},
        {"name": "Sage", "player": False},
        {"name": "Observer", "player": True},
    ],
    "items": [{"name": "Campfire", "props": {"fuel": 60}}],
}


def _plan(*beats, arc="Trouble brews."):
    return json.dumps({"arc": arc, "beats": list(beats)})


def test_valid_beats_pass_through():
    raw = "Sure! " + _plan(
        {"op": "announce", "text": "A storm rolls in."},
        {"op": "whisper", "to": "Reed", "text": "Sage hid berries.", "urgent": True},
    )
    plan = parse_plan(raw, DIGEST)
    assert plan["arc"] == "Trouble brews."
    assert [b["op"] for b in plan["beats"]] == ["announce", "whisper"]
    assert plan["beats"][1]["urgent"] is True


def test_rejects_unknown_people_players_and_ops():
    raw = _plan(
        {"op": "whisper", "to": "Nobody", "text": "hi"},
        {"op": "whisper", "to": "Observer", "text": "the player is off limits"},
        {"op": "spawn_dragon", "text": "!"},
    )
    assert parse_plan(raw, DIGEST)["beats"] == []


def test_adjust_is_clamped_and_must_target_known_numbers():
    raw = _plan(
        {"op": "adjust", "item": "Campfire", "prop": "fuel", "delta": -500},
        {"op": "adjust", "item": "Campfire", "prop": "colour", "delta": 3},
    )
    beats = parse_plan(raw, DIGEST)["beats"]
    assert beats == [{"op": "adjust", "item": "Campfire", "prop": "fuel", "delta": -30, "why": ""}]


def test_at_most_max_beats_and_garbage_is_none():
    many = [{"op": "announce", "text": f"event {i}"} for i in range(5)]
    assert len(parse_plan(_plan(*many), DIGEST)["beats"]) == MAX_BEATS
    assert parse_plan("no json here", DIGEST) is None


@pytest.fixture
def camp_loop(world):
    from agent_profile import AgentProfile
    from director import build_from_blueprint, load_world
    from game_loop import GameLoop

    build_from_blueprint(world, load_world("town_daily_life"), clear=True)
    loop = GameLoop(world, verbose=False)
    loop.force_mock = True
    for name in world.list_entities("Person") or []:
        loop.add_agent(AgentProfile.from_world(world, name), force_mock=True)
    loop.storyteller = Storyteller(interval=4.0)
    yield loop
    loop.close()


def test_template_storyteller_moves_the_world(camp_loop):
    from action import get_property

    fuel_before = get_property(camp_loop.world, "Campfire", "Item", "fuel")
    for _ in range(3):
        camp_loop.tick(2.0, auto_plan=False)

    beats = camp_loop.storyteller.beats
    assert beats, "a beat should fire once the interval has passed"
    assert camp_loop.storyteller.arc
    ops = {b["op"] for b in beats}
    if "adjust" in ops:
        assert get_property(camp_loop.world, "Campfire", "Item", "fuel") < fuel_before
    if "announce" in ops:
        assert any("wind" in c["text"] for c in camp_loop.chronicle)


def test_fresh_conflict_becomes_the_next_story_beat(camp_loop):
    reed = camp_loop.get_agent_state("Reed")
    camp_loop.tick(2.0, auto_plan=False)
    camp_loop._record_conflict(reed, "Sage", "Berry1", "test")
    camp_loop.storyteller.mark_started(camp_loop.sim_time - 10)
    camp_loop.tick(2.0, auto_plan=False)

    beat = camp_loop.storyteller.beats[-1]
    assert beat["op"] == "whisper" and beat["to"] == "Reed" and "Sage" in beat["text"]
    assert any(h["kind"] == "story" for h in reed.history)


def test_does_not_repeat_last_rounds_whisper_or_adjust():
    digest = dict(DIGEST, recent_beats=[
        {"t": 10.0, "op": "whisper", "to": "Sage", "text": "old"},
        {"t": 20.0, "op": "whisper", "to": "Reed", "text": "x"},
        {"t": 20.0, "op": "adjust", "item": "Campfire", "prop": "fuel", "delta": -5},
    ])
    raw = _plan(
        {"op": "whisper", "to": "Reed", "text": "again?"},
        {"op": "adjust", "item": "Campfire", "prop": "fuel", "delta": -5},
    )
    assert parse_plan(raw, digest)["beats"] == []
    raw = _plan({"op": "whisper", "to": "Sage", "text": "your turn"})
    assert parse_plan(raw, digest)["beats"][0]["to"] == "Sage"


def test_the_director_tab_says_what_the_storyteller_did():
    from unittest.mock import MagicMock

    from director_view import beat_line, build_director_view, draft_summary
    from game_loop import GameLoop
    from storyteller import Storyteller

    loop = GameLoop(MagicMock(), verbose=False)
    loop.storyteller = Storyteller(interval=60.0, use_llm=False)
    loop.storyteller.arc = "Wood is running out before the cold night."
    loop.storyteller.beats = [
        {"clock": "08:00", "op": "whisper", "to": "Reed", "text": "Ash took your wood.", "urgent": True},
        {"clock": "09:00", "op": "adjust", "item": "Campfire", "prop": "fuel", "from": 5, "to": 2, "why": "wind"},
    ]
    loop.sim_time = 30.0
    view = build_director_view(loop, None)
    assert view["arc"] == "Wood is running out before the cold night." and view["mode"] == "template"
    assert [b["line"] for b in view["beats"]] == [
        "Changed Campfire's fuel 5 -> 2 - wind",
        "Whispered to Reed (urgent - interrupts their plan): Ash took your wood.",
    ]
    assert view["next_in_hours"] is not None and view["next_in_hours"] > 0
    assert beat_line({"op": "announce", "text": "A trader arrives."}) == "Announced to everyone: A trader arrives."
    summary = draft_summary({"ok": True, "patch": {"title": "Lost Seal", "stages": [
        {"title": "Ask around", "objective": "Talk to Sage", "giver": "Reed"}]}})
    assert summary["lines"] == ["Lost Seal", "1. Ask around (from Reed): Talk to Sage"]
