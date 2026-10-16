"""历史面板的人话：结构化日志 → 一句玩家看得懂的话；对话双方都看得到说了什么。"""

from __future__ import annotations

from narration import entry_line, goal_phrase, pretty


def test_names_and_kinds_read_naturally():
    assert pretty("BerryGrove") == "Berry Grove"
    assert pretty("kind:wood") == "some wood"
    assert pretty("Berry1") == "Berry1"


def test_goals_read_as_intentions():
    assert goal_phrase({"goal": "Go to location", "target_entity": "BerryGrove"}) == "go to Berry Grove"
    assert goal_phrase({"goal": "Gain information", "target_entity": "Sage"}) == "talk to Sage"
    assert goal_phrase({"goal": "Interact with", "target_entity": "OakTree", "interact_action": "chop"}) == "chop (Oak Tree)"


def test_steps_read_from_the_viewers_side():
    talk = {"kind": "done", "action": "talk", "args": ["Ash", "Sage", "CampfireClearing"]}
    assert entry_line(talk, "Ash") == "Talked with Sage."
    assert entry_line(talk, "Sage") == "Ash came over to talk."
    move = {"kind": "start", "action": "move", "args": ["Reed", "RiverBank", "Forest"]}
    assert entry_line(move, "Reed") == "Sets off for Forest."
    failed = {"kind": "failed", "action": "pick_up", "args": ["Ash", "Berry1", "BerryGrove"],
              "message": "Berry1 is not at BerryGrove"}
    assert entry_line(failed, "Ash") == "Tried to pick up Berry1, but couldn't: Berry1 is not at BerryGrove"


def test_decisions_say_what_they_chose():
    chosen = {"kind": "decision", "outcome": "chosen", "goal": {"goal": "Use item on", "target_entity": "Campfire"}}
    assert entry_line(chosen, "Ash") == "Decided to use something on Campfire."
    assert entry_line({"kind": "decision", "outcome": "wait"}, "Ash") == "Couldn't find anything useful to do, so waits."


def test_a_conversation_shows_up_in_both_histories(world):
    from agent_profile import AgentProfile
    from director import build_from_blueprint, load_world
    from game_loop import GameLoop
    from world_simulator import WorldSimulator

    build_from_blueprint(world, load_world("two_npc_talk"), clear=True)
    sim = WorldSimulator()
    sim.sync_agents(world, force_mock=True)
    loop: GameLoop = sim._game_loop
    a, b = sorted(n for n in world.list_entities("Person") if not loop._is_player(loop.get_agent_state(n)))[:2]
    here = world.run_query("MATCH (:Person {name: $n})-[:LOCATED_AT]->(l) RETURN l.name AS l", n=a)[0]["l"]
    state = loop.get_agent_state(a)
    state.current_plan, state.plan_index = [("talk", [a, b, here])], 0
    loop.run_turn(auto_plan=False)
    assert state.last_action_result.success, state.last_action_result.message

    mine = sim.history(a, world=world)["entries"][0]
    theirs = sim.history(b, world=world)["entries"][0]
    assert mine["line"] == f"Talked with {b}." and theirs["line"] == f"{a} came over to talk."
    # Rule mode writes no words; the panel says so instead of showing nothing.
    assert "LLM mode" in mine["note"] and "LLM mode" in theirs["note"]
    sim.reset()


def test_conversations_are_kept_line_by_line_for_the_client():
    from unittest.mock import MagicMock

    from game_loop import CONVERSATION_LIMIT, GameLoop

    loop = GameLoop(MagicMock(), verbose=False)
    loop.record_conversation("Ash", "Sage", "CampfireClearing", [
        {"speaker": "Ash", "text": "Did you take the berries?"},
        {"speaker": "Sage", "text": ""},  # empty lines are dropped
        {"speaker": "Sage", "text": "Only the ones nobody wanted."},
    ])
    (convo,) = loop.conversations
    assert [l["speaker"] for l in convo["lines"]] == ["Ash", "Sage"]
    loop.record_conversation("Ash", "Sage", "CampfireClearing", [])  # nothing said: not recorded
    for i in range(CONVERSATION_LIMIT + 3):
        loop.record_conversation("Reed", "Rowan", "Forest", [{"speaker": "Reed", "text": f"line {i}"}])
    assert len(loop.conversations) == CONVERSATION_LIMIT
    assert len({c["id"] for c in loop.conversations}) == CONVERSATION_LIMIT


def test_both_talkers_stand_until_their_lines_are_read():
    from unittest.mock import MagicMock

    from game_loop import AgentState, GameLoop, PendingAction, reading_seconds

    class Who:
        def __init__(self, name):
            self.name = name

    loop = GameLoop(MagicMock(), verbose=False)
    loop.sim_per_wall = 0.5
    ash, sage, reed = (AgentState(agent=Who(n)) for n in ("Ash", "Sage", "Reed"))
    loop.agents = [ash, sage, reed]
    reed.pending_action = PendingAction("move", ["Reed"], 0.0, 1.0, 1.0)  # busy elsewhere
    lines = [{"speaker": "Ash", "text": "Did you take the berries?"},
             {"speaker": "Sage", "text": "Only the ones nobody wanted."}]
    loop._hold_while_talking(["Ash", "Sage", "Reed"], lines)

    expected = sum(reading_seconds(l["text"]) for l in lines) * 0.5
    assert ash.pending_action.hold and ash.pending_action.duration == expected
    assert sage.pending_action.hold, "the listener waits too"
    assert not reed.pending_action.hold, "someone busy is not interrupted"

    ash.current_plan, ash.plan_index = [("move", ["Ash", "A", "B"])], 0
    assert loop._complete_pending(ash) is None
    assert ash.pending_action is None and ash.plan_index == 0, "standing there doesn't skip a step"


def test_whoever_is_spoken_to_waits_for_the_speaker():
    from unittest.mock import MagicMock

    from game_loop import ENGAGE_WAIT, AgentState, GameLoop, PendingAction
    from registry import act

    class Who:
        def __init__(self, name):
            self.name = name

    loop = GameLoop(MagicMock(), verbose=False)
    loop._check_plan_step = lambda *a, **k: (True, "")
    loop._plan_walk = lambda *a, **k: (None, False)
    loop._is_player = lambda state: False
    ash, sage = AgentState(agent=Who("Ash")), AgentState(agent=Who("Sage"))
    loop.agents = [ash, sage]
    talk = act("converse")
    ash.current_plan, ash.plan_index = [(talk, ["Ash", "Sage", "Camp"])], 0

    # Sage is busy: Ash waits (without skipping the step) and asks Sage to stay put afterwards.
    sage.pending_action = PendingAction("move", ["Sage"], 0.0, 4.0, 4.0)
    started = loop._start_next_step(ash)
    assert started["waiting_for"] == "Sage" and ash.pending_action.hold and ash.plan_index == 0
    assert sage.engaged_by == "Ash"

    # Sage finishes the move: no new step, no new decision while engaged.
    loop.sim_time = 4.1
    sage.pending_action = None
    assert loop._engaged(sage)
    assert loop._pick_deciders([sage], include_player=True, limit=0) == []

    # Ash sets off; the engagement lasts until Ash gets there.
    ash.pending_action = None
    started = loop._start_next_step(ash)
    assert started["action"] == talk and not ash.pending_action.hold
    assert sage.engaged_until > ash.pending_action.completes_at

    # Nobody waits forever.
    loop.sim_time = sage.engaged_until + ENGAGE_WAIT
    assert not loop._engaged(sage) and sage.engaged_by is None


def test_the_plan_shows_what_is_done_doing_and_left():
    from game_loop import AgentState, PendingAction
    from narration import plan_view
    from registry import act

    class Who:
        name = "Ash"

    state = AgentState(agent=Who())
    move, take = act("move"), act("pick_up")
    state.current_plan = [(move, ["Ash", "Shelter", "Forest"]), (take, ["Ash", "Wood1", "Forest"]),
                          (move, ["Ash", "Forest", "CampfireClearing"])]
    state.plan_index = 1
    state.status = "acting"
    state.pending_action = PendingAction(take, ["Ash", "Wood1", "Forest"], 10.0, 4.0, 14.0, walk_seconds=3.0)

    view = plan_view(state, 11.0)
    assert [s["state"] for s in view["steps"]] == ["done", "current", "todo"]
    assert [s["line"] for s in view["steps"]] == ["Go to Forest", "Pick up Wood1", "Go to Campfire Clearing"]
    assert view["step"] == 2
    assert view["now"]["label"] == "On the way: Pick up Wood1" and view["now"]["progress"] == 0.25

    assert plan_view(state, 13.5)["now"]["label"] == "Doing: Pick up Wood1"
    state.pending_action, state.status = None, "failed"
    state.plan_error = "Wood1 is gone"
    assert plan_view(state, 15.0)["now"]["label"] == "The plan failed: Wood1 is gone"


def test_talking_to_the_player_says_one_line_and_waits_for_the_answer(monkeypatch):
    import dialogue_service as ds

    monkeypatch.setattr(ds, "_llm_speaker_utterance", lambda *a, **k: ("Seen any berries today?", {}))
    monkeypatch.setattr(ds, "_llm_dialogue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("answered for the player")))
    out = ds.generate_talk_dialogue(None, "Sage", "Ash", "Camp", listener_speaks=False)
    assert out["lines"] == [{"speaker": "Sage", "text": "Seen any berries today?"}]
    assert out["awaiting_reply"]


def test_an_npc_stands_while_the_player_chats_and_goes_when_they_leave():
    from unittest.mock import MagicMock

    from game_loop import AgentState, GameLoop

    class Who:
        name = "Sage"

    loop = GameLoop(MagicMock(), verbose=False)
    loop._is_player = lambda state: False
    sage = AgentState(agent=Who())
    loop.agents = [sage]
    assert loop.engage("Sage", "Ash", 45.0) and loop._engaged(sage)
    loop.release("Sage", "Reed")  # someone else can't release it
    assert loop._engaged(sage)
    loop.release("Sage", "Ash")
    assert not loop._engaged(sage)


def test_every_step_in_the_history_keeps_the_reason_it_was_done():
    from unittest.mock import MagicMock

    from game_loop import AgentState, GameLoop
    from narration import entry_reason

    class Who:
        name = "Ash"

    loop = GameLoop(MagicMock(), verbose=False)
    ash = AgentState(agent=Who())
    ash.last_goal = {"goal": "Collect item", "target_entity": "Wood1", "reason": "The fire is dying."}
    loop._log(ash, "start", "pick_up(Ash, Wood1, Forest)", action="pick_up", args=["Ash", "Wood1", "Forest"])
    ash.last_goal = {"goal": "Go to location", "target_entity": "RiverBank", "reason": "Wants a bath."}
    loop._log(ash, "done", "talk(Sage, Ash, Camp)", action="talk", own_step=False, why="Sage came over to talk")

    first, second = ash.history
    assert entry_reason(first) == {"for": "get Wood1", "why": "The fire is dying."}, "the goal at the time, not now"
    assert entry_reason(second) == {"why": "Sage came over to talk"}, "someone else's step: their reason"
    decision = {"kind": "decision", "goal": {"goal": "Gain information", "target_entity": "Sage"},
                "triggers": ["Sage arrived"]}
    assert entry_reason(decision) == {"for": "talk to Sage", "why": "Sage arrived"}
