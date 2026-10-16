"""
任务线接口（导演第二版）。server.py 只负责 include_router；世界和剧情走向通过参数注入，
这里不 import server，避免循环依赖。

    GET  /api/questlines          现有任务线和进度
    POST /api/questlines/draft    LLM 写一条并验证（不写数据库）
    POST /api/questlines/apply    把草稿写进世界（写之前按当前世界再验证一次）
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


class DraftRequest(BaseModel):
    theme: str = ""


class ApplyRequest(BaseModel):
    # 不给就用最近一次草稿
    patch: Optional[Dict[str, Any]] = None


def make_router(get_world: Callable[[], Any], story_arc: Callable[[], str]) -> APIRouter:
    router = APIRouter(prefix="/api/questlines", tags=["questlines"])
    last: Dict[str, Any] = {}
    lock = threading.Lock()  # 同时只写一条，免得两次 apply 撞名

    @router.get("")
    def list_questlines():
        from quest import active_questlines

        return {"questlines": active_questlines(get_world())}

    @router.post("/draft")
    async def draft(req: DraftRequest):
        from quest import draft_questline

        world = get_world()
        result = await asyncio.to_thread(draft_questline, world, req.theme, story_arc=story_arc())
        body = _result_body(result)
        if result.ok:
            last["patch"] = result.patch.to_dict()
        return body

    @router.post("/apply")
    async def apply(req: ApplyRequest):
        patch = req.patch or last.get("patch")
        if not patch:
            raise HTTPException(status_code=400, detail="No questline to apply; draft one first")
        world = get_world()

        def work():
            with lock:
                return _verify_and_apply(world, patch)

        out = await asyncio.to_thread(work)
        if not out.get("ok"):
            raise HTTPException(status_code=409, detail=out)
        last.pop("patch", None)
        return out

    return router


def _result_body(result) -> Dict[str, Any]:
    v = result.verification
    return {
        "ok": result.ok,
        "attempts": result.attempts,
        "problems": result.problems,
        "fixes": result.fixes,
        "patch": result.patch.to_dict() if result.patch else None,
        "walkthrough": [[f"{a}({', '.join(args)})" for a, args in stage] for stage in (v.walkthrough if v else [])],
    }


def _verify_and_apply(world, data: Dict[str, Any]) -> Dict[str, Any]:
    """世界可能在草稿之后变了：按现在的世界重新校验、验证，通过才写。"""
    from compiler import load_snapshot
    from quest import QuestlinePatch, apply_questline, read_digest, validate, verify

    patch = QuestlinePatch.from_dict(data)
    digest = read_digest(world)
    problems = validate(patch, digest.known())
    if problems:
        return {"ok": False, "problems": problems}
    v = verify(load_snapshot(world), patch, digest.player)
    if not v.ok:
        return {"ok": False, "problems": [v.reason]}
    return {"ok": True, **apply_questline(world, patch)}
