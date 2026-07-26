"""日常日程与昼夜单元测试（不依赖 Neo4j）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from routine import (  # noqa: E402
    DAY_HOUR_OFFSET,
    DAY_LENGTH_SECONDS,
    current_slot,
    day_phase,
    format_clock,
    hour_of_day,
    is_meal_time,
    is_night,
    parse_routine,
    routine_goal_candidate,
    should_social_interrupt,
    social_interrupt_goal,
)


def test_hour_offset_starts_at_dawn():
    assert abs(hour_of_day(0.0) - DAY_HOUR_OFFSET) < 1e-6
    h = hour_of_day(DAY_LENGTH_SECONDS / 2)
    assert abs(h - ((DAY_HOUR_OFFSET + 12) % 24)) < 1e-6


def test_day_night_and_meals():
    assert day_phase(7) == "dawn"
    assert day_phase(10) == "morning"
    assert day_phase(12.5) == "lunch"
    assert is_meal_time(12.5)
    assert is_meal_time(19)
    assert is_night(22)
    assert is_night(3)
    assert not is_night(10)
    assert "昼" in format_clock(0.0) or "夜" in format_clock(0.0)


def test_parse_and_current_slot_cross_midnight():
    slots = parse_routine(
        [
            {"start": 8, "end": 17, "location": "Forest", "activity": "chop"},
            {"start": 17, "end": 21, "location": "CampfireClearing", "activity": "fire", "social": True},
            {"start": 21, "end": 8, "location": "Shelter", "activity": "sleep"},
        ]
    )
    assert len(slots) == 3
    # sim 0 → 6:00 不在 8-17；落在 sleep 跨夜槽
    s = current_slot(slots, 0.0)
    assert s is not None and s.location == "Shelter"
    # 约 10:00
    hour_frac = (10.0 - DAY_HOUR_OFFSET) % 24
    t = (hour_frac / 24.0) * DAY_LENGTH_SECONDS
    s = current_slot(slots, t)
    assert s is not None and s.location == "Forest"


def test_routine_goal_and_social_gate():
    slots = parse_routine(
        [
            {
                "start": 18.5,
                "end": 21,
                "location": "CampfireClearing",
                "activity": "dinner",
                "social": True,
                "goal": "Gain information",
            }
        ]
    )
    slot = slots[0]
    cand = routine_goal_candidate(slot, character="Ash", current_location="Forest")
    assert cand["goal"] == "Go to location"
    assert cand["target_entity"] == "CampfireClearing"

    assert should_social_interrupt(
        slot=slot,
        current_goal={"goal": "Go to location", "target_entity": "CampfireClearing"},
        social_target="Rowan",
        last_social_at=-1e9,
        sim_time=10.0,
    )
    g = social_interrupt_goal("Rowan", activity="dinner")
    assert g["goal"] == "Gain information" and g["target_entity"] == "Rowan"


if __name__ == "__main__":
    test_hour_offset_starts_at_dawn()
    test_day_night_and_meals()
    test_parse_and_current_slot_cross_midnight()
    test_routine_goal_and_social_gate()
    print("ok")
