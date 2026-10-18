"""NPC 赶路时偶尔嘀咕：心情图标 / 有的没的 / 说漏要紧事（玩家在旁边就算听到了）。"""

from __future__ import annotations

import json
import random
from types import SimpleNamespace

import mutter as m
from mutter import Mutterer, mood_icon


class Agent:
    def __init__(self, name, mood="calm"):
        self.name, self.mood = name, mood


def _walking(name, mood="calm", path=((0, 0), (1, 0), (2, 0))):
    from game_loop import PendingAction

    return SimpleNamespace(agent=Agent(name, mood), pending_action=PendingAction(
        name="move", args=[name, "Forest"], started_at=0.0, duration=3.0, completes_at=3.0, path=list(path)))


class FakeWorld:
    def __init__(self, *, knows=None, dormant_hear=(), player_near=True):
        self.knows = knows or {}
        self.dormant_hear = list(dormant_hear)
        self.player_near = player_near
        self.links = []

    def run_query(self, cypher, **p):
        if "MATCH (p:Person {name: $n}) RETURN coalesce(p.mood" in cypher:
            return [{"mood": "furious", "hp": 90, "personality": "gruff", "goal": "chop wood"}]
        if "MATCH (q:Quest) WHERE q.questline" in cypher:
            if not self.dormant_hear:
                return []
            return [{"name": "Q", "state": "dormant", "stages": "[]", "current": 0,
                     "start": json.dumps({"hear": self.dormant_hear})}]
        if "KNOWS_FACT|BELIEVES]->(c)" in cypher:
            return [{"name": c, "text": t, "labels": ["Fact"]}
                    for c, t in self.knows.get(p["n"], {}).items() if c in p["wanted"]]
        if "<-[:LOCATED_AT]-(p:Person)" in cypher:
            return [{"player": "Ash", "nx": 3, "ny": 3, "px": 4 if self.player_near else 40, "py": 3}]
        raise AssertionError(cypher)

    def link_entities(self, a, la, b, lb, rel):
        self.links.append((a, b, rel))


def _loop(world, *states):
    return SimpleNamespace(
        world=world, agents=list(states), sim_time=10.0, force_mock=True, memory=None,
        _is_player=lambda s: s.agent.name == "Ash", _rule_based=lambda s: True,
    )


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_mood_words_map_to_icons():
    assert mood_icon("hopeful") == "happy"
    assert mood_icon("Furious with Reed") == "angry"
    assert mood_icon("wary") == "scared"
    assert mood_icon("something odd") == "thinking"
    assert mood_icon("happy", hp=20) == "hurt", "badly hurt shows first"


def test_walking_or_standing_people_mutter_but_not_mid_conversation(monkeypatch):
    from game_loop import PendingAction

    monkeypatch.setattr(m, "CHANCE", 1.0)
    talking = SimpleNamespace(agent=Agent("Sage"), pending_action=None, status="talking")
    called = SimpleNamespace(agent=Agent("Moss"), pending_action=None, engaged_by="Reed")
    chatting = SimpleNamespace(agent=Agent("Fern"), pending_action=PendingAction(
        name="talk", args=["Fern", "Sage", "Camp"], started_at=0.0, duration=1.0, completes_at=1.0))
    loop = _loop(FakeWorld(), talking, called, chatting)
    Mutterer(rng=random.Random(1), use_llm=False, clock=Clock()).on_tick(loop)
    assert not getattr(loop, "mutters", None), "nobody mutters over a conversation"

    for state in (_walking("Reed"), SimpleNamespace(agent=Agent("Reed"), pending_action=None)):
        loop = _loop(FakeWorld(), state)
        Mutterer(rng=random.Random(1), use_llm=False, clock=Clock()).on_tick(loop)
        (entry,) = loop.mutters
        assert entry["who"] == "Reed" and entry["icon"] == "angry" and entry["emoji"]


def test_the_model_is_told_what_they_are_doing():
    from game_loop import PendingAction

    assert "standing around" in m._doing(SimpleNamespace(pending_action=None))
    assert "on your way to" in m._doing(_walking("Reed"))
    chop = PendingAction(name="chop", args=["Reed", "Tree"], started_at=0.0, duration=2.0, completes_at=2.0)
    assert "busy with" in m._doing(SimpleNamespace(pending_action=chop))


def test_people_do_not_mutter_all_the_time(monkeypatch):
    monkeypatch.setattr(m, "CHANCE", 1.0)
    clock = Clock()
    loop = _loop(FakeWorld(), _walking("Reed"))
    mut = Mutterer(rng=random.Random(2), use_llm=False, clock=clock)
    for _ in range(5):
        mut.on_tick(loop)
    assert len(loop.mutters) == 1
    clock.now += m.PERSON_GAP + 1
    mut.on_tick(loop)
    assert len(loop.mutters) == 2


def test_a_secret_slips_out_and_the_player_nearby_hears_it(monkeypatch):
    monkeypatch.setattr(m, "CHANCE", 1.0)
    monkeypatch.setattr(m, "LEAK_CHANCE", 1.0)
    world = FakeWorld(knows={"Reed": {"MineSmoke": "Someone lives in the old mine."}}, dormant_hear=["MineSmoke"])
    loop = _loop(world, _walking("Reed"))
    Mutterer(rng=random.Random(3), use_llm=False, clock=Clock()).on_tick(loop)
    (entry,) = loop.mutters
    assert entry["important"] and entry["overheard"] and "old mine" in entry["text"]
    assert world.links == [("Ash", "MineSmoke", "KNOWS_FACT")], "the player now knows it -> the questline can start"


def test_nobody_hears_a_secret_muttered_far_away(monkeypatch):
    monkeypatch.setattr(m, "CHANCE", 1.0)
    monkeypatch.setattr(m, "LEAK_CHANCE", 1.0)
    world = FakeWorld(knows={"Reed": {"MineSmoke": "x"}}, dormant_hear=["MineSmoke"], player_near=False)
    loop = _loop(world, _walking("Reed"))
    Mutterer(rng=random.Random(3), use_llm=False, clock=Clock()).on_tick(loop)
    assert loop.mutters[0]["important"] and not loop.mutters[0]["overheard"]
    assert world.links == []


def test_llm_lines_are_written_in_the_background(monkeypatch):
    monkeypatch.setattr(m, "CHANCE", 1.0)
    monkeypatch.setattr(m, "MOOD_ONLY_CHANCE", 0.0)
    monkeypatch.setattr(m, "_llm_line", lambda name, info, doing, icon: f"{name} grumbles about {doing}")
    clock = Clock()
    loop = _loop(FakeWorld(), _walking("Reed"))
    loop.force_mock, loop._rule_based = False, (lambda s: False)
    mut = Mutterer(rng=random.Random(4), clock=clock)
    mut._llm_allowed = lambda loop, state: True
    mut.on_tick(loop)
    mut._pending[0][0].result(timeout=5)
    mut.on_tick(loop)  # 下一拍收结果
    (entry,) = loop.mutters
    assert entry["text"].startswith("Reed grumbles about")
