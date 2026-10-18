"""
任务线进度：每拍检查当前阶段的完成条件；完成时发奖励、全营地广播、提醒下一阶段的委托人，推进阶段。

休眠的任务线（state=dormant）先等玩家接下：听说了某条线索、捡到某样东西、走到某个地方、
某些条件成立、或者依次做了某些事（见 quest.trail），可以限定时段；任一成立就转为 active，
先发生异象（omen：一句话 + 改世界），再提醒第一阶段的委托人。
休眠期间，知道那条线索的 NPC 会被提醒去告诉玩家。

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


def came_across(world, player: str, start: Dict[str, Any], *, seen: List[Dict[str, Any]] = (),
                hour: Optional[float] = None, here: str = "") -> Optional[str]:
    """玩家接下任务的那条途径（说给玩家听的一句话）；还没有返回 None。

    seen：任务线写进世界以来玩家的足迹（quest.trail）；hour：现在几点（判 start.at）。
    """
    from .spec import in_hours, parse_hours

    span = parse_hours(start.get("at") or "")
    if span and hour is not None and not in_hours(hour, span):
        return None
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
    if start.get("when"):
        from graph_ops import check_all

        try:
            if check_all(world, start["when"], {"player": player, "here": here})[0]:
                return "noticed something was off"
        except Exception:  # noqa: BLE001
            pass
    if start.get("pattern"):
        from routine import DAY_LENGTH_SECONDS
        from .trail import matches

        within = float(start.get("within_hours") or 0) * DAY_LENGTH_SECONDS / 24.0
        if matches(start["pattern"], list(seen), within=within):
            return "felt drawn into something"
    return None


def _player_here(world, player: str) -> str:
    rows = world.run_query(
        "MATCH (:Person {name: $p})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1", p=player
    ) or []
    return rows[0]["loc"] if rows else ""


def _omen(world, player: str, here: str, omen: Dict[str, Any]) -> None:
    """接下的那一刻发生的事：执行效果；被挪到这里的东西重新摆到地上。"""
    from graph_ops import apply_all

    effects = omen.get("effects") or []
    if not effects:
        return
    apply_all(world, effects, {"player": player, "here": here})
    moved = [e.get("entity") for e in effects if isinstance(e, dict) and e.get("op") == "relocate"]
    if moved and here:
        world.run_query("MATCH (i:Item) WHERE i.name IN $n REMOVE i.x, i.y", n=moved)
        try:
            from spatial import place_unplaced

            place_unplaced(world, here)
        except Exception:  # noqa: BLE001
            pass


def check_progress(world, *, announce: Callable[[str], None],
                   seen_since: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
                   hour: Optional[float] = None) -> List[Dict[str, Any]]:
    """推进所有任务线；返回这次的变化：接下了（started）或完成了某个阶段。announce(text) 负责公告。

    seen_since(quest) -> 这条任务线写进世界以来玩家的足迹；hour：现在几点。
    """
    from graph_ops import apply_all, check_all

    player = _player(world)
    if not player:
        return []
    ctx = {"player": player}
    done: List[Dict[str, Any]] = []
    for q in active_questlines(world):
        stages = q["stages"]
        if q.get("state") == "dormant":
            start = q.get("start") or {}
            here = _player_here(world, player)
            seen = seen_since(q["name"]) if seen_since else []
            how = came_across(world, player, start, seen=seen, hour=hour, here=here)
            if not how or not stages:
                continue
            world.run_query("MATCH (q:Quest {name: $n}) SET q.state = 'active', q.started_by = $how",
                            n=q["name"], how=how)
            omen = start.get("omen") or {}
            if omen:
                try:
                    _omen(world, player, here, omen)
                except Exception as e:  # noqa: BLE001
                    print(f"[WARN] questline omen {q['name']}: {e}")
                if omen.get("text"):
                    announce(str(omen["text"]))
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
    """GameLoop 每拍回调：记玩家足迹，推进任务线，公告进大事记，委托人 / 知情人收到提醒。"""

    def __init__(self) -> None:
        from .trail import Observer

        self.observer = Observer()
        # 每条休眠任务线头一次被看到的时刻：pattern 只看那之后玩家做的事
        self.since: Dict[str, float] = {}

    def seen_since(self, player: str, quest: str, now: float) -> List[Dict[str, Any]]:
        from .trail import events

        return events(player, self.since.setdefault(quest, now))

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
        now = float(loop.sim_time)
        if player:
            try:
                self.observer.observe(world, player, now=now, conversations=getattr(loop, "conversations", []))
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] player trail: {e}")
            told = set()
            for t in tellers_to_nudge(world, player):
                if nudge(t["person"], "quest_hook",
                         f"You know something {player} should hear: {t['text'] or t['clue']} "
                         f"If you run into {player}, tell them about it.", 0.6):
                    told.add(t["quest"])
            for name in told:
                world.run_query("MATCH (q:Quest {name: $n}) SET q.hook_told = true", n=name)

        from routine import hour_of_day

        seen = (lambda quest: self.seen_since(player, quest, now)) if player else None
        for done in check_progress(world, announce=announce, seen_since=seen, hour=hour_of_day(now)):
            nxt = done.get("next") or {}
            if done.get("started"):
                text = (f"{player} has {done['how']} and may want to help. You could ask them to: "
                        f"{nxt.get('objective')}")
            else:
                text = (f"The player finished '{done['stage'].get('title')}'. You could now ask them to: "
                        f"{nxt.get('objective')}")
            nudge(nxt.get("giver") or "", "quest", text, 0.7)
