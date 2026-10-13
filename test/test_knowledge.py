"""人物知识（不依赖 Neo4j）：信念差分、按信念过滤规划视图 / 快照、对话传递。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("agent", "planner", "actions"):
    sys.path.insert(0, str(ROOT / sub))

from compiler import PlanningConfig, Snapshot, compile_pddl  # noqa: E402
from item_chain import ItemInfo, WorldView, plan_obtain  # noqa: E402
from knowledge import (  # noqa: E402
    Knowledge,
    describe_sighting,
    diff_sighting,
    filter_snapshot,
    filter_view,
    mentioned_beliefs,
)


def _view() -> WorldView:
    return WorldView(
        adjacency={
            "Village": ["Forest", "Hills"], "Forest": ["Village"],
            "Hills": ["Village", "Mine"], "Mine": ["Hills"],
        },
        items={
            "Log1": ItemInfo("Log1", "log", True, "Forest"),
            "Log2": ItemInfo("Log2", "log", True, "Hills"),
            "Stone1": ItemInfo("Stone1", "stone", True, None, holder="Kai"),
        },
        person_loc={"Kai": "Village", "Oren": "Village"},
    )


def test_unknown_items_are_invisible_to_the_planner():
    k = Knowledge(places={"Village", "Forest"})
    view = filter_view(_view(), "Kai", k)
    assert set(view.items) == {"Stone1"}
    # 当前地点的出口总是看得见；更远的没去过、没听说过就不知道
    assert view.adjacency["Village"] == ["Forest", "Hills"]
    assert "Mine" not in view.adjacency and view.adjacency["Hills"] == ["Village"]
    steps, err = plan_obtain(view, "Kai", "kind:log")
    assert steps is None and "log" in err


def test_planner_uses_believed_location():
    # 以为 Log1 还在 Village（其实已在 Forest）：计划去 Village 捡，到场才会发现
    k = Knowledge(places={"Village", "Forest"}, items={"Log1": ("Village", None)})
    steps, _ = plan_obtain(filter_view(_view(), "Kai", k), "Kai", "kind:log")
    assert steps and steps[-1][0] == "pick_up" and "Log1" in steps[-1][1]
    assert not any(s[0] == "move" for s in steps)


def test_omniscient_view_is_unchanged():
    view = _view()
    assert filter_view(view, "Kai", Knowledge(omniscient=True)) is view


def test_diff_sighting_only_refutes_what_can_be_checked():
    believed = {
        "Log1": ("Forest", None),     # 以为在这，没看到 → 否定
        "Log2": ("Hills", None),      # 别处的 → 保留
        "Coin": (None, "Oren"),       # Oren 在场但没拿着 → 否定
        "Gem": (None, "Mira"),        # Mira 不在场 → 保留
        "Axe": ("Forest", None),      # 还在 → 不重复写
    }
    seen = {"Axe": None, "Log3": None, "Bread": "Oren"}
    new, gone = diff_sighting("Forest", seen, believed, present={"Oren"})
    assert new == {"Log3": None, "Bread": "Oren"}
    assert gone == ["Coin", "Log1"]
    text = describe_sighting("Forest", new, gone, {"Log3": "log", "Log1": "log"})
    assert "Saw at Forest: Log3 (log)" in text and "Oren holding Bread" in text
    assert "No longer at Forest" in text


def test_dialogue_shares_only_what_is_said_with_a_place():
    k = Knowledge(
        places={"Forest", "Village"},
        items={"Log1": ("Forest", None), "Bread1": (None, "Oren"), "Stone1": ("Village", None)},
        kinds={"Log1": "log", "Bread1": "bread", "Stone1": "stone"},
    )
    said = mentioned_beliefs("There are plenty of logs up in the Forest.", k)
    assert said == {"Log1": ("Forest", None)}
    assert mentioned_beliefs("Oren has some bread.", k) == {"Bread1": (None, "Oren")}
    assert mentioned_beliefs("I like stones.", k) == {}


def _snap() -> Snapshot:
    from test_pddl_compiler import _item

    snap = Snapshot()
    snap.objects.update(
        Village=("Location", {}),
        Forest=("Location", {}),
        Hills=("Location", {}),
        Mine=("Location", {}),
        Kai=("Person", {"hp": 100}),
        Log1=_item("log"),
        Log2=_item("log"),
        Workshop=_item("workbench"),
    )
    snap.edges["ADJACENT_TO"] = {
        ("Village", "Forest"), ("Forest", "Village"), ("Village", "Hills"), ("Hills", "Village"),
        ("Hills", "Mine"), ("Mine", "Hills"),
    }
    snap.location.update(Kai="Village", Log1="Forest", Log2="Mine", Workshop="Village")
    return snap


def test_compiled_pddl_only_sees_known_world():
    k = Knowledge(
        places={"Village", "Hills"},
        items={"Log1": ("Hills", None), "Workshop": ("Village", None)},
    )
    snap = filter_snapshot(_snap(), "Kai", k)
    assert "Mine" not in snap.objects and "Log2" not in snap.objects
    assert snap.location["Log1"] == "Hills"
    assert ("Hills", "Mine") not in snap.edges["ADJACENT_TO"]
    goal = [{"op": "has_item", "person": "@self", "item": "kind:plank"}]
    c = compile_pddl(snap, "Kai", goal, config=PlanningConfig())
    assert "(located-at Log1 Hills)" in c.problem and "Log2" not in c.problem
