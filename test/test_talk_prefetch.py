"""搭话台词出发时就在后台写：走到了还没写好就站着等（不卡这一拍），写好了直接用，不再当场调大模型。"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest


class Who:
    def __init__(self, name):
        self.name = name


@pytest.fixture
def loop(monkeypatch):
    from game_loop import AgentState, GameLoop

    loop = GameLoop(MagicMock(), verbose=False)
    loop._is_player = lambda state: False
    loop._rule_based_name = lambda agent: False
    loop.agents = [AgentState(agent=Who(n)) for n in ("Reed", "Sage")]
    yield loop
    loop.close()


@pytest.fixture
def slow_llm(monkeypatch):
    """假的生成：等 release 才返回，记下参数。"""
    import dialogue_service

    release = threading.Event()
    calls = []

    def fake(world, a, b, loc, **kw):
        calls.append((a, b, loc, kw))
        release.wait(5)
        return {"lines": [{"speaker": a, "text": "Hi."}, {"speaker": b, "text": "Hello."}],
                "source": "llm", "trust_delta": 1, "trust_base": 0}

    monkeypatch.setattr(dialogue_service, "generate_talk_dialogue", fake)
    return release, calls


def _converse(loop, args):
    from game_loop import PendingAction
    from registry import act

    state = loop.get_agent_state(args[0])
    state.pending_action = PendingAction(name=act("converse"), args=list(args), started_at=0.0,
                                         duration=1.0, completes_at=1.0)
    loop._prefetch_talk(state, act("converse"), list(args))
    return state


def test_lines_are_written_in_the_background_without_touching_the_world(loop, slow_llm):
    release, calls = slow_llm
    _converse(loop, ["Reed", "Sage", "Camp"])
    release.set()
    loop._talk_prefetch["Reed"]["future"].result(timeout=5)
    (a, b, loc, kw) = calls[0]
    assert (a, b, loc) == ("Reed", "Sage", "Camp")
    assert kw["apply_trust"] is False, "background generation must not write trust"
    assert kw["listener_speaks"] is True


def test_arriving_before_the_lines_are_ready_waits_without_blocking(loop, slow_llm):
    release, _ = slow_llm
    state = _converse(loop, ["Reed", "Sage", "Camp"])
    loop.sim_time = 1.0
    assert loop._awaiting_lines(state), "not ready yet: keep standing"
    assert state.pending_action.completes_at > loop.sim_time
    assert loop.get_agent_state("Sage").engaged_by == "Reed", "the listener is held in place"

    release.set()
    loop._talk_prefetch["Reed"]["future"].result(timeout=5)
    assert not loop._awaiting_lines(state), "ready: the talk can happen"
    got = loop._take_prefetched_talk("Reed", ["Reed", "Sage", "Camp"])
    assert [ln["text"] for ln in got["lines"]] == ["Hi.", "Hello."]
    assert "Reed" not in loop._talk_prefetch


def test_a_different_talk_does_not_use_stale_lines(loop, slow_llm):
    release, _ = slow_llm
    release.set()
    _converse(loop, ["Reed", "Sage", "Camp"])
    assert loop._take_prefetched_talk("Reed", ["Reed", "Sage", "Forest"]) is None


def test_giving_up_after_the_timeout(loop, slow_llm, monkeypatch):
    import game_loop

    state = _converse(loop, ["Reed", "Sage", "Camp"])
    loop._talk_prefetch["Reed"]["t0"] -= game_loop.TALK_PREFETCH_TIMEOUT + 1
    loop.sim_time = 1.0
    assert not loop._awaiting_lines(state), "waited long enough"
    got = loop._take_prefetched_talk("Reed", ["Reed", "Sage", "Camp"])
    assert got["lines"] == [] and got["source"] == "error"
    slow_llm[0].set()


def test_rule_based_npcs_are_not_prefetched(loop, slow_llm):
    loop._rule_based_name = lambda agent: True
    _converse(loop, ["Reed", "Sage", "Camp"])
    assert "Reed" not in loop._talk_prefetch
    assert slow_llm[1] == []


def test_trust_is_settled_only_when_the_talk_happens(monkeypatch):
    import dialogue_service as ds

    settled = []
    monkeypatch.setattr(ds, "_settle_trust", lambda w, a, b, r, base: settled.append(base) or r)
    monkeypatch.setattr(ds, "write_dialogue_memories", lambda *a, **k: None)
    dlg = {"lines": [], "source": "llm", "trust_delta": 2, "trust_base": 3}
    out = ds.apply_talk_dialogue(None, dlg, "Reed", "Sage", "Camp")
    assert settled == [3] and "trust_base" not in out
    ds.apply_talk_dialogue(None, {"lines": [], "source": "error"}, "Reed", "Sage", "Camp")
    assert settled == [3], "nothing to settle without a deferred trust"
