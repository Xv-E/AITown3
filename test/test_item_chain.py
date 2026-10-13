"""通用获取链规划（不依赖 Neo4j）：在 WorldView 快照上反推工具 / 合成链。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from item_chain import (  # noqa: E402
    ItemInfo,
    Recipe,
    WorldView,
    describe_recipes,
    plan_obtain,
    plan_use_on,
    plan_use_self,
)


def _town() -> WorldView:
    adj = {
        "TownSquare": ["Market", "Tavern", "Warehouse"],
        "Market": ["TownSquare"],
        "Tavern": ["TownSquare"],
        "Warehouse": ["TownSquare", "Farm"],
        "Farm": ["Warehouse"],
    }
    items = {
        "Stove": ItemInfo("Stove", "stove", False, "Tavern",
                          props={"accepts_from": '["kind:ingredient"]'}),
        "OrderBoard": ItemInfo("OrderBoard", "counter", False, "Tavern",
                               props={"accepts_from": '["kind:coin", "kind:meal"]', "orders": 1}),
        "MarketStall": ItemInfo("MarketStall", "stall", False, "Market",
                                props={"accepts_from": '["kind:coin", "kind:crate"]', "stock": 0}),
        "Vegetable1": ItemInfo("Vegetable1", "ingredient", True, "Farm"),
        "Crate1": ItemInfo("Crate1", "crate", True, "Warehouse"),
        "Coin1": ItemInfo("Coin1", "coin", True, None, holder="Tilda"),
    }
    stock_ok = [{"op": "prop_gte", "entity": "@target", "prop": "stock", "value": 1}]
    recipes = [
        Recipe("Vegetable1", "Stove", produces=["meal"], consumes=True),
        Recipe("Crate1", "MarketStall", consumes=True),
        Recipe("Coin1", "OrderBoard", consumes=True),
        Recipe("Coin1", "MarketStall", preconditions=stock_ok, produces=["food"], consumes=True),
    ]
    return WorldView(
        adjacency=adj,
        items=items,
        person_loc={"Bruno": "Tavern", "Mira": "Market", "Tilda": "Tavern"},
        recipes=recipes,
    )


def test_cook_crafts_meal_then_serves():
    steps, err = plan_use_on(_town(), "Bruno", "OrderBoard", prefer_kind="meal")
    assert steps is not None, err
    names = [s[0] for s in steps]
    assert ("pick_up", ["Bruno", "Vegetable1", "Farm"]) in steps
    assert ("use_on", ["Bruno", "Vegetable1", "Stove", "Tavern", "Tavern"]) in steps
    assert steps[-1] == ("use_on", ["Bruno", "kind:meal", "OrderBoard", "Tavern", "Tavern"])
    assert names.index("pick_up") < names.index("use_on")


def test_merchant_restocks_from_warehouse():
    steps, err = plan_use_on(_town(), "Mira", "MarketStall", prefer_kind="crate")
    assert steps is not None, err
    assert steps[0] == ("move", ["Mira", "Market", "TownSquare"])
    assert ("pick_up", ["Mira", "Crate1", "Warehouse"]) in steps
    assert steps[-1] == ("use_on", ["Mira", "Crate1", "MarketStall", "Market", "Market"])


def test_customer_uses_held_coin_without_walking():
    steps, err = plan_use_on(_town(), "Tilda", "OrderBoard", prefer_kind="coin")
    assert steps == [("use_on", ["Tilda", "Coin1", "OrderBoard", "Tavern", "Tavern"])], err


def test_preconditions_block_sold_out_stall():
    steps, err = plan_use_on(_town(), "Tilda", "MarketStall", prefer_kind="coin")
    assert steps is None and err


def test_obtain_kind_by_crafting_and_use_self_needs_holding():
    steps, err = plan_obtain(_town(), "Bruno", "kind:meal")
    assert steps is not None, err
    assert steps[-1][0] == "use_on" and steps[-1][1][2] == "Stove"
    steps, _ = plan_use_self(_town(), "Tilda", "kind:meal")
    assert steps is None


def test_use_self_can_craft_when_allowed():
    steps, err = plan_use_self(_town(), "Bruno", "kind:meal", craft=True)
    assert steps is not None, err
    names = [s[0] for s in steps]
    assert "use_on" in names and steps[-1][1] == ["Bruno", "kind:meal"]


def test_describe_recipes_is_generic():
    lines = describe_recipes(_town())
    assert any("ingredient on Stove" in ln and "meal" in ln for ln in lines)


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print("ok")
