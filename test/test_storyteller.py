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


# ---------------------------------------------------------------------------
# 剧情线索 + 靠对话推进（talk / ask / 心事 / 差事）
# ---------------------------------------------------------------------------

STREET = {
    "people": [
        {"name": "BigMa", "player": False},
        {"name": "Bones", "player": False},
        {"name": "Hawker", "player": False, "routine": True},
        {"name": "AhLok", "player": True},
    ],
    "items": [],
    "player": "AhLok",
    "favor_items": ["cash", "beer"],
    "errands": [],
    "sim_time": 100.0,
}


def test_talk_ask_and_threads_are_validated():
    raw = json.dumps({
        "arc": "The Dragons squeeze the market.",
        "threads": [{"id": "Big Ma's debt!", "title": "Big Ma's debt", "people": ["BigMa", "Nobody"],
                     "stakes": "her cart", "note": "She is two days behind."}],
        "beats": [
            {"op": "talk", "who": "Bones", "to": "BigMa", "topic": "the money", "thread": "BigMasDebt"},
            {"op": "ask", "who": "BigMa", "item": "CASH", "why": "to pay the Dragons", "reward": "free fish balls"},
            {"op": "ask", "who": "Bones", "item": "beer", "why": "thirsty"},          # 一轮只许一个求助
        ],
    })
    plan = parse_plan(raw, STREET)
    assert [b["op"] for b in plan["beats"]] == ["talk", "ask"]
    assert plan["beats"][1]["item"] == "cash"                       # 规范成真实的物品种类
    assert plan["threads"][0]["id"] == "BigMaSDebt" and plan["threads"][0]["people"] == ["BigMa"]


def test_cannot_send_the_player_background_people_or_someone_already_on_the_way():
    digest = dict(STREET, errands=[{"who": "Bones", "to": "AhLok", "topic": "x"}])
    raw = json.dumps({"beats": [
        {"op": "talk", "who": "AhLok", "to": "BigMa", "topic": "player is not a puppet"},
        {"op": "talk", "who": "Hawker", "to": "BigMa", "topic": "background NPC"},
        {"op": "talk", "who": "Bones", "to": "BigMa", "topic": "already busy"},
        {"op": "ask", "who": "BigMa", "item": "dragon egg", "why": "not a real item"},
    ], "arc": "x"})
    assert parse_plan(raw, digest)["beats"] == []


def test_story_context_only_asks_the_person_the_errand_is_for():
    from storyteller import format_story_context

    errand = {"to": "AhLok", "topic": "ask AhLok for cash: the Dragons", "ask": {"item": "cash", "why": "the Dragons", "reward": "fish balls"}}
    text, item = format_story_context("The Dragons want their money.", errand, "AhLok")
    assert item == "cash" and "ask AhLok for cash" in text and "fish balls" in text
    text, item = format_story_context("The Dragons want their money.", errand, "Bones")
    assert item is None and text == "On your mind lately: The Dragons want their money."


def test_template_turns_debts_and_faction_news_into_conversations():
    owed = dict(STREET, owed=[{"owner": "BigMa", "business": "MaCart", "faction": "SilverDragons", "dues": 6.0, "fee": 2.0}])
    plan = Storyteller(interval=50.0, use_llm=False).compose_template(owed)
    beat = plan["beats"][0]
    assert beat["op"] == "ask" and beat["who"] == "BigMa" and beat["item"] == "cash"
    assert plan["threads"][0]["id"] == beat["thread"]

    news = dict(STREET, faction_news=[{"t": 90.0, "day": 3, "text": "SilverDragons smashed up BigMa's cart."}])
    plan = Storyteller(interval=50.0, use_llm=False).compose_template(news)
    beat = plan["beats"][0]
    assert beat["op"] == "talk" and beat["who"] == "BigMa" and beat["to"] == "AhLok"


def test_threads_are_capped_and_keep_their_notes():
    from storyteller import MAX_OPEN_THREADS, THREAD_NOTES

    st = Storyteller(interval=50.0, use_llm=False)
    for i in range(MAX_OPEN_THREADS + 1):
        st.merge_threads([{"id": f"T{i}", "title": f"Thread {i}", "status": "open"}], t=float(i))
    assert len(st.open_threads()) == MAX_OPEN_THREADS
    assert st.thread("T0")["status"] == "faded"
    for i in range(THREAD_NOTES + 3):
        st.note("T1", f"step {i}")
    assert len(st.thread("T1")["notes"]) == THREAD_NOTES
    st.merge_threads([{"id": "T1", "status": "resolved", "note": "settled"}])
    assert st.thread("T1")["status"] == "resolved" and st.thread("T1")["notes"][-1]["text"] == "settled"


def test_a_done_errand_becomes_thread_progress_and_a_favor(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import inner_life
    import storyteller as stmod

    errands = {"BigMa": {"to": "AhLok", "topic": "ask AhLok for cash", "thread": "Debt",
                         "ask": {"item": "cash", "why": "the Dragons", "reward": ""}}}
    cleared, favors = [], []
    monkeypatch.setattr(stmod, "read_errand", lambda world, who: errands.get(who))
    monkeypatch.setattr(stmod, "clear_errand", lambda world, who: cleared.append(who))
    monkeypatch.setattr(stmod, "_has_open_favor", lambda *a: False)
    monkeypatch.setattr(inner_life, "register_favor",
                        lambda world, g, h, favor, location="": favors.append((g, h, favor["item"])) or "FavorQ")
    states = {"BigMa": SimpleNamespace(player=False), "AhLok": SimpleNamespace(player=True)}
    loop = SimpleNamespace(world=MagicMock(), sim_time=40.0, get_agent_state=states.get,
                           _is_player=lambda s: s.player)
    st = Storyteller(interval=50.0, use_llm=False)
    st.merge_threads([{"id": "Debt", "title": "Big Ma's debt", "people": ["BigMa", "AhLok"]}])
    st.on_conversation(loop, "BigMa", "AhLok", "FishMarket",
                       [{"speaker": "BigMa", "text": "Lok, could you spare a dollar? The Dragons..."}])
    notes = [n["text"] for n in st.thread("Debt")["notes"]]
    assert any("BigMa talked to AhLok" in n and "spare a dollar" in n for n in notes)
    assert favors == [("BigMa", "AhLok", "cash")] and cleared == ["BigMa"]


def test_an_errand_comes_first_and_expires(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import storyteller as stmod
    from game_loop import GameLoop

    loop = GameLoop(MagicMock(), verbose=False)
    state = SimpleNamespace(agent=SimpleNamespace(name="BigMa"))
    monkeypatch.setattr(stmod, "read_errand", lambda world, who: {"to": "AhLok", "topic": "the debt", "set_at": 0.0})
    loop.sim_time = 10.0
    goal = loop._story_errand_goal(state)
    assert goal["goal"] == "Gain information" and goal["target_entity"] == "AhLok" and goal["source"] == "story"
    loop.sim_time = 1e6
    assert loop._story_errand_goal(state) is None


def test_director_view_describes_talk_and_ask():
    from director_view import beat_line

    assert beat_line({"op": "talk", "who": "Bones", "to": "BigMa", "topic": "the money"}) == \
        "Sent Bones to talk to Big Ma: the money"
    assert beat_line({"op": "ask", "who": "BigMa", "to": "AhLok", "item": "cash", "why": "the Dragons"}) == \
        "Sent Big Ma to ask Ah Lok for cash: the Dragons"
