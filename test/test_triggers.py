import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))

from triggers import (  # noqa: E402
    INTERRUPT_PRIORITY,
    WAKE_PRIORITY,
    Observation,
    describe_triggers,
    diff_triggers,
)


class _Slot:
    def __init__(self, key, activity, location):
        self.key = key
        self.activity = activity
        self.goal = "Go to location"
        self.location = location


def _obs(**kw):
    base = dict(slot="work", location="Market", alerts=frozenset(), people=frozenset(),
                inventory=frozenset(), hp=100)
    base.update(kw)
    return Observation(**base)


def _diff(prev, cur, alerts=None, slot=None):
    return diff_triggers(prev, cur, alerts=alerts or {}, slot=slot, clock="Day 1 08:00", sim_time=10.0)


def test_first_observation_starts_decision():
    out = _diff(None, _obs())
    assert [t.kind for t in out] == ["start"]


def test_nothing_changed_no_triggers():
    assert _diff(_obs(), _obs()) == []


def test_routine_slot_change():
    slot = _Slot("lunch", "order lunch", "Tavern")
    out = _diff(_obs(), _obs(slot="lunch"), slot=slot)
    assert [t.kind for t in out] == ["routine_slot"]
    assert out[0].source == "routine"
    assert "order lunch" in out[0].text and "Tavern" in out[0].text


def test_new_alert_priority_follows_signal():
    key = "Stove|critical|on fire"
    alerts = {key: {"item": "Stove", "signal": "critical", "message": "on fire"}}
    out = _diff(_obs(), _obs(alerts=frozenset([key])), alerts=alerts)
    assert out[0].kind == "alert"
    assert out[0].priority >= WAKE_PRIORITY
    # 已存在的告警不重复触发
    assert _diff(_obs(alerts=frozenset([key])), _obs(alerts=frozenset([key])), alerts=alerts) == []


def test_person_arrived_only_when_staying_put():
    out = _diff(_obs(), _obs(people=frozenset(["Bruno"])))
    assert [t.kind for t in out] == ["person_arrived"]
    # 自己走进一个有人的地方不算「有人来」
    assert _diff(_obs(), _obs(location="Tavern", people=frozenset(["Bruno"]))) == []


def test_received_item_and_hurt():
    out = _diff(_obs(), _obs(inventory=frozenset(["Meal1"]), hp=80))
    kinds = {t.kind: t for t in out}
    assert "received_item" in kinds and "hurt" in kinds
    assert kinds["hurt"].priority >= INTERRUPT_PRIORITY
    assert kinds["received_item"].priority < INTERRUPT_PRIORITY


def test_describe_sorted_by_priority():
    out = _diff(_obs(), _obs(inventory=frozenset(["Meal1"]), hp=80))
    text = describe_triggers(out)
    assert text.index("HP") < text.index("Meal1")
