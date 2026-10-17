"""
动作耗时 — 仿真时钟下每个动作占用的 sim seconds，由动作自身声明（内容包 "duration"）。

travel 能力的动作：两个地点都有大地图坐标（map_x / map_y）时，耗时 × 地图距离（至少 1）。
区域内走路另算：先走到目标旁边（或出口）再做动作，走路时间按格子距离加在前面（见 action_walk）。
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

DEFAULT_DURATION = 1.0


def _action(action_name: str) -> Any:
    from registry import get_registry

    return get_registry().get(action_name)


def is_travel(action_name: str) -> bool:
    return getattr(_action(action_name), "capability", None) == "travel"


def get_action_duration(
    action_name: str,
    *,
    instant: bool = False,
    args: Optional[List[Any]] = None,
    world: Any = None,
    walk: float = 0.0,
) -> float:
    """返回动作耗时（含 walk 秒的区域内走路）；instant 模式（兼容旧回合）为 0。"""
    if instant:
        return 0.0
    action = _action(action_name)
    base = float(getattr(action, "duration", DEFAULT_DURATION) or DEFAULT_DURATION)
    if world is not None and args and is_travel(action_name) and len(args) >= 3:
        try:
            from spatial import map_distance

            dist = map_distance(world, str(args[1]), str(args[2]))
        except Exception:
            dist = None
        if dist:
            base *= max(1.0, dist)
    return base + max(0.0, walk)


def action_walk(
    world: Any, actor: str, action_name: str, args: List[Any]
) -> Tuple[float, Optional[Tuple[str, Tuple[int, int]]]]:
    """(走路秒数, 走完停在哪)；算不出来就当不用走。"""
    if world is None:
        return 0.0, None
    try:
        from spatial import plan_walk

        return plan_walk(world, actor, list(args), travel=is_travel(action_name))
    except Exception:
        return 0.0, None
