"""
决策触发器 — NPC 不再每拍都想，只在「有事发生」时做一次决策。

两类来源：
  routine  日程换段（起床 / 上班 / 吃饭 / 回家）
  event    突发事件：需求信号出现、有人来到身边、收到东西、受伤、计划失败、闲太久

检测只比较「上次看到的世界」与「现在的世界」，不含任何具体玩法。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

# 打断进行中计划所需的最低优先级
INTERRUPT_PRIORITY = 0.8
# 睡着时只有这个优先级以上的事件会叫醒
WAKE_PRIORITY = 1.0
SCAN_INTERVAL = 1.0
MAX_QUEUE = 6

ALERT_PRIORITY = {"critical": 1.0, "urgent": 0.85, "info": 0.5}


@dataclass
class Trigger:
    source: str  # routine | event
    kind: str  # routine_slot | alert | person_arrived | received_item | hurt | plan_failed | idle | start
    text: str
    priority: float = 0.5
    at: float = 0.0
    data: Dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.kind}|{self.text}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind,
            "text": self.text,
            "priority": self.priority,
            "at": self.at,
        }


@dataclass
class Observation:
    """上次扫描时看到的世界（用于做差分）。"""

    slot: Optional[str] = None
    location: Optional[str] = None
    alerts: frozenset = frozenset()
    people: frozenset = frozenset()
    inventory: frozenset = frozenset()
    hp: Optional[int] = None


def held_items(world: "Neo4jWorld", person: str) -> frozenset:
    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:HAS_ITEM]->(i:Item) RETURN i.name AS n", n=person
    ) or []
    return frozenset(r["n"] for r in rows if r.get("n"))


def observe(
    world: "Neo4jWorld",
    character: str,
    *,
    location: Optional[str],
    hp: Optional[int],
    sim_time: float,
    alerts: List[Dict[str, Any]],
) -> tuple:
    """返回 (Observation, {alert_key: alert}, slot)。"""
    from item_scripts import alert_applies_to
    from routine import current_slot, load_person_routine, people_around

    slots = load_person_routine(world, character)
    slot = current_slot(slots, sim_time) if slots else None
    mine: Dict[str, Dict[str, Any]] = {}
    for a in alerts:
        if alert_applies_to(world, character, a, sim_time=sim_time):
            mine[f"{a.get('item')}|{a.get('signal')}|{a.get('message')}"] = a
    people = frozenset(people_around(world, character, location))
    obs = Observation(
        slot=slot.key if slot else None,
        location=location,
        alerts=frozenset(mine),
        people=people,
        inventory=held_items(world, character),
        hp=hp,
    )
    return obs, mine, slot


def diff_triggers(
    prev: Optional[Observation],
    cur: Observation,
    *,
    alerts: Dict[str, Dict[str, Any]],
    slot: Any,
    clock: str,
    sim_time: float,
) -> List[Trigger]:
    if prev is None:
        return [Trigger("event", "start", f"{clock}: the day is underway; decide what to do.",
                        0.6, sim_time)]
    out: List[Trigger] = []
    if cur.slot != prev.slot and slot is not None:
        what = slot.activity or slot.goal
        out.append(Trigger(
            "routine", "routine_slot",
            f"{clock}: your routine now says '{what}' at {slot.location}.",
            0.6, sim_time, {"slot": cur.slot},
        ))
    for key in sorted(cur.alerts - prev.alerts):
        a = alerts.get(key) or {}
        out.append(Trigger(
            "event", "alert",
            f"{a.get('item')}: {a.get('message') or 'needs attention'}",
            ALERT_PRIORITY.get(str(a.get("signal") or "info"), 0.5), sim_time,
            {"item": a.get("item"), "signal": a.get("signal")},
        ))
    if cur.location == prev.location:
        for name in sorted(cur.people - prev.people):
            out.append(Trigger("event", "person_arrived", f"{name} came over to you at {cur.location}.",
                               0.5, sim_time, {"person": name}))
    for item in sorted(cur.inventory - prev.inventory):
        out.append(Trigger("event", "received_item", f"You received {item}.",
                           0.7, sim_time, {"item": item}))
    if prev.hp is not None and cur.hp is not None and cur.hp < prev.hp:
        out.append(Trigger("event", "hurt", f"You lost {prev.hp - cur.hp} HP (now {cur.hp}).",
                           0.95, sim_time))
    return out


def describe_triggers(triggers: List[Trigger]) -> str:
    if not triggers:
        return ""
    lines = [f"- [{t.source}] {t.text}" for t in sorted(triggers, key=lambda t: -t.priority)]
    return "\n".join(lines)
