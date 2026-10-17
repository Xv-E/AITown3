"""聊完话要想想要不要改主意：触发器带上原话、玩家的话能打断计划、决策 prompt 里看得到刚才说了什么。"""

from __future__ import annotations

from unittest.mock import MagicMock


class Who:
    def __init__(self, name):
        self.name = name


def _loop(*names, player=None):
    from game_loop import AgentState, GameLoop

    loop = GameLoop(MagicMock(), verbose=False)
    loop._is_player = lambda state: state.agent.name == player
    loop.agents = [AgentState(agent=Who(n)) for n in names]
    return loop


def test_a_finished_conversation_makes_both_think_again():
    from game_loop import PLAYER_TALK_PRIORITY, TALK_PRIORITY
    from triggers import INTERRUPT_PRIORITY

    loop = _loop("Ash", "Sage", "Reed", player="Ash")
    lines = [{"speaker": "Reed", "text": "The fire needs wood."}, {"speaker": "Sage", "text": "I'll get some."}]
    assert loop.heard("Sage", "Reed", lines)
    (t,) = loop.get_agent_state("Sage").pending_triggers
    assert t.kind == "conversation" and "The fire needs wood." in t.text
    assert t.priority == TALK_PRIORITY < INTERRUPT_PRIORITY, "NPC chat waits for the current step"

    asked = [{"speaker": "Ash", "text": "Could you chop some wood for me?"}]
    loop.heard("Sage", "Ash", asked)
    loop.heard("Sage", "Ash", asked + [{"speaker": "Sage", "text": "Maybe later."}])
    from_player = [x for x in loop.get_agent_state("Sage").pending_triggers if x.data.get("with") == "Ash"]
    assert len(from_player) == 1, "a newer round replaces the older one"
    assert from_player[0].priority == PLAYER_TALK_PRIORITY >= INTERRUPT_PRIORITY, "the player's request can interrupt"
    assert "decide whether to do it" in from_player[0].text
    assert not loop.heard("Ash", "Sage", lines), "the player decides for themselves"


def test_the_decision_sees_what_was_just_said():
    loop = _loop("Ash", "Sage")
    loop.sim_time = 100.0
    loop.record_conversation("Ash", "Sage", "Camp", [
        {"speaker": "Ash", "text": "Could you chop some wood?"},
        {"speaker": "Sage", "text": "Fine, I'll do it."},
    ])
    talk = loop.recent_talk("Sage")
    assert "With Ash at Camp" in talk and 'Ash: "Could you chop some wood?"' in talk
    loop.sim_time = 1000.0
    assert loop.recent_talk("Sage") == "", "old conversations are not 'just said'"


def test_the_situation_block_carries_the_talk_and_feelings():
    from prompt_builder import PromptBuilder

    pb = PromptBuilder(world=MagicMock(), memory=None)
    for name in ("_get_tensions_text", "_get_local_graph_text", "_get_routine_text",
                 "_get_known_facts_text", "_get_memories_text"):
        setattr(pb, name, lambda *a, **k: "")
    pb.world.run_query = lambda *a, **k: [{"player": "Ash", "trust": 6, "me_player": False}]
    agent = Who("Sage")
    agent._recent_talk = 'With Ash at Camp:\n  Ash: "Could you chop some wood?"'
    text = pb.build_situation_block(agent)
    assert "=== What Was Just Said ===" in text and "chop some wood" in text
    assert "You trust Ash a lot" in text


def test_npc_talk_does_not_change_trust_toward_the_player(monkeypatch):
    import dialogue_service as ds

    world = MagicMock()
    world.run_query = lambda q, **k: [{"pl": False, "role": "npc"}] if "is_player" in q else []
    assert ds._is_player_name(world, "Reed") is False
    world.run_query = lambda q, **k: [{"pl": True, "role": "player"}]
    assert ds._is_player_name(world, "Ash") is True
