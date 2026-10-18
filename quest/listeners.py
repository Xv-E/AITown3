"""
任务线监听器的运行时：每拍对每条任务线、每个正在听的监听器，看事件总线（quest.trail）里
它等的事发生了没有、条件成不成立，成立就执行 then（说一句 / 改世界 / 接下任务线 / 提醒某个 NPC）。

触发记录存在 Quest 节点的 listener_state 上（只触发一次的、冷却中的，读档后也算数）；
「只看条件」的监听器按上升沿触发（条件从不成立变成成立的那一拍），这一点只记在内存里。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import trail
from .spec import ListenerSpec, in_hours, parse_hours

Announce = Callable[[str], None]
Nudge = Callable[[str, str, str, float], bool]


def hours_to_sim(hours: float) -> float:
    from routine import DAY_LENGTH_SECONDS

    return float(hours or 0) * DAY_LENGTH_SECONDS / 24.0


def load(q: Dict[str, Any]) -> List[ListenerSpec]:
    from .spec import _listener

    try:
        raw = json.loads(q.get("listeners") or "[]") if isinstance(q.get("listeners"), str) else (q.get("listeners") or [])
    except json.JSONDecodeError:
        raw = []
    return [_listener(x) for x in raw if isinstance(x, dict)]


def listening(ls: ListenerSpec, q: Dict[str, Any]) -> bool:
    """这条任务线现在的状态下，这个监听器在不在听。"""
    state, phase = q.get("state"), ls.phase()
    if phase == "dormant":
        return state == "dormant"
    if phase == "active":
        return state == "active"
    if phase == "always":
        return state in ("dormant", "active")
    if phase.startswith("stage:") and state == "active":
        stages = q.get("stages") or []
        k = int(q.get("current") or 0)
        return k < len(stages) and stages[k].get("id") == phase[6:]
    return False


@dataclass
class Match:
    actor: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)


def evaluate(ls: ListenerSpec, seen: List[Dict[str, Any]], *, player: str, hour: Optional[float],
             check: Callable[[List[Dict[str, Any]], Dict[str, str]], bool], here: str = "") -> Optional[Match]:
    """等的事发生了、条件也成立就返回匹配（谁做的、哪几条事件），否则 None。纯函数，不改任何东西。"""
    span = parse_hours(ls.at)
    if span and hour is not None and not in_hours(hour, span):
        return None
    within = hours_to_sim(ls.within_hours)
    if ls.sequence:
        got = trail.find_sequence(ls.sequence, seen, within=within, player=player)
    elif ls.on:
        got = trail.find_count(ls.on, ls.count, seen, within=within, player=player)
    else:
        got = []
    if got is None:
        return None
    actor = got[-1]["who"] if got and got[-1]["who"] else player
    if ls.when and not check(ls.when, {"player": player, "here": here, "actor": actor}):
        return None
    return Match(actor=actor, events=list(got))


class ListenerMemory:
    """跨拍要记住的：每个监听器从什么时候开始听、只看条件的上次成不成立。"""

    def __init__(self) -> None:
        self.since: Dict[Tuple[str, str], float] = {}  # (任务线, 监听器) -> 开始听的时刻
        self.was_true: Set[Tuple[str, str]] = set()

    def started_listening(self, key: Tuple[str, str], now: float) -> float:
        return self.since.setdefault(key, now)

    def stopped_listening(self, key: Tuple[str, str]) -> None:
        self.since.pop(key, None)
        self.was_true.discard(key)


def run(world, quests: List[Dict[str, Any]], *, player: str, now: float, hour: Optional[float],
        memory: ListenerMemory, announce: Announce, nudge: Nudge,
        start_quest: Callable[[Dict[str, Any], str, str], Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """跑一遍所有监听器；返回因此被接下的任务线（start_quest 的结果）。"""
    from graph_ops import check_all

    def check(conds, ctx) -> bool:
        try:
            return check_all(world, conds, ctx)[0]
        except Exception:  # noqa: BLE001
            return False

    here = ""
    started: List[Dict[str, Any]] = []
    for q in quests:
        listeners = load(q)
        if not listeners:
            continue
        fired = _state(q)
        changed = False
        for ls in listeners:
            key = (q["name"], ls.id)
            if not listening(ls, q):
                memory.stopped_listening(key)
                continue
            since = memory.started_listening(key, now)
            record = fired.get(ls.id) or {}
            if ls.fires_once() and record.get("n"):
                continue
            if ls.cooldown_hours and now - float(record.get("last", -1e18)) < hours_to_sim(ls.cooldown_hours):
                continue
            seen = trail.events(None, max(since, float(record.get("used", -1e18)) + 1e-9))
            if not here:
                here = _here(world, player)
            got = evaluate(ls, seen, player=player, hour=hour, check=check, here=here)
            if not ls.on and not ls.sequence:  # 只看条件：上升沿才算
                if got is None:
                    memory.was_true.discard(key)
                    continue
                if key in memory.was_true:
                    continue
                memory.was_true.add(key)
            if got is None:
                continue
            fired[ls.id] = {"n": int(record.get("n") or 0) + 1, "last": now,
                            "used": max([e["t"] for e in got.events] + [float(record.get("used", -1e18))])}
            changed = True
            trail.note("", "listener", ls.id, at=now, target=q["name"])
            done = _fire(world, q, ls, got, player=player, here=here, announce=announce, nudge=nudge,
                         start_quest=start_quest)
            if done:
                started.append(done)
                q["state"] = "active"
                q["just_started"] = True  # 这一拍不判第一阶段：先让委托人开口
        if changed:
            world.run_query("MATCH (q:Quest {name: $n}) SET q.listener_state = $s",
                            n=q["name"], s=json.dumps(fired, ensure_ascii=False))
    return started


def _state(q: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(q.get("listener_state") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _here(world, player: str) -> str:
    rows = world.run_query(
        "MATCH (:Person {name: $p})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1", p=player
    ) or []
    return rows[0]["loc"] if rows else ""


def _fire(world, q: Dict[str, Any], ls: ListenerSpec, got: Match, *, player: str, here: str,
          announce: Announce, nudge: Nudge, start_quest) -> Optional[Dict[str, Any]]:
    then = ls.then
    ctx = {"player": player, "here": here, "actor": got.actor}
    if then.get("effects"):
        try:
            apply_effects(world, then["effects"], ctx)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] listener {q['name']}.{ls.id}: {e}")
    if then.get("say"):
        announce(str(then["say"]).replace("@player", player).replace("@actor", got.actor))
    nudge_spec = then.get("nudge")
    if isinstance(nudge_spec, dict) and nudge_spec.get("who"):
        nudge(str(nudge_spec["who"]), "quest_event",
              str(nudge_spec.get("text") or "").replace("@player", player).replace("@actor", got.actor), 0.65)
    if then.get("start") and q.get("state") == "dormant":
        return start_quest(q, f"set something in motion ({ls.id})", here)
    return None


def apply_effects(world, effects: List[Dict[str, Any]], ctx: Dict[str, str]) -> None:
    """执行效果；被挪到 @here 的东西重新摆到地上（不然还留着原来那块地的坐标）。"""
    from graph_ops import apply_all, resolve_token

    apply_all(world, effects, ctx)
    moved = [e for e in effects if isinstance(e, dict) and e.get("op") == "relocate"]
    for e in moved:
        try:
            name, to = resolve_token(e.get("entity"), ctx), resolve_token(e.get("to"), ctx)
        except ValueError:
            continue
        world.run_query("MATCH (i:Item {name: $n}) REMOVE i.x, i.y", n=name)
        try:
            from spatial import place_unplaced

            place_unplaced(world, to)
        except Exception:  # noqa: BLE001
            pass
