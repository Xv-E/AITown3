"""任务线监听器：听事件总线上的任何事（玩家 / NPC / 时辰 / 消息 / 任务进度），成立就说一句、改世界、
接下任务线或提醒某个 NPC。规划器能编译的监听器也算玩家能做的事，阶段要的标记可以由它给。"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from quest import trail
from quest.listeners import ListenerMemory, evaluate, run
from quest.spec import _listener, validate
from quest.verify import listener_actions, verify
from test_quest import GOOD, KNOWN, _camp, _patch


@pytest.fixture(autouse=True)
def fresh_trail():
    trail.clear()
    yield
    trail.clear()


def _ev(who, event, what, t, target=""):
    return {"who": who, "event": event, "what": what, "target": target, "t": float(t)}


# ---------- 事件总线 ----------

def test_who_filters():
    e = _ev("Reed", "enter", "Storehouse", 1)
    assert trail.hit({"event": "enter", "what": "Storehouse"}, e, "Traveler")
    assert trail.hit({"event": "enter", "who": "npc"}, e, "Traveler")
    assert trail.hit({"event": "enter", "who": "Reed"}, e, "Traveler")
    assert not trail.hit({"event": "enter", "who": "@player"}, e, "Traveler")
    assert trail.hit({"event": "news", "contains": "fire"}, _ev("", "news", "The FIRE went out", 2))


def test_counting_within_a_window():
    seen = [_ev("Traveler", "do", "interact", t, "OldWell") for t in (1, 2, 30)]
    on = {"event": "do", "who": "@player", "what": "interact", "target": "OldWell"}
    assert trail.find_count(on, 3, seen, player="Traveler")
    assert not trail.find_count(on, 3, seen, within=10, player="Traveler")
    assert trail.find_count(on, 2, seen, within=10, player="Traveler")


def test_the_observer_sees_npcs_time_and_news():
    from routine import DAY_LENGTH_SECONDS, hour_of_day

    class World:
        def __init__(self):
            self.rows = [{"who": "Reed", "loc": "Camp", "items": []}, {"who": "Traveler", "loc": "Camp", "items": []}]

        def run_query(self, cypher, **p):
            return [{"names": []}] if "KNOWS_FACT" in cypher else self.rows

    w = World()
    reed = SimpleNamespace(agent=SimpleNamespace(name="Reed"), history=[{"kind": "done", "action": "chop", "args": ["Reed", "Tree"]}])
    loop = SimpleNamespace(agents=[reed], chronicle=[{"t": 0, "text": "old news"}])
    obs = trail.Observer()
    t0 = 0.0
    while hour_of_day(t0) >= 20 or hour_of_day(t0) < 6:  # 从白天开始
        t0 += DAY_LENGTH_SECONDS / 24
    obs.observe(w, "Traveler", now=t0, loop=loop)
    assert trail.events() == [], "the first look only records how things are"

    w.rows[0]["loc"] = "Storehouse"
    reed.history.append({"kind": "done", "action": "pick_up", "args": ["Reed", "Grain"]})
    loop.chronicle.append({"t": 5, "text": "The storehouse door was found open."})
    later = t0
    while not (hour_of_day(later) >= 21 or hour_of_day(later) < 5):  # 到夜里
        later += DAY_LENGTH_SECONDS / 24
    obs.observe(w, "Traveler", now=later, loop=loop)
    got = {(e["who"], e["event"], e["what"]) for e in trail.events()}
    assert ("Reed", "enter", "Storehouse") in got
    assert ("Reed", "do", "pick_up") in got and ("Reed", "do", "chop") not in got, "only new history"
    assert ("", "news", "The storehouse door was found open.") in got
    assert ("", "time", "night") in got


# ---------- 写法与校验 ----------

FOOTSTEPS = {"id": "Footsteps", "active": "dormant",
             "sequence": [{"event": "enter", "who": "@player", "what": "Forest"},
                          {"event": "enter", "who": "@player", "what": "Camp"},
                          {"event": "enter", "who": "@player", "what": "Forest"}],
             "within_hours": 2, "at": "night",
             "then": {"say": "Footsteps echo yours.", "start": True}}
KNOCKING = {"id": "Knocking", "active": "stage:s2", "on": {"event": "enter", "who": "@player", "what": "OldMine"},
            "then": {"say": "Three knocks.", "effects": [{"op": "set_prop", "entity": "OldMine", "prop": "knocked",
                                                           "value": True}]}}
GOSSIP = {"id": "Gossip", "active": "active", "on": {"event": "enter", "who": "Hollis", "what": "Camp"},
          "then": {"nudge": {"who": "Mara", "text": "Hollis came to camp. Tell @player."}}}


def test_listeners_parse_and_validate():
    p = _patch(listeners=[FOOTSTEPS, KNOCKING, GOSSIP])
    assert validate(p, KNOWN) == []
    ls = p.listeners[0]
    assert ls.phase() == "dormant" and ls.fires_once()
    assert p.listeners[1].phase() == "stage:s2" and not p.listeners[1].fires_once()
    assert p.to_dict()["listeners"][2]["then"]["nudge"]["who"] == "Mara"


@pytest.mark.parametrize("change, needle", [
    ({"active": "someday"}, "active is dormant"),
    ({"active": "active"}, "must listen while it is dormant"),
    ({"sequence": [{"event": "teleport", "what": "Forest"}]}, "event is one of"),
    ({"sequence": [{"event": "enter", "who": "Ghost", "what": "Forest"}]}, "who is @player"),
    ({"sequence": [{"event": "enter", "what": "Moon"}]}, "is not a region"),
    ({"at": "teatime"}, "at 'teatime'"),
    ({"then": {}}, "then needs"),
    ({"then": {"nudge": {"who": "Traveler", "text": "x"}}}, "nudge is"),
    ({"count": 99}, "count is 1-10"),
])
def test_bad_listeners_are_explained(change, needle):
    problems = validate(_patch(listeners=[{**FOOTSTEPS, **change}]), KNOWN)
    assert any(needle in x for x in problems), problems


# ---------- 规划器 ----------

def _knocking_patch(listeners):
    stages = copy.deepcopy(GOOD["stages"])
    stages[1]["complete_when"] = [{"op": "prop_eq", "entity": "OldMine", "prop": "knocked", "value": True}]
    return _patch(stages=stages, listeners=listeners)


def test_a_listener_can_give_what_a_stage_needs():
    (action, ls), = listener_actions(_knocking_patch([KNOCKING]))
    assert ls.id == "Knocking"
    assert {"op": "person_at", "person": "@c", "location": "OldMine"} in action["requires"]
    result = verify(_camp(), _knocking_patch([KNOCKING]), "Traveler")
    assert result.ok, result.reason
    result = verify(_camp(), _knocking_patch([]), "Traveler")
    assert not result.ok and "stage 2" in result.reason, "without the listener nobody knocks"


def test_a_listener_can_be_the_way_in():
    assert verify(_camp(), _patch(listeners=[FOOTSTEPS]), "Traveler").ok
    # 只等 NPC 做事：规划器证明不了，不拦
    waits = {**GOSSIP, "id": "Arrival", "active": "dormant", "then": {"start": True}}
    assert verify(_camp(), _patch(listeners=[waits]), "Traveler").ok


# ---------- 运行时 ----------

class QuestWorld:
    def __init__(self):
        self.saved = {}

    def run_query(self, cypher, **p):
        if "listener_state" in cypher:
            self.saved[p["n"]] = json.loads(p["s"])
            return []
        if "RETURN l.name AS loc" in cypher:
            return [{"loc": "Forest"}]
        raise AssertionError(cypher)


def _quest(listeners, state="dormant"):
    return {"name": "TheOldMine", "state": state, "current": 0, "stages": copy.deepcopy(GOOD["stages"]),
            "listeners": json.dumps(listeners), "listener_state": None, "start": {}}


def _run(world, q, now, hour=23.0, started=None, said=None, nudged=None, memory=None):
    return run(world, [q], player="Traveler", now=now, hour=hour, memory=memory or ListenerMemory(),
               announce=(said if said is not None else []).append,
               nudge=lambda who, kind, text, pr: (nudged if nudged is not None else []).append((who, text)) or True,
               start_quest=lambda quest, how, here: (started if started is not None else []).append(how) or
               {"quest": quest["name"], "started": True, "how": how})


def test_pacing_at_night_starts_the_questline_once():
    world, q, mem, said, started = QuestWorld(), _quest([FOOTSTEPS]), ListenerMemory(), [], []
    _run(world, q, 0.0, memory=mem, said=said, started=started)  # 开始听
    for t, where in ((1, "Forest"), (2, "Camp")):
        trail.note("Traveler", "enter", where, at=t)
    assert _run(world, q, 3.0, memory=mem, said=said, started=started) == []
    trail.note("Traveler", "enter", "Forest", at=4)
    assert _run(world, q, 4.0, hour=14.0, memory=mem, said=said, started=started) == [], "not at night"
    done = _run(world, q, 5.0, memory=mem, said=said, started=started)
    assert done and said == ["Footsteps echo yours."] and started
    assert world.saved["TheOldMine"]["Footsteps"]["n"] == 1


def test_repeating_listeners_need_new_events_and_respect_the_cooldown():
    knock = {**KNOCKING, "active": "active", "cooldown_hours": 1, "then": {"say": "Knock."}}
    world, q, mem, said = QuestWorld(), _quest([knock], state="active"), ListenerMemory(), []
    _run(world, q, 0.0, memory=mem, said=said)
    trail.note("Traveler", "enter", "OldMine", at=1)
    _run(world, q, 1.0, memory=mem, said=said)
    q["listener_state"] = json.dumps(world.saved["TheOldMine"])
    _run(world, q, 2.0, memory=mem, said=said)
    assert said == ["Knock."], "the same visit doesn't knock twice"
    trail.note("Traveler", "enter", "OldMine", at=3)
    _run(world, q, 3.0, memory=mem, said=said)
    assert said == ["Knock."], "still cooling down"
    from routine import DAY_LENGTH_SECONDS

    later = 3.0 + DAY_LENGTH_SECONDS  # 冷却过了，但那次进门已经过去了：要再进一次
    trail.note("Traveler", "enter", "OldMine", at=later)
    _run(world, q, later, memory=mem, said=said)
    assert said == ["Knock.", "Knock."]


def test_npc_events_nudge_someone():
    world, q, mem, nudged = QuestWorld(), _quest([GOSSIP], state="active"), ListenerMemory(), []
    _run(world, q, 0.0, memory=mem, nudged=nudged)
    trail.note("Hollis", "enter", "Camp", at=1)
    _run(world, q, 1.0, memory=mem, nudged=nudged)
    assert nudged == [("Mara", "Hollis came to camp. Tell Traveler.")]


def test_listeners_only_listen_in_their_phase():
    world, q, mem, said = QuestWorld(), _quest([KNOCKING], state="active"), ListenerMemory(), []
    _run(world, q, 0.0, memory=mem, said=said)
    trail.note("Traveler", "enter", "OldMine", at=1)
    _run(world, q, 1.0, memory=mem, said=said)
    assert said == [], "stage s2 hasn't begun"


def test_condition_only_listeners_fire_on_the_rising_edge():
    ls = _listener({"id": "WellDry", "when": [{"op": "prop_lte", "entity": "OldWell", "prop": "water", "value": 10}],
                    "then": {"say": "The well is dry."}})
    state = {"dry": False}
    check = lambda conds, ctx: state["dry"]  # noqa: E731
    assert evaluate(ls, [], player="Traveler", hour=None, check=check) is None
    state["dry"] = True
    assert evaluate(ls, [], player="Traveler", hour=None, check=check) is not None
