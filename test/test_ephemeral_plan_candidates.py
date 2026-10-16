"""临时 action 进入规划候选：goal 带 interact_action → 计划绑定具体 id。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "actions"))

from ephemeral_action_dsl import EphemeralAction  # noqa: E402
from goal_planner import _plan_interact_with  # noqa: E402
from item_interact import ephemeral_action_goal_candidates  # noqa: E402
import item_interact as ii  # noqa: E402
import goal_planner as gp  # noqa: E402


def test_candidates_shape_mock(monkeypatch):
    world = MagicMock()
    world.list_entities.return_value = ["OakTree"]

    monkeypatch.setattr(ii, "is_interactive_item", lambda w, n: n == "OakTree")
    monkeypatch.setattr(ii, "_item_loc", lambda w, n: "Forest")
    acts = [
        EphemeralAction(
            id="chop", label="Chop", source_code="x", cypher="RETURN 1", host_item="OakTree"
        ),
        EphemeralAction(
            id="examine",
            label="Examine",
            source_code="y",
            cypher="RETURN 1",
            host_item="OakTree",
        ),
    ]
    monkeypatch.setattr(ii, "ensure_ephemeral_actions", lambda *a, **k: acts)

    # get_person_location is imported inside the function
    import action as action_mod

    monkeypatch.setattr(action_mod, "get_person_location", lambda w, n: "Forest")

    cands = ephemeral_action_goal_candidates(world, "Rowan", force_mock=True)
    ids = {(c["target_entity"], c["interact_action"]) for c in cands}
    assert ("OakTree", "chop") in ids
    # examine 在有 chop 时会被剪枝掉
    assert ("OakTree", "examine") not in ids
    assert all(c["goal"] == "Interact with" for c in cands)
    assert all(c["source"] == "ephemeral_action" for c in cands)

    # 关闭剪枝应保留 examine
    raw = ephemeral_action_goal_candidates(world, "Rowan", force_mock=True, prune=False)
    raw_ids = {(c["target_entity"], c["interact_action"]) for c in raw}
    assert ("OakTree", "examine") in raw_ids


def test_plan_binds_action_id(monkeypatch):
    world = MagicMock()
    world.run_query.return_value = [{"loc": "Forest"}]

    monkeypatch.setattr(ii, "is_interactive_item", lambda w, n: True)
    monkeypatch.setattr(ii, "interaction_success", lambda w, n: False)
    monkeypatch.setattr(ii, "ensure_ephemeral_actions", lambda *a, **k: [
        EphemeralAction(id="chop", label="Chop", source_code="", cypher="RETURN 1")
    ])
    monkeypatch.setattr(ii, "FORCE_MOCK", True)

    monkeypatch.setattr(gp, "_get_entity_location", lambda w, label, name: "Forest")
    monkeypatch.setattr(gp, "_get_person_location", lambda w, name: "Forest")

    result = _plan_interact_with(world, "Rowan", "OakTree", interact_action="chop")
    assert result.success
    assert result.plan == [("interact", ["Rowan", "OakTree", "chop"])]
    assert "chop" in result.engine


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
