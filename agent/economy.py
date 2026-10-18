"""
经济：价格随供需变，商店的货得自己做出来。

价格（每个游戏小时更新一次，写在物品的 price 上；buy / sell 按 price 结算）：
    price = value × 稀缺 × 需求
    稀缺 = (基准数量 + 2) / (现有数量 + 2)    基准 = 第一次看到这类东西时世上有几件
    需求 = 1 + 0.15 × 最近的需求（有人买、商店缺货都会加；每小时衰减 30%）
  夹在 0.5–3 倍之间，至少 1 个钱。全记在 (:Market {name: 'Market'}) 上，导演选项卡能看到。

商店：商人（trader）的 sells 列出卖哪几类东西、每类备几件（shop_stock，默认 1）。
缺货就成了他自己的目标（Collect item kind:<类别>）—— 规划器会让他去采集、制造或者收购；
手上有了就自动上架（for_sale = true）。

钱从不凭空产生：只在买卖时从一个人转到另一个人。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

ECONOMY_EVERY = 12.5     # 模拟秒（= 一个游戏小时）
MIN_FACTOR, MAX_FACTOR = 0.5, 3.0
DEMAND_STEP = 0.15
DEMAND_DECAY = 0.7
SHORTAGE_DEMAND = 1.0    # 商店某类缺货：每小时需求 +1
SHOP_PRIORITY = 0.78


def _market(world) -> Dict[str, Any]:
    rows = world.run_query(
        "MERGE (m:Market {name: 'Market'}) RETURN coalesce(m.state, '{}') AS s"
    ) or []
    try:
        return json.loads(rows[0]["s"]) if rows else {}
    except (TypeError, ValueError):
        return {}


def _save_market(world, state: Dict[str, Any]) -> None:
    world.run_query("MERGE (m:Market {name: 'Market'}) SET m.state = $s", s=json.dumps(state))


def add_demand(world, kind: str, amount: float = 1.0) -> None:
    kind = (kind or "").strip().lower()
    if not kind:
        return
    state = _market(world)
    entry = state.setdefault(kind, {})
    entry["demand"] = round(float(entry.get("demand") or 0) + amount, 3)
    _save_market(world, state)


def factor(base: float, supply: float, demand: float) -> float:
    scarcity = (base + 2.0) / (supply + 2.0)
    return max(MIN_FACTOR, min(MAX_FACTOR, scarcity * (1.0 + DEMAND_STEP * demand)))


def update_prices(world) -> Dict[str, Any]:
    """每类东西：数一数现有多少、算出价格，写到每件物品的 price 上。"""
    rows = world.run_query(
        """
        MATCH (i:Item) WHERE coalesce(i.value, 0) > 0 AND coalesce(i.item_kind, '') <> ''
        RETURN toLower(i.item_kind) AS kind, count(i) AS supply, avg(toFloat(i.value)) AS value
        """
    ) or []
    state = _market(world)
    for r in rows:
        kind = r["kind"]
        entry = state.setdefault(kind, {})
        entry.setdefault("base", int(r["supply"]))
        demand = float(entry.get("demand") or 0)
        f = factor(float(entry["base"]), float(r["supply"]), demand)
        entry["prev"] = entry.get("price")
        entry["price"] = max(1, round(float(r["value"] or 1) * f))
        entry["supply"] = int(r["supply"])
        entry["factor"] = round(f, 2)
        entry["demand"] = round(demand * DEMAND_DECAY, 3)
        world.run_query(
            """
            MATCH (i:Item) WHERE toLower(coalesce(i.item_kind, '')) = $k AND coalesce(i.value, 0) > 0
            SET i.price = CASE WHEN round(toFloat(i.value) * $f) < 1 THEN 1
                               ELSE toInteger(round(toFloat(i.value) * $f)) END
            """,
            k=kind, f=f,
        )
    for kind, entry in state.items():
        if kind not in {r["kind"] for r in rows}:
            entry["supply"] = 0  # 世上一件都没有了：价格保留，下次出现时按缺货算
    _save_market(world, state)
    return state


def market_view(world) -> List[Dict[str, Any]]:
    """导演选项卡：每类东西的价格、比平时贵多少、世上有几件、需求。"""
    out = []
    for kind, e in sorted(_market(world).items()):
        if e.get("price") is None:
            continue
        prev = e.get("prev")
        trend = "" if prev in (None, e["price"]) else ("up" if e["price"] > prev else "down")
        out.append({"kind": kind, "price": e["price"], "factor": e.get("factor", 1.0), "supply": e.get("supply", 0),
                    "demand": round(float(e.get("demand") or 0), 1), "trend": trend})
    return out


# ----- 商店 -----

def shop_of(world, trader: str) -> Dict[str, Any]:
    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})
        RETURN coalesce(p.trader, false) AS trader, coalesce(p.sells, []) AS sells,
               coalesce(p.shop_stock, 1) AS stock
        """,
        n=trader,
    ) or []
    if not rows or not rows[0]["trader"]:
        return {}
    sells = rows[0]["sells"]
    if isinstance(sells, str):
        try:
            sells = json.loads(sells)
        except ValueError:
            sells = [s.strip() for s in sells.split(",")]
    return {"sells": [str(s).strip().lower() for s in sells or [] if str(s).strip()], "stock": int(rows[0]["stock"] or 1)}


def shop_shortages(world, trader: str) -> List[str]:
    """商店还缺的类别（手上这类东西少于要备的件数）。"""
    shop = shop_of(world, trader)
    if not shop.get("sells"):
        return []
    held = world.run_query(
        """
        MATCH (:Person {name: $n})-[:HAS_ITEM]->(i:Item)
        RETURN toLower(coalesce(i.item_kind, '')) AS kind, count(i) AS n
        """,
        n=trader,
    ) or []
    have = {r["kind"]: int(r["n"]) for r in held}
    return [k for k in shop["sells"] if have.get(k, 0) < shop["stock"]]


def restock_candidates(world, trader: str) -> List[Dict[str, Any]]:
    """缺货 → 商人自己的目标：弄一件来（采集、制造、收购都行，交给规划器）。"""
    return [
        {
            "goal": "Collect item",
            "target_entity": f"kind:{kind}",
            "reason": f"The shop has run out of {kind}; get or make one to sell",
            "priority": SHOP_PRIORITY,
            "source": "shop",
        }
        for kind in shop_shortages(world, trader)
    ]


def list_goods(world, trader: str) -> List[str]:
    """商人手上属于他卖的那几类、还没上架的东西：上架。返回刚上架的。"""
    shop = shop_of(world, trader)
    if not shop.get("sells"):
        return []
    rows = world.run_query(
        """
        MATCH (:Person {name: $n})-[:HAS_ITEM]->(i:Item)
        WHERE toLower(coalesce(i.item_kind, '')) IN $kinds AND NOT coalesce(i.for_sale, false)
        SET i.for_sale = true
        RETURN i.name AS name
        """,
        n=trader, kinds=shop["sells"],
    ) or []
    return [r["name"] for r in rows]


def traders(world) -> List[str]:
    rows = world.run_query("MATCH (p:Person) WHERE coalesce(p.trader, false) RETURN p.name AS n ORDER BY n") or []
    return [r["n"] for r in rows]


def economy_step(world) -> Dict[str, Any]:
    """每个游戏小时：商店缺货记成需求、上架做好的东西、更新价格。"""
    listed: Dict[str, List[str]] = {}
    for t in traders(world):
        for kind in shop_shortages(world, t):
            add_demand(world, kind, SHORTAGE_DEMAND)
        new = list_goods(world, t)
        if new:
            listed[t] = new
    update_prices(world)
    return {"listed": listed}


def kind_of(world, item: str) -> str:
    rows = world.run_query("MATCH (i:Item {name: $n}) RETURN toLower(coalesce(i.item_kind, '')) AS k", n=item) or []
    return rows[0]["k"] if rows else ""
