"""
日常日程 — 将 sim_time 映射为一日时段，驱动个人 routine 与社交打断。

每个 Person 可挂 routine（JSON 数组），槽位示例：
  {"start": 8, "end": 17, "location": "Tavern", "activity": "run tavern",
   "goal": "Go to location", "social": true}
跨午夜用 start > end（如 21→6）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

# 默认：300 仿真秒 = 一天（生产循环稍慢一点看得清）
DAY_LENGTH_SECONDS = 300.0
# sim_time=0 对应黎明 6:00
DAY_HOUR_OFFSET = 6.0
SOCIAL_INTERRUPT_COOLDOWN = 18.0
ROUTINE_GOAL_PRIORITY = 0.72
SOCIAL_GOAL_PRIORITY = 0.88
FIRE_EMERGENCY_PRIORITY = 0.95

# 昼夜分段（小时）
NIGHT_START = 21.0
NIGHT_END = 6.0
MEAL_WINDOWS = ((12.0, 14.0), (18.5, 20.5))


def is_night(hour: float) -> bool:
    h = float(hour) % 24
    return h >= NIGHT_START or h < NIGHT_END


def is_meal_time(hour: float) -> bool:
    h = float(hour) % 24
    for a, b in MEAL_WINDOWS:
        if a <= h < b:
            return True
    return False


def day_phase(hour: float) -> str:
    h = float(hour) % 24
    if 5 <= h < 8:
        return "dawn"
    if 8 <= h < 12:
        return "morning"
    if 12 <= h < 14:
        return "lunch"
    if 14 <= h < 17:
        return "afternoon"
    if 17 <= h < 18.5:
        return "dusk"
    if 18.5 <= h < 21:
        return "dinner"
    return "night"


def phase_label(phase: str) -> str:
    return {
        "dawn": "Dawn",
        "morning": "Morning",
        "lunch": "Lunch",
        "afternoon": "Afternoon",
        "dusk": "Dusk",
        "dinner": "Dinner",
        "night": "Night",
    }.get(phase, phase)


@dataclass(frozen=True)
class RoutineSlot:
    start: float
    end: float
    location: str
    activity: str = ""
    goal: str = "Go to location"
    social: bool = False
    target_entity: str = ""
    # 到达 location 后执行的内容动作（如 sleep）；设置后 goal 视为 "Perform action"
    action: str = ""
    # 目标需要的物品类别（Use item on / Use item）
    item_kind: str = ""
    # 每天该槽位目标完成一次后不再重复
    once: bool = False
    # Use item：手边没有该 kind 时允许自己按配方合成（否则只拿现成的）
    craft: bool = False
    # goal = "Achieve"：要达到的世界状态（条件写法同动作 requires），交给编译 PDDL
    conditions: List[Dict[str, Any]] = field(default_factory=list, hash=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.start}-{self.end}:{self.location}:{self.activity}"


def hour_of_day(
    sim_time: float,
    *,
    day_length: float = DAY_LENGTH_SECONDS,
    hour_offset: float = DAY_HOUR_OFFSET,
) -> float:
    """当前时刻（0–24 小时，可含小数）。"""
    length = max(1.0, float(day_length))
    t = max(0.0, float(sim_time)) % length
    return ((t / length) * 24.0 + float(hour_offset)) % 24.0


def format_clock(
    sim_time: float,
    *,
    day_length: float = DAY_LENGTH_SECONDS,
    hour_offset: float = DAY_HOUR_OFFSET,
) -> str:
    h = hour_of_day(sim_time, day_length=day_length, hour_offset=hour_offset)
    hour = int(h) % 24
    minute = int((h - int(h)) * 60)
    absolute_hours = max(0.0, float(sim_time)) / max(1.0, float(day_length)) * 24.0 + float(
        hour_offset
    )
    day = int(absolute_hours // 24.0) + 1
    phase = phase_label(day_phase(h))
    sky = "Night" if is_night(h) else "Day"
    return f"Day {day} {hour:02d}:{minute:02d} ({phase}/{sky})"


def describe_routine_context(
    slot: Optional[RoutineSlot],
    sim_time: float,
    *,
    day_length: float = DAY_LENGTH_SECONDS,
) -> str:
    clock = format_clock(sim_time, day_length=day_length)
    hour = hour_of_day(sim_time, day_length=day_length)
    lines = [f"Time: {clock}."]
    if is_meal_time(hour):
        lines.append("It is mealtime.")
    if is_night(hour):
        lines.append("Night has fallen — most people rest unless something urgent needs them.")
    if not slot:
        lines.append("No personal routine for this hour.")
    else:
        social = "yes" if slot.social else "no"
        act = slot.activity or "(no activity label)"
        lines.append(
            f"Current routine: {act} at {slot.location} "
            f"(hours {slot.start:g}-{slot.end:g}, social={social})."
        )
        lines.append(
            "Prefer following your work/meal/rest routine unless something in the world needs you, "
            "someone is here to talk, or new information starts a personal story."
        )
    return " ".join(lines)


def _in_slot(hour: float, start: float, end: float) -> bool:
    start = float(start) % 24
    end = float(end) % 24
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    # 跨午夜
    return hour >= start or hour < end


def parse_routine(raw: Any) -> List[RoutineSlot]:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    slots: List[RoutineSlot] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        loc = str(item.get("location") or "").strip()
        if not loc:
            continue
        try:
            start = float(item.get("start", 0))
            end = float(item.get("end", 24))
        except (TypeError, ValueError):
            continue
        goal = str(item.get("goal") or "Go to location").strip() or "Go to location"
        slots.append(
            RoutineSlot(
                start=start,
                end=end,
                location=loc,
                activity=str(item.get("activity") or "").strip(),
                goal=goal,
                social=bool(item.get("social", False)),
                target_entity=str(item.get("target_entity") or "").strip(),
                action=str(item.get("action") or "").strip(),
                item_kind=str(item.get("item_kind") or "").strip().lower(),
                once=bool(item.get("once", False)),
                craft=bool(item.get("craft", False)),
                conditions=[dict(c) for c in item.get("conditions") or [] if isinstance(c, dict)],
            )
        )
    return slots


def load_person_routine(world: "Neo4jWorld", person: str) -> List[RoutineSlot]:
    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})
        RETURN p.routine AS routine
        LIMIT 1
        """,
        n=person,
    )
    if not rows:
        return []
    return parse_routine(rows[0].get("routine"))


def current_slot(
    slots: List[RoutineSlot],
    sim_time: float,
    *,
    day_length: float = DAY_LENGTH_SECONDS,
) -> Optional[RoutineSlot]:
    if not slots:
        return None
    hour = hour_of_day(sim_time, day_length=day_length)
    for slot in slots:
        if _in_slot(hour, slot.start, slot.end):
            return slot
    return slots[0]


def routine_goal_candidate(
    slot: RoutineSlot,
    *,
    character: str,
    current_location: Optional[str] = None,
) -> Dict[str, Any]:
    """把当前日程槽位转成目标候选。"""
    goal = slot.goal or "Go to location"
    target = slot.target_entity or slot.location

    if slot.action:
        return {
            "goal": "Perform action",
            "target_entity": slot.action,
            "at_location": slot.location,
            "reason": f"Routine: {slot.activity or slot.action} at {slot.location}",
            "priority": ROUTINE_GOAL_PRIORITY,
            "confidence": 0.8,
            "source": "routine",
            "routine_key": slot.key,
            "routine_activity": slot.activity,
            "routine_social": slot.social,
        }

    if goal == "Go to location":
        target = slot.location
        reason = f"Routine: go to {slot.location}"
        if slot.activity:
            reason += f" ({slot.activity})"
    elif goal == "Gain information":
        # 未指定对象时先到日程地点，到场后再社交打断
        if not slot.target_entity:
            if current_location == slot.location:
                goal = "Gain information"
                target = None  # 由社交打断补全
            else:
                goal = "Go to location"
                target = slot.location
        reason = f"Routine: {slot.activity or 'social / gather info'}"
    else:
        reason = f"Routine: {slot.activity or goal}"

    out = {
        "goal": goal,
        "target_entity": target,
        "reason": reason,
        "priority": ROUTINE_GOAL_PRIORITY,
        "confidence": 0.8,
        "source": "routine",
        "routine_key": slot.key,
        "routine_activity": slot.activity,
        "routine_social": slot.social,
    }
    if slot.item_kind:
        out["item_kind"] = slot.item_kind
        if goal == "Use item" and not slot.target_entity:
            out["target_entity"] = f"kind:{slot.item_kind}"
    if slot.craft:
        out["craft"] = True
    if slot.conditions:
        out["conditions"] = [dict(c) for c in slot.conditions]
    return out


def day_index(sim_time: float, *, day_length: float = DAY_LENGTH_SECONDS) -> int:
    absolute_hours = max(0.0, float(sim_time)) / max(1.0, float(day_length)) * 24.0 + DAY_HOUR_OFFSET
    return int(absolute_hours // 24.0) + 1


def clock_info(sim_time: float, *, day_length: float = DAY_LENGTH_SECONDS) -> Dict[str, Any]:
    h = hour_of_day(sim_time, day_length=day_length)
    phase = day_phase(h)
    return {
        "text": format_clock(sim_time, day_length=day_length),
        "day": day_index(sim_time, day_length=day_length),
        "hour": round(h, 3),
        "phase": phase,
        "phase_label": phase_label(phase),
        "is_night": is_night(h),
        "day_length_seconds": day_length,
    }


def people_at_location(
    world: "Neo4jWorld",
    location: str,
    *,
    exclude: Optional[str] = None,
) -> List[str]:
    from content import get_content

    rows = world.run_query(
        """
        MATCH (p:Person)-[:LOCATED_AT]->(l:Location {name: $loc})
        WHERE coalesce(p.dead, false) = false AND coalesce(p.hp, 0) > 0
          AND NOT coalesce(p.state, '') IN $hidden
        RETURN p.name AS name
        """,
        loc=location,
        hidden=get_content().hidden_states(),
    )
    names = [str(r["name"]) for r in (rows or []) if r.get("name")]
    if exclude:
        names = [n for n in names if n != exclude]
    return names


def pick_social_target(
    world: "Neo4jWorld",
    character: str,
    location: Optional[str],
    *,
    recent_talk_targets: Optional[List[str]] = None,
) -> Optional[str]:
    if not location:
        return None
    others = people_at_location(world, location, exclude=character)
    if not others:
        return None
    recent = set(recent_talk_targets or [])
    for name in others:
        if name not in recent:
            return name
    return others[0]


def should_social_interrupt(
    *,
    slot: Optional[RoutineSlot],
    current_goal: Optional[Dict[str, Any]],
    social_target: Optional[str],
    last_social_at: float,
    sim_time: float,
    cooldown: float = SOCIAL_INTERRUPT_COOLDOWN,
) -> bool:
    if not social_target:
        return False
    if sim_time < last_social_at + cooldown:
        return False
    # 日程允许社交，或当前已在日程地点且槽位标了 social
    if slot is not None and not slot.social:
        return False
    goal = (current_goal or {}).get("goal") or ""
    target = (current_goal or {}).get("target_entity")
    # 已经在对某人打听则不打断
    if goal == "Gain information" and target == social_target:
        return False
    return True


def social_interrupt_goal(social_target: str, *, activity: str = "") -> Dict[str, Any]:
    reason = f"Met {social_target}; pausing routine to talk"
    if activity:
        reason = f"During routine '{activity}', met {social_target}; switching to talk"
    return {
        "goal": "Gain information",
        "target_entity": social_target,
        "reason": reason,
        "priority": SOCIAL_GOAL_PRIORITY,
        "confidence": 0.85,
        "source": "social_interrupt",
    }
