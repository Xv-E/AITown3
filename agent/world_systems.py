"""
世界系统 — 昼夜时钟 + 通用物品脚本 tick（无玩法特判）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld


def tick_world_systems(world: "Neo4jWorld", dt: float, sim_time: float) -> Dict[str, Any]:
    from item_scripts import tick_item_scripts
    from routine import day_phase, format_clock, hour_of_day, is_night

    report: Dict[str, Any] = {"events": []}
    hour = hour_of_day(sim_time)
    phase = day_phase(hour)
    night = is_night(hour)
    _set_world_clock(world, sim_time=sim_time, hour=hour, phase=phase, night=night)

    scripts = tick_item_scripts(world, float(dt))
    report["scripts"] = scripts
    report["events"].extend(scripts.get("events") or [])
    report["alerts"] = scripts.get("alerts") or []
    report["respawned"] = scripts.get("spawned") or []
    report["clock"] = format_clock(sim_time)
    report["phase"] = phase
    report["night"] = night
    return report


def get_campfire_status(world: "Neo4jWorld") -> Optional[Dict[str, Any]]:
    """item_interact 的离线 mock 用：任意带 fuel 的物品状态。"""
    return _legacy_fuel_snapshot(world)


def urgent_alert_goal(
    world: "Neo4jWorld",
    character: str,
    *,
    sim_time: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """该角色分内的紧急物品告警 → 目标（由 script.alerts 与 alert.audience 决定）。"""
    from item_scripts import active_script_alerts, alert_applies_to, alert_to_goal

    for alert in active_script_alerts(world):
        if alert.get("signal") not in ("critical", "urgent"):
            continue
        if not alert_applies_to(world, character, alert, sim_time=sim_time):
            continue
        goal = alert_to_goal(world, character, alert)
        if goal:
            return goal
    return None


def _legacy_fuel_snapshot(world: "Neo4jWorld") -> Optional[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (i:Item)
        WHERE i.fuel IS NOT NULL
        RETURN i.name AS name,
               coalesce(i.fuel, 0) AS fuel,
               coalesce(i.lit, false) AS lit
        ORDER BY name
        LIMIT 1
        """
    )
    if not rows:
        return None
    r = rows[0]
    fuel = float(r.get("fuel") or 0)
    lit = bool(r.get("lit")) and fuel > 0
    return {
        "name": r.get("name"),
        "fuel": fuel,
        "lit": lit,
        "low": fuel > 0 and fuel < 18,
        "out": fuel <= 0 or not lit,
        "fires": [{"name": r.get("name"), "fuel": fuel, "lit": lit}],
    }


def _set_world_clock(world, *, sim_time: float, hour: float, phase: str, night: bool) -> None:
    try:
        world.run_query(
            """
            MERGE (m:WorldMeta {name: 'WorldMeta'})
            SET m.sim_time = $t,
                m.hour_of_day = $h,
                m.day_phase = $phase,
                m.is_night = $night
            """,
            t=float(sim_time),
            h=float(hour),
            phase=phase,
            night=bool(night),
        )
    except Exception:
        pass
