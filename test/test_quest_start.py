"""任务线要玩家接下才开始：听说一件事、捡到小纸条、走到某个地方，任一成立；之前休眠不推进。"""

from __future__ import annotations

import copy

from quest.runtime import check_progress, tellers_to_nudge
from quest.spec import completeness_problems, validate
from quest.verify import verify
from test_quest import GOOD, KNOWN, _camp, _patch

NOTE = {"name": "TornNote", "located_at": "Forest", "item_kind": "note", "description": "Smoke - the old mine?"}


def _with_start(start, items=None):
    return _patch(start=start, items=copy.deepcopy(GOOD["items"]) + (items if items is not None else [NOTE]))


def test_start_is_read_from_the_draft():
    p = _with_start({"hear": "SmokeRumor", "find": ["TornNote"], "visit": []})
    assert (p.start.hear, p.start.find, p.start.visit) == (["SmokeRumor"], ["TornNote"], [])
    assert p.to_dict()["start"]["find"] == ["TornNote"]
    assert _patch().start.empty(), "no start: the questline begins at once (old drafts)"


def test_a_good_start_validates_and_verifies():
    p = _with_start({"hear": ["SmokeRumor"], "find": ["TornNote"], "visit": ["OldMine"]})
    assert validate(p, KNOWN) == []
    result = verify(_camp(), p, "Traveler")
    assert result.ok, result.reason


def test_bad_starts_are_explained():
    cases = [
        ({"hear": ["Gossip"]}, "must be one of this questline's clues"),
        ({"find": ["Nothing"]}, "is not an item"),
        ({"visit": ["Atlantis"]}, "is not a region"),
    ]
    for start, needle in cases:
        problems = validate(_with_start(start), KNOWN)
        assert any(needle in x for x in problems), (start, problems)

    clues = [{"name": "SmokeRumor", "text": "x", "known_by": ["Mara", "Traveler"]}]
    problems = validate(_patch(start={"hear": ["SmokeRumor"]}, clues=clues), KNOWN)
    assert any("already knows it" in x for x in problems), problems

    held = {**NOTE, "located_at": "", "held_by": "Traveler"}
    problems = validate(_with_start({"find": ["TornNote"]}, [held]), KNOWN)
    assert any("already holds it" in x for x in problems), problems


def test_a_start_the_player_is_already_at_is_rejected():
    result = verify(_camp(), _with_start({"visit": ["Camp"]}), "Traveler")
    assert not result.ok and "already true" in result.reason, result.reason


def test_an_unreachable_start_is_rejected_unless_someone_can_tell_them():
    hidden = {**NOTE, "located_at": "", "held_by": "Hollis"}  # 在 Hollis 手里，玩家拿不到
    result = verify(_camp(), _with_start({"find": ["TornNote"]}, [hidden]), "Traveler")
    assert not result.ok and "can't reach any way into" in result.reason, result.reason
    result = verify(_camp(), _with_start({"find": ["TornNote"], "hear": ["SmokeRumor"]}, [hidden]), "Traveler")
    assert result.ok, result.reason


def test_a_complete_package_says_how_the_player_comes_across_it():
    assert any('"start"' in x for x in completeness_problems(_patch()))
    assert not any('"start"' in x for x in completeness_problems(_with_start({"find": ["TornNote"]})))


class FakeWorld:
    """只认 runtime 用到的那几条查询。"""

    def __init__(self, quest, *, knows=(), holds=(), at="Camp"):
        self.quest, self.knows, self.holds, self.at = quest, set(knows), set(holds), at
        self.writes = []

    def run_query(self, cypher, **p):
        if "is_player" in cypher:
            return [{"n": "Traveler"}]
        if "MATCH (q:Quest) WHERE q.questline" in cypher:
            return [dict(self.quest)]
        if "SET" in cypher:
            self.writes.append((cypher, p))
            if "'active'" in cypher:
                self.quest["state"] = "active"
            if "hook_told" in cypher:
                self.quest["hook_told"] = True
            return []
        if "KNOWS_FACT|BELIEVES]->(c {name: $c})" in cypher and "p.name <> $player" in cypher:
            return [{"person": "Mara", "text": "Someone lives in the old mine."}]
        if "KNOWS_FACT|BELIEVES" in cypher:
            return [{"ok": 1}] if p["c"] in self.knows else []
        if "HAS_ITEM" in cypher:
            return [{"ok": 1}] if p["i"] in self.holds else []
        if "RETURN l.name AS loc" in cypher:
            return [{"loc": self.at}]
        if "LOCATED_AT" in cypher:
            return [{"ok": 1}] if p["r"] == self.at else []
        raise AssertionError(cypher)


def _quest():
    import json

    return {"name": "TheOldMine", "title": "The Old Mine", "premise": "", "state": "dormant", "current": 0,
            "stages": json.dumps(GOOD["stages"]), "hook": "Mara saw smoke rising past the forest.",
            "hook_told": False,
            "start": json.dumps({"hear": ["SmokeRumor"], "find": ["TornNote"], "visit": ["OldMine"]})}


def test_a_dormant_questline_waits_for_the_player():
    world = FakeWorld(_quest())
    news = []
    assert check_progress(world, announce=news.append) == []
    assert world.quest["state"] == "dormant" and news == []


def test_finding_the_note_starts_it_and_the_giver_hears_of_it():
    world = FakeWorld(_quest(), holds={"TornNote"})
    news = []
    (done,) = check_progress(world, announce=news.append)
    assert done["started"] and done["how"] == "found TornNote"
    assert done["next"]["giver"] == "Mara", "the first stage's giver gets nudged"
    assert world.quest["state"] == "active"
    assert news and "smoke" in news[0]


def test_hearing_the_clue_starts_it_too():
    world = FakeWorld(_quest(), knows={"SmokeRumor"})
    (done,) = check_progress(world, announce=lambda _: None)
    assert done["how"] == "heard about SmokeRumor"


def test_whoever_knows_the_clue_is_nudged_to_tell_the_player():
    world = FakeWorld(_quest())
    (t,) = tellers_to_nudge(world, "Traveler")
    assert (t["person"], t["clue"]) == ("Mara", "SmokeRumor")
    world.quest["hook_told"] = True
    assert tellers_to_nudge(world, "Traveler") == [], "only once per questline"


# ---------- 任何事都能是接下任务的途径：条件、按顺序发生的事、时段、异象 ----------

from quest import trail  # noqa: E402
from quest.runtime import came_across  # noqa: E402

BACK_AND_FORTH = [{"event": "enter", "what": "Forest"}, {"event": "enter", "what": "Camp"},
                  {"event": "enter", "what": "Forest"}]


def _ev(event, what, t, target=""):
    return {"who": "Traveler", "event": event, "what": what, "target": target, "t": float(t)}


def test_a_pattern_matches_in_order_with_other_things_in_between():
    seen = [_ev("enter", "Forest", 1), _ev("pick_up", "Stick", 2), _ev("enter", "Camp", 3),
            _ev("talk", "Mara", 4), _ev("enter", "Forest", 5)]
    assert trail.matches(BACK_AND_FORTH, seen)
    assert not trail.matches(BACK_AND_FORTH, seen[:3]), "not back yet"
    assert not trail.matches(BACK_AND_FORTH, seen, within=3.0), "too slow"
    poke = [{"event": "do", "what": "interact", "target": "OldWell", "times": 2}]
    assert trail.matches(poke, [_ev("do", "interact", 1, "OldWell"), _ev("do", "interact", 2, "OldWell")])
    assert not trail.matches(poke, [_ev("do", "interact", 1, "OldWell"), _ev("do", "interact", 2, "Tree")])


class TrailWorld:
    def __init__(self):
        self.loc, self.items = "Camp", []

    def run_query(self, cypher, **p):
        return [{"loc": self.loc, "items": list(self.items)}]


def test_the_observer_turns_changes_into_events():
    trail.clear()
    w, obs = TrailWorld(), trail.Observer()
    old = [{"id": "1:Mara:Traveler", "a": "Mara", "b": "Traveler"}]
    obs.observe(w, "Traveler", now=0.0, conversations=old)
    assert trail.events("Traveler") == [], "what was already there is not news"
    w.loc, w.items = "Forest", ["TornNote"]
    obs.observe(w, "Traveler", now=1.0, conversations=old + [{"id": "2:Traveler:Reed", "a": "Traveler", "b": "Reed"}])
    got = [(e["event"], e["what"]) for e in trail.events("Traveler")]
    assert got == [("enter", "Forest"), ("pick_up", "TornNote"), ("talk", "Reed")]
    trail.clear()


def test_new_kinds_of_start_validate():
    p = _with_start({"pattern": BACK_AND_FORTH, "within_hours": 2, "at": "night",
                     "when": [{"op": "prop_gte", "entity": "Strongbox", "prop": "value", "value": 0}],
                     "omen": {"text": "Footsteps echo yours.",
                              "effects": [{"op": "relocate", "entity": "TornNote", "to": "@here"}]}})
    assert validate(p, KNOWN) == []
    assert p.to_dict()["start"]["at"] == "night"


def test_bad_new_starts_are_explained():
    cases = [
        ({"pattern": [{"event": "fly", "what": "Forest"}]}, "event is one of"),
        ({"pattern": [{"event": "enter", "what": "Moon"}]}, "is not a region"),
        ({"pattern": [{"event": "talk", "what": "Traveler"}]}, "is not an NPC"),
        ({"pattern": [{"event": "do", "what": "teleport"}]}, "is not an action"),
        ({"pattern": BACK_AND_FORTH, "at": "teatime"}, "start.at"),
        ({"pattern": BACK_AND_FORTH, "omen": {"effects": []}}, "omen needs a text"),
    ]
    for start, needle in cases:
        problems = validate(_with_start(start), KNOWN)
        assert any(needle in x for x in problems), (start, problems)


def test_a_pacing_pattern_verifies_and_an_unreachable_one_does_not():
    assert verify(_camp(), _with_start({"pattern": BACK_AND_FORTH}), "Traveler").ok
    hidden = {**NOTE, "located_at": "", "held_by": "Hollis"}
    bad = _with_start({"pattern": [{"event": "pick_up", "what": "TornNote"}]}, [hidden])
    result = verify(_camp(), bad, "Traveler")
    assert not result.ok and "can't reach any way into" in result.reason, result.reason


def test_time_of_day_gates_every_way_in():
    world = FakeWorld(_quest(), holds={"TornNote"})
    start = {"find": ["TornNote"], "at": "night"}
    assert came_across(world, "Traveler", start, hour=14.0) is None, "daytime: doesn't count"
    assert came_across(world, "Traveler", start, hour=23.5) == "found TornNote"
    assert came_across(world, "Traveler", {**start, "at": "22-4"}, hour=2.0) == "found TornNote"


def test_pacing_back_and_forth_wakes_the_footsteps():
    import json

    q = _quest()
    q["start"] = json.dumps({"pattern": BACK_AND_FORTH, "omen": {"text": "Footsteps echo yours..."}})
    world, news = FakeWorld(q), []
    seen = [_ev("enter", "Forest", 1), _ev("enter", "Camp", 2)]
    assert check_progress(world, announce=news.append, seen_since=lambda _: seen) == []
    seen.append(_ev("enter", "Forest", 3))
    (done,) = check_progress(world, announce=news.append, seen_since=lambda _: seen)
    assert done["started"] and world.quest["state"] == "active"
    assert news[0] == "Footsteps echo yours...", "the omen comes first"
