"""
委托（任务）：NPC 卡在「缺某样东西」上时挂出委托，谁送来就从他自己的钱里付酬劳。

  (:Request {id, poster, kind, reward, status, reason, created, taker, closed})
    status: open | done | cancelled

- 卡住的人自动发：日程 / 店里要的东西连着几次弄不到，就挂一张「需要 1 件 <类别>，付 N 钱」。
  酬劳 ≈ 市价 × 1.2，但不超过他手上的钱（守恒：钱只是转手）。
- 谁把这类东西交给他（give），委托就完成：他付钱，好感 +；同一人同一类只挂一张。
- 一天没人接、或者他自己弄到了，就撤下。
- LLM 角色在决策 prompt 里看得到别人的委托，可以选目标 Fulfil request（target = 发委托的人）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

EXPIRE_AFTER = 300.0      # 模拟秒（= 一天）
REWARD_MARKUP = 1.2


def _price(world, kind: str) -> int:
    from economy import _market

    entry = _market(world).get(kind) or {}
    if entry.get("price"):
        return int(entry["price"])
    rows = world.run_query(
        "MATCH (i:Item) WHERE toLower(coalesce(i.item_kind, '')) = $k RETURN avg(toFloat(coalesce(i.value, 1))) AS v",
        k=kind,
    ) or []
    return max(1, round(float((rows[0] or {}).get("v") or 3))) if rows else 3


def _coins(world, name: str) -> int:
    rows = world.run_query("MATCH (p:Person {name: $n}) RETURN coalesce(p.coins, 0) AS c", n=name) or []
    return int(float(rows[0]["c"] or 0)) if rows else 0


def open_request(world, poster: str, kind: str) -> Optional[Dict[str, Any]]:
    rows = world.run_query(
        "MATCH (r:Request {poster: $p, kind: $k, status: 'open'}) RETURN properties(r) AS r LIMIT 1",
        p=poster, k=kind,
    ) or []
    return dict(rows[0]["r"]) if rows else None


def post(world, poster: str, kind: str, *, at: float = 0.0, reason: str = "") -> Optional[Dict[str, Any]]:
    """挂一张委托（同一人同一类已经有就不再挂）。返回新挂的（或 None）。"""
    kind = (kind or "").strip().lower()
    if not kind or open_request(world, poster, kind):
        return None
    reward = max(1, min(_coins(world, poster), round(_price(world, kind) * REWARD_MARKUP)))
    if _coins(world, poster) <= 0:
        reward = 0  # 没钱也能求人帮忙，只是没有酬劳
    rid = f"{poster}-{kind}-{int(at)}"
    world.run_query(
        """
        CREATE (:Request {id: $id, poster: $p, kind: $k, reward: $r, status: 'open', reason: $why, created: $at})
        """,
        id=rid, p=poster, k=kind, r=int(reward), why=reason[:200], at=float(at),
    )
    return {"id": rid, "poster": poster, "kind": kind, "reward": reward, "reason": reason}


def open_requests(world, *, exclude: str = "") -> List[Dict[str, Any]]:
    rows = world.run_query(
        "MATCH (r:Request {status: 'open'}) RETURN properties(r) AS r ORDER BY r.created"
    ) or []
    return [dict(r["r"]) for r in rows if dict(r["r"]).get("poster") != exclude]


def recent_requests(world, limit: int = 12) -> List[Dict[str, Any]]:
    rows = world.run_query(
        "MATCH (r:Request) RETURN properties(r) AS r ORDER BY r.created DESC LIMIT $n", n=int(limit)
    ) or []
    return [dict(r["r"]) for r in rows]


def on_give(world, giver: str, receiver: str, item: str, *, at: float = 0.0) -> str:
    """有人把东西交给发委托的人：是他要的那类就完成委托、付酬劳。返回一句话（没完成就是 ""）。"""
    from economy import kind_of

    if not giver or giver == receiver:
        return ""
    kind = kind_of(world, item)
    req = open_request(world, receiver, kind)
    if not req:
        return ""
    pay = max(0, min(int(req.get("reward") or 0), _coins(world, receiver)))
    if pay:
        world.run_query(
            """
            MATCH (a:Person {name: $r}), (b:Person {name: $g})
            SET a.coins = coalesce(a.coins, 0) - $n, b.coins = coalesce(b.coins, 0) + $n
            """,
            r=receiver, g=giver, n=pay,
        )
    world.run_query(
        "MATCH (q:Request {id: $id}) SET q.status = 'done', q.taker = $g, q.closed = $at, q.paid = $n",
        id=req["id"], g=giver, at=float(at), n=pay,
    )
    try:
        from relations import adjust

        adjust(world, receiver, giver, 2, reason=f"brought the {kind} I asked for", at=at)
    except Exception:  # noqa: BLE001
        pass
    return f"{receiver}'s request for {kind} is done; {giver} gets {pay} coins."


def tidy(world, *, now: float) -> List[str]:
    """撤下过期的、和发委托的人自己已经弄到的。返回撤下的 id。"""
    closed = []
    for req in open_requests(world):
        have = world.run_query(
            """
            MATCH (:Person {name: $p})-[:HAS_ITEM]->(i:Item) WHERE toLower(coalesce(i.item_kind, '')) = $k
            RETURN count(i) AS n
            """,
            p=req["poster"], k=req["kind"],
        ) or []
        got = bool(have and int(have[0]["n"] or 0) > 0)
        if got or now - float(req.get("created") or 0) > EXPIRE_AFTER:
            world.run_query(
                "MATCH (q:Request {id: $id}) SET q.status = 'cancelled', q.closed = $at, q.note = $why",
                id=req["id"], at=float(now), why="got it themselves" if got else "nobody came",
            )
            closed.append(req["id"])
    return closed


def requests_view(world, viewer: str = "") -> List[Dict[str, Any]]:
    """给客户端：委托列表；viewer 手上有没有能交的。"""
    from narration import pretty

    held = {}
    if viewer:
        for r in world.run_query(
            """
            MATCH (:Person {name: $v})-[:HAS_ITEM]->(i:Item)
            RETURN toLower(coalesce(i.item_kind, '')) AS k, collect(i.name) AS items
            """,
            v=viewer,
        ) or []:
            held[r["k"]] = r["items"]
    out = []
    for req in recent_requests(world, 20):
        out.append({
            "id": req["id"],
            "poster": req["poster"],
            "kind": req["kind"],
            "reward": int(req.get("reward") or 0),
            "status": req.get("status") or "open",
            "reason": req.get("reason") or "",
            "taker": req.get("taker") or "",
            "note": req.get("note") or "",
            "you_have": [pretty(i) for i in held.get(req["kind"], [])],
            "line": f"{pretty(req['poster'])} needs {req['kind']}"
                    + (f" - pays {int(req.get('reward') or 0)} coins" if req.get("reward") else " - no pay, just a favour"),
        })
    return out


def prompt_text(world, me: str) -> str:
    """决策 prompt：别人在求什么（你能帮的标出来）。"""
    reqs = open_requests(world, exclude=me)
    if not reqs:
        return ""
    lines = []
    for r in reqs[:6]:
        pay = f"pays {r.get('reward')} coins" if r.get("reward") else "no pay"
        lines.append(f"- {r['poster']} needs {r['kind']} ({pay}). Help with goal Fulfil request, target {r['poster']}.")
    return "\n".join(lines)
