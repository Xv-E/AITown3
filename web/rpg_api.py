"""
RPG 系统接口：制造面板、交易面板，以及「替我挑好参数再执行」的通用动作入口。

    GET  /api/crafting            玩家能做的配方（缺什么、在不在工作台旁）
    GET  /api/trade/{npc}         和某个商人：他卖什么、你能卖什么、各多少钱
    POST /api/do                  {"action", "target", "other"}：采集 / 制造 / 买 / 卖
                                  （具体用哪几件材料、哪把工具由服务端挑）

server.py 只负责 include_router；世界和「动作做完之后」的回调通过参数注入。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


class DoRequest(BaseModel):
    action: str
    target: str = ""  # 采集点 / 要买卖的物品
    other: str = ""   # 交易对象


def _player(world) -> str:
    rows = world.run_query(
        "MATCH (p:Person) WHERE coalesce(p.is_player, false) OR p.role = 'player' RETURN p.name AS n LIMIT 1"
    ) or []
    if not rows:
        raise HTTPException(status_code=404, detail="No player in this world")
    return rows[0]["n"]


def _here(world, person: str) -> str:
    rows = world.run_query(
        "MATCH (:Person {name: $p})-[:LOCATED_AT]->(l:Location) RETURN l.name AS l LIMIT 1", p=person
    ) or []
    return str(rows[0]["l"]) if rows else ""


def trade_view(world, me: str, npc: str) -> Dict[str, Any]:
    from narration import pretty

    rows = world.run_query(
        """
        MATCH (n:Person {name: $npc}), (m:Person {name: $me})
        RETURN coalesce(n.trader, false) AS trader, coalesce(n.coins, 0) AS npc_coins,
               coalesce(m.coins, 0) AS my_coins
        """,
        npc=npc, me=me,
    ) or []
    if not rows:
        raise HTTPException(status_code=404, detail=f"No such person: {npc}")
    r = rows[0]

    def goods(owner: str, only_for_sale: bool) -> List[Dict[str, Any]]:
        out = []
        for g in world.run_query(
            """
            MATCH (:Person {name: $o})-[:HAS_ITEM]->(i:Item)
            RETURN i.name AS name, coalesce(i.item_kind, '') AS kind, coalesce(i.price, i.value, 0) AS value,
                   coalesce(i.for_sale, false) AS for_sale, coalesce(i.description, '') AS description
            ORDER BY i.name
            """,
            o=owner,
        ) or []:
            if only_for_sale and not g.get("for_sale"):
                continue
            out.append({**g, "label": pretty(g["name"])})
        return out

    my_coins, npc_coins = float(r["my_coins"] or 0), float(r["npc_coins"] or 0)
    sells = [
        {**g, "price": int(g["value"]), "affordable": my_coins >= float(g["value"])}
        for g in goods(npc, True)
    ]
    buys = [
        {**g, "price": int(float(g["value"]) // 2), "affordable": npc_coins >= float(g["value"])}
        for g in goods(me, False) if float(g["value"] or 0) >= 1
    ]
    return {
        "npc": npc,
        "trader": bool(r["trader"]),
        "my_coins": int(my_coins),
        "npc_coins": int(npc_coins),
        "sells": sells,
        "buys": buys,
    }


def make_router(get_world: Callable[[], Any], after_action: Optional[Callable[..., None]] = None) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["rpg"])

    @router.get("/crafting")
    def crafting():
        from rpg_content import crafting_view

        world = get_world()
        me = _player(world)
        return {"player": me, "recipes": crafting_view(world, me)}

    @router.get("/trade/{npc}")
    def trade(npc: str):
        world = get_world()
        return trade_view(world, _player(world), npc)

    @router.get("/requests")
    def requests_list():
        """委托板：谁在求什么、给多少钱、你手上有没有能交的。"""
        from job_board import requests_view

        world = get_world()
        return {"requests": requests_view(world, _player(world))}

    @router.post("/requests/{rid}/deliver")
    def deliver(rid: str):
        """把委托要的东西交给发委托的人（要在他身边）。"""
        from job_board import on_give, open_requests
        from registry import get_registry

        world = get_world()
        me = _player(world)
        req = next((r for r in open_requests(world) if r["id"] == rid), None)
        if req is None:
            return {"success": False, "message": "That request is no longer open."}
        items = world.run_query(
            """
            MATCH (:Person {name: $me})-[:HAS_ITEM]->(i:Item) WHERE toLower(coalesce(i.item_kind, '')) = $k
            RETURN i.name AS n ORDER BY n LIMIT 1
            """,
            me=me, k=req["kind"],
        ) or []
        if not items:
            return {"success": False, "message": f"You don't have any {req['kind']}."}
        ok, reason = get_registry().execute(
            world, "give", giver=me, i=items[0]["n"], receiver=req["poster"], l=_here(world, me)
        )
        if not ok:
            return {"success": False, "message": reason}
        done = on_give(world, me, req["poster"], items[0]["n"])
        return {"success": True, "message": done or f"You give {items[0]['n']} to {req['poster']}."}

    @router.post("/party/{npc}")
    def party(npc: str, join: bool = True):
        """邀请 npc 同行（他对你的好感够、或是盟友才会来）/ 让他离队。"""
        from relations import invite, leave

        world = get_world()
        me = _player(world)
        if join:
            ok, message = invite(world, me, npc)
        else:
            ok, message = True, leave(world, npc)
        return {"success": ok, "message": message}

    @router.post("/ally/{npc}")
    def ally(npc: str):
        """向 npc 提出结盟。"""
        from relations import propose_alliance

        world = get_world()
        ok, message = propose_alliance(world, _player(world), npc)
        return {"success": ok, "message": message}

    @router.post("/do")
    def do(req: DoRequest):
        from content import get_content
        from graph_ops import render_text
        from registry import get_registry
        from rpg_content import bind_params

        world = get_world()
        me = _player(world)
        spec = get_content().actions.get(req.action)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Unknown action: {req.action}")
        if (spec.get("rpg") or {}).get("system") in ("gather", "craft"):
            params, why = bind_params(world, me, spec, target=req.target)
            if params is None:
                return {"success": False, "message": why, "action": req.action}
        elif req.action in ("buy", "sell"):
            if not req.target or not req.other:
                raise HTTPException(status_code=400, detail="buy / sell need target (item) and other (trader)")
            who = "seller" if req.action == "buy" else "buyer"
            params = {"c": me, "i": req.target, who: req.other, "l": _here(world, me)}
        else:
            raise HTTPException(status_code=400, detail=f"{req.action} is not a gathering, crafting or trade action")
        ok, reason = get_registry().execute(world, req.action, **params)
        message = reason
        if ok:
            message = render_text(str(spec.get("narrative") or "Done."), {**params, "actor": me}) or "Done."
            if after_action is not None:
                try:
                    after_action(world, me, req.action, params)
                except Exception as e:  # noqa: BLE001
                    print(f"[WARN] after {req.action}: {e}")
        return {"success": ok, "message": message, "action": req.action, "params": params}

    return router
