"""
任务线进度：每拍检查当前阶段的完成条件；完成时发奖励、全营地广播、提醒下一阶段的委托人，推进阶段。

休眠的任务线（state=dormant）先等玩家接下：听说了某条线索、捡到某样东西、走到某个地方，
任一成立就转为 active，第一阶段的委托人收到提醒。休眠期间，知道那条线索的 NPC 会被提醒去告诉玩家。

只通过 GameLoop.hooks 接入（on_tick），不改模拟本身。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional


def _load(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (TypeError, json.JSONDecodeError):
        return default


def active_questlines(world) -> List[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (q:Quest) WHERE q.questline = true
        RETURN q.name AS name, q.title AS title, q.description AS premise, q.state AS state,
               coalesce(q.current, 0) AS current, q.stages AS stages, q.start AS start,
               coalesce(q.hook, '') AS hook, coalesce(q.hook_told, false) AS hook_told
        ORDER BY name
        """
    ) or []
    return [{**r, "stages": _load(r.get("stages"), []), "start": _load(r.get("start"), {})} for r in rows]


def _player(world) -> Optional[str]:
    rows = world.run_query(
        "MATCH (p:Person) WHERE coalesce(p.is_player, false) OR p.role = 'player' RETURN p.name AS n LIMIT 1"
    ) or []
    return rows[0]["n"] if rows else None


def came_across(world, player: str, start: Dict[str, Any]) -> Optional[str]:
    """玩家接下任务的那条途径（说给玩家听的一句话）；还没有返回 None。"""
    for clue in start.get("hear") or []:
        if world.run_query(
            "MATCH (:Person {name: $p})-[:KNOWS_FACT|BELIEVES]->(c {name: $c}) RETURN 1 AS ok LIMIT 1",
            p=player, c=clue,
        ):
            return f"heard about {clue}"
    for item in start.get("find") or []:
        if world.run_query(
            "MATCH (:Person {name: $p})-[:HAS_ITEM]->(:Item {name: $i}) RETURN 1 AS ok LIMIT 1", p=player, i=item
        ):
            return f"found {item}"
    for region in start.get("visit") or []:
        if world.run_query(
            "MATCH (:Person {name: $p})-[:LOCATED_AT]->(:Location {name: $r}) RETURN 1 AS ok LIMIT 1",
            p=player, r=region,
        ):
            return f"came to {region}"
    return None


def check_progress(world, *, announce: Callable[[str], None]) -> List[Dict[str, Any]]:
    """推进所有任务线；返回这次的变化：接下了（started）或完成了某个阶段。announce(text) 负责公告。"""
    from graph_ops import apply_all, check_all

    player = _player(world)
    if not player:
        return []
    ctx = {"player": player}
    done: List[Dict[str, Any]] = []
    for q in active_questlines(world):
        stages = q["stages"]
        if q.get("state") == "dormant":
            how = came_across(world, player, q.get("start") or {})
            if not how or not stages:
                continue
            world.run_query("MATCH (q:Quest {name: $n}) SET q.state = 'active', q.started_by = $how",
                            n=q["name"], how=how)
            announce(f"{player} {how}: {q.get('hook') or q.get('title')}")
            done.append({"quest": q["name"], "started": True, "how": how, "next": stages[0]})
            continue  # 接下的这一拍不判第一阶段：先让委托人开口
        if q.get("state") != "active":
            continue
        k = int(q["current"])
        if k >= len(stages):
            continue
        stage = stages[k]
        ok, _ = check_all(world, stage.get("complete_when") or [], ctx)
        if not ok:
            continue
        apply_all(world, stage.get("reward") or [], ctx)
        nxt = stages[k + 1] if k + 1 < len(stages) else None
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


def tellers_to_nudge(world, player: str) -> List[Dict[str, Any]]:
    """休眠任务线里「听说」线索的知情人（每条任务线只提醒一次）：[{quest, person, clue, text}]。"""
    out = []
    for q in active_questlines(world):
        if q.get("state") != "dormant" or q.get("hook_told"):
            continue
        for clue in (q.get("start") or {}).get("hear") or []:
            rows = world.run_query(
                """
                MATCH (p:Person)-[:KNOWS_FACT|BELIEVES]->(c {name: $c})
                WHERE p.name <> $player
                RETURN p.name AS person, coalesce(c.text, '') AS text ORDER BY person
                """,
                c=clue, player=player,
            ) or []
            out += [{"quest": q["name"], "clue": clue, **r} for r in rows]
    return out


class QuestTracker:
    """GameLoop 每拍回调：推进任务线，公告进大事记，委托人 / 知情人收到提醒。"""

    def on_tick(self, loop) -> None:
        from triggers import Trigger

        def announce(text: str) -> None:
            loop._record_announcements({"events": [{"type": "announce", "text": text}]})

        def nudge(name: str, kind: str, text: str, priority: float) -> bool:
            state = loop.get_agent_state(name or "")
            if state is None:
                return False
            loop._push_triggers(state, [Trigger("event", kind, text, priority, loop.sim_time)])
            return True

        world = loop.world
        player = _player(world)
        if player:
            told = set()
            for t in tellers_to_nudge(world, player):
                if nudge(t["person"], "quest_hook",
                         f"You know something {player} should hear: {t['text'] or t['clue']} "
                         f"If you run into {player}, tell them about it.", 0.6):
                    told.add(t["quest"])
            for name in told:
                world.run_query("MATCH (q:Quest {name: $n}) SET q.hook_told = true", n=name)

        for done in check_progress(world, announce=announce):
            nxt = done.get("next") or {}
            if done.get("started"):
                text = (f"{player} has {done['how']} and may want to help. You could ask them to: "
                        f"{nxt.get('objective')}")
            else:
                text = (f"The player finished '{done['stage'].get('title')}'. You could now ask them to: "
                        f"{nxt.get('objective')}")
            nudge(nxt.get("giver") or "", "quest", text, 0.7)
