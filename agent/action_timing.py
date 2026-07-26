"""
动作耗时表 — 仿真时钟下每个动作占用的 sim seconds。
"""

from __future__ import annotations

from typing import Dict

# 默认动作时长（仿真秒）
ACTION_DURATIONS: Dict[str, float] = {
    "move": 1.0,
    "pick_up": 1.0,
    "drop": 1.0,
    "give": 1.0,
    "use_item": 1.0,
    "use_on": 1.5,
    "interact": 3.0,
    "attack": 1.5,
    "talk": 4.0,
    "wait": 1.0,
    "noop": 1.0,
}

DEFAULT_DURATION = 1.0


def get_action_duration(action_name: str, *, instant: bool = False) -> float:
    """返回动作耗时；instant 模式（兼容旧回合）为 0。"""
    if instant:
        return 0.0
    return float(ACTION_DURATIONS.get(action_name, DEFAULT_DURATION))
