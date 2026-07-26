"""profile_planner 单元测试（无需 Neo4j 的部分）。"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AITOWN3 = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for sub in ("agent",):
    p = os.path.join(_AITOWN3, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_profile import AgentProfile  # noqa: E402
from profile_planner import _goal_priorities  # noqa: E402


class _FakeWorld:
    def list_entities(self, label):
        return []


def main() -> int:
    ok = True

    explorer = AgentProfile(
        _FakeWorld(),  # type: ignore[arg-type]
        "Alice",
        parent_goal="explore every unknown location and uncover mysteries",
        personality="curious explorer",
        values=["curiosity", "honor"],
    )
    pri = _goal_priorities(explorer)
    ok &= pri[0] == "explore"
    print(f"  [{'OK' if pri[0] == 'explore' else 'FAIL'}] explorer prioritizes explore: {pri}")

    merchant = AgentProfile(
        _FakeWorld(),  # type: ignore[arg-type]
        "Merchant",
        parent_goal="collect valuable items and grow wealth",
        personality="greedy collector",
    )
    pri2 = _goal_priorities(merchant)
    ok &= pri2[0] == "collect"
    print(f"  [{'OK' if pri2[0] == 'collect' else 'FAIL'}] merchant prioritizes collect: {pri2}")

    guard = AgentProfile(
        _FakeWorld(),  # type: ignore[arg-type]
        "Guard",
        parent_goal="defeat any threat to the castle",
        personality="aggressive warrior",
    )
    pri3 = _goal_priorities(guard)
    ok &= pri3[0] == "combat"
    print(f"  [{'OK' if pri3[0] == 'combat' else 'FAIL'}] guard prioritizes combat: {pri3}")

    print("\nALL PASS" if ok else "\nSOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
