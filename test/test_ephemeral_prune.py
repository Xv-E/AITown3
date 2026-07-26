"""临时 action 候选剪枝测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from item_interact import prune_ephemeral_candidates, _score_ephemeral_action  # noqa: E402


def _cand(item: str, aid: str, label: str, hints=None):
    hints = hints or {}
    pri, tags = _score_ephemeral_action(aid=aid, label=label, item=item, need_hints=hints)
    return {
        "target_entity": item,
        "interact_action": aid,
        "priority": pri,
        "metadata": {"action_label": label, "prune_tags": tags},
    }


def test_drop_examine_when_util_exists():
    rows = [
        _cand("OakTree", "chop", "Chop for wood", {"want_wood": True}),
        _cand("OakTree", "examine", "Examine", {"want_wood": True}),
        _cand("StoneDial", "turn_clockwise", "Turn", {}),
        _cand("StoneDial", "examine", "Look", {}),
    ]
    kept = prune_ephemeral_candidates(rows, max_per_item=2, max_total=4)
    ids = {(c["target_entity"], c["interact_action"]) for c in kept}
    assert ("OakTree", "chop") in ids
    assert ("OakTree", "examine") not in ids
    assert ("StoneDial", "turn_clockwise") in ids
    assert ("StoneDial", "examine") not in ids


def test_max_total():
    rows = [
        _cand("A", "chop", "c", {"want_wood": True}),
        _cand("B", "turn_clockwise", "t", {}),
        _cand("C", "gather", "g", {}),
        _cand("D", "unlock", "u", {}),
        _cand("E", "press", "p", {}),
    ]
    kept = prune_ephemeral_candidates(rows, max_per_item=2, max_total=3)
    assert len(kept) == 3


if __name__ == "__main__":
    test_drop_examine_when_util_exists()
    test_max_total()
    print("ok")
