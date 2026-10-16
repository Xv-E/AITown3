"""
任务线进度：每拍检查当前阶段的完成条件；完成时发奖励、全营地广播、提醒下一阶段的委托人，推进阶段。

只通过 GameLoop.hooks 接入（on_tick），不改模拟本身。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional


def active_questlines(world) -> List[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (q:Quest) WHERE q.questline = true
        RETURN q.name AS name, q.title AS title, q.description AS premise, q.state AS state,
               coalesce(q.current, 0) AS current, q.stages AS stages
        ORDER BY name
        """
    ) or []
    out = []
    for r in rows:
        try:
            stages = json.loads(r.get("stages") or "[]")
        except json.JSONDecodeError:
            stages = []
        out.append({**r, "stages": stages})
    return out


def _player(world) -> Optional[str]:
    rows = world.run_query(
        "MATCH (p:Person) WHERE coalesce(p.is_player, false) OR p.role = 'player' RETURN p.name AS n LIMIT 1"
    ) or []
    return rows[0]["n"] if rows else None


def check_progress(world, *, announce: Callable[[str], None]) -> List[Dict[str, Any]]:
    """推进所有进行中的任务线；返回这次完成的阶段。announce(text) 负责公告。"""
    from graph_ops import apply_all, check_all

    player = _player(world)
    if not player:
        return []
    ctx = {"player": player}
    done: List[Dict[str, Any]] = []
    for q in active_questlines(world):
        if q.get("state") != "active":
            continue
        k = int(q["current"])
        if k >= len(q["stages"]):
            continue
        stage = q["stages"][k]
        ok, _ = check_all(world, stage.get("complete_when") or [], ctx)
        if not ok:
            continue
        apply_all(world, stage.get("reward") or [], ctx)
        nxt = q["stages"][k + 1] if k + 1 < len(q["stages"]) else None
        world.run_query(
            """
            MATCH (q:Quest {name: $n})
            SET q.current = $k, q.state = $state, q.stage = $stage, q.objective = $objective
            """,
            n=q["name"],
            k=k + 1,
            state="active" if nxt else "completed",
            stage=nxt["title"] if nxt else "done",
            objective=nxt["objective"] if nxt else "",
        )
        announce(stage.get("news") or f"{player} finished: {stage.get('title')}.")
        done.append({"quest": q["name"], "stage": stage, "next": nxt})
    return done


class QuestTracker:
    """GameLoop 每拍回调：推进任务线，公告进大事记，下一阶段的委托人收到提醒。"""

    def on_tick(self, loop) -> None:
        from triggers import Trigger

        def announce(text: str) -> None:
            loop._record_announcements({"events": [{"type": "announce", "text": text}]})

        for done in check_progress(loop.world, announce=announce):
            nxt = done.get("next") or {}
            giver = loop.get_agent_state(nxt.get("giver") or "")
            if giver is not None:
                loop._push_triggers(giver, [Trigger(
                    "event", "quest",
                    f"The player finished '{done['stage'].get('title')}'. You could now ask them to: "
                    f"{nxt.get('objective')}",
                    0.7, loop.sim_time,
                )])
