"""
动作耗时 — 仿真时钟下每个动作占用的 sim seconds，由动作自身声明（内容包 "duration"）。

travel 能力的动作：两个地点都有大地图坐标（map_x / map_y）时，耗时 × 地图距离（至少 1）。
"""

from __future__ import annotations

from typing import Any, List, Optional

DEFAULT_DURATION = 1.0


def get_action_duration(
    action_name: str,
    *,
    instant: bool = False,
    args: Optional[List[Any]] = None,
    world: Any = None,
) -> float:
    """返回动作耗时；instant 模式（兼容旧回合）为 0。"""
    if instant:
        return 0.0
    from registry import get_registry

    action = get_registry().get(action_name)
    base = float(getattr(action, "duration", DEFAULT_DURATION) or DEFAULT_DURATION)
    if world is None or not args or getattr(action, "capability", None) != "travel" or len(args) < 3:
        return base
    try:
        from spatial import map_distance

        dist = map_distance(world, str(args[1]), str(args[2]))
    except Exception:
        dist = None
    return base * max(1.0, dist) if dist else base
