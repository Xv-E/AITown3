"""
事件总线：镇上最近发生的事，按时间排成一串，给任务线的监听器（listeners）和 start.pattern 匹配
（「夜里在森林和营地之间来回走了三趟」「Reed 偷偷进了仓库」「天亮了」……）。

事件：{"who", "event", "what", "target", "t"}
  enter         某人走进某个地点          （每拍对比所有人的位置，见 Observer）
  pick_up/drop  某人拿到 / 放下某样东西    （每拍对比所有人的背包）
  talk          某人和 what 说上了话       （从 loop.conversations 里看，两边各一条）
  do            某人做了某个动作           （NPC 从 GameLoop 的历史里看；玩家由服务端执行动作后 note）
  learn         玩家知道了某条线索 / 传闻  （每拍对比玩家的 KNOWS_FACT / BELIEVES）
  time          进入某个时段（what = night / dawn / ……，见 spec.TIMES_OF_DAY）
  hour          整点（what = "0".."23"）    new_day  新的一天（what = 第几天）
  news          大事记里多了一条（what = 原文）
  stage_done    任务线的某个阶段完成了（what = 阶段 id，target = 任务线）
  quest_started 任务线被接下了（what = 任务线）
  listener      某个监听器触发了（what = 监听器 id，target = 任务线）

只在内存里（重启 / 读档后从头记），不碰数据库结构。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

TRAIL_LIMIT = 600
PERSON_EVENTS = ("enter", "pick_up", "drop", "talk", "do", "learn")
WORLD_EVENTS = ("time", "hour", "new_day", "news", "stage_done", "quest_started", "listener")
EVENTS = PERSON_EVENTS + WORLD_EVENTS

_events: Deque[Dict[str, Any]] = deque(maxlen=TRAIL_LIMIT)
_lock = threading.Lock()


def note(who: str, event: str, what: str, *, at: float, target: str = "") -> None:
    if event not in EVENTS or what in (None, ""):
        return
    if event in PERSON_EVENTS and not who:
        return
    with _lock:
        _events.append({"who": who or "", "event": event, "what": str(what), "target": target or "", "t": float(at)})


def events(who: Optional[str] = None, since: float = float("-inf")) -> List[Dict[str, Any]]:
    """since 之后的事件；给了 who 就只要这个人的。"""
    with _lock:
        return [e for e in _events if e["t"] >= since and (who is None or e["who"] == who)]


def clear() -> None:
    with _lock:
        _events.clear()


# ---------- 匹配 ----------

def who_matches(want: str, who: str, player: str) -> bool:
    """want：@player / npc / anyone（或不写）/ 某个人的名字。"""
    want = (want or "anyone").strip()
    if want == "anyone":
        return True
    if want == "@player":
        return bool(who) and who == player
    if want == "npc":
        return bool(who) and who != player
    return who == want


def hit(on: Dict[str, Any], e: Dict[str, Any], player: str = "") -> bool:
    """一条事件是不是 on 说的那种：event 相同，who / what / target 对得上，contains 是 what 的子串。"""
    if on.get("event") != e["event"]:
        return False
    if on.get("what") not in (None, "") and str(on["what"]) != e["what"]:
        return False
    if on.get("target") and on["target"] != e["target"]:
        return False
    if on.get("contains") and str(on["contains"]).lower() not in e["what"].lower():
        return False
    if e["event"] in PERSON_EVENTS or on.get("who"):
        return who_matches(str(on.get("who") or "anyone"), e["who"], player)
    return True


def expand(pattern: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """{"times": 3} 展开成三步。"""
    out = []
    for step in pattern or []:
        out += [step] * max(1, int(step.get("times") or 1))
    return out


def find_sequence(pattern: List[Dict[str, Any]], seen: List[Dict[str, Any]], *, within: float = 0.0,
                  player: str = "") -> Optional[List[Dict[str, Any]]]:
    """seen 里按顺序出现了 pattern 的每一步（中间可以夹别的事）就返回匹配到的那几条事件，否则 None。

    within > 0 时首尾不超过这么多模拟秒。旧写法的 pattern 不写 who，默认指玩家。
    """
    steps = expand(pattern)
    if not steps:
        return None
    steps = [s if s.get("who") or s.get("event") not in PERSON_EVENTS else {**s, "who": "@player"} for s in steps]
    for start, first in enumerate(seen):
        if not hit(steps[0], first, player):
            continue
        got = [first]
        for e in seen[start + 1:]:
            if len(got) == len(steps):
                break
            if within and e["t"] - first["t"] > within:
                break
            if hit(steps[len(got)], e, player):
                got.append(e)
        if len(got) == len(steps):
            return got
    return None


def matches(pattern: List[Dict[str, Any]], seen: List[Dict[str, Any]], *, within: float = 0.0,
            player: str = "") -> bool:
    if player == "" and seen:
        player = seen[0]["who"]  # 旧调用：seen 只有玩家自己的事件
    return find_sequence(pattern, seen, within=within, player=player) is not None


def find_count(on: Dict[str, Any], count: int, seen: List[Dict[str, Any]], *, within: float = 0.0,
               player: str = "") -> Optional[List[Dict[str, Any]]]:
    """on 那种事发生了 count 次（within > 0 时这几次落在这么多模拟秒内）就返回这几条，否则 None。"""
    hits = [e for e in seen if hit(on, e, player)]
    n = max(1, int(count or 1))
    for i in range(len(hits) - n + 1):
        window = hits[i:i + n]
        if not within or window[-1]["t"] - window[0]["t"] <= within:
            return window
    return None


# ---------- 观察 ----------

class Observer:
    """每拍看一眼世界，把变化记成事件：谁换了地方、谁的背包变了、谁跟谁说上了话、
    NPC 做完了什么、玩家知道了什么、到了什么时辰、大事记多了什么。"""

    def __init__(self) -> None:
        self.where: Dict[str, str] = {}
        self.held: Dict[str, Set[str]] = {}
        self.known: Optional[Set[str]] = None
        self.talks: Set[str] = set()
        self.history_seen: Dict[str, int] = {}  # NPC -> 看过的最后一条历史的 id()
        self.news: Optional[int] = None  # 看过的最后一条大事记的 id()
        self.clock: Optional[Tuple[int, int, frozenset]] = None  # (第几天, 几点, 正处在的时段)
        self.started = False

    def observe(self, world, player: str, *, now: float, conversations: List[Dict[str, Any]] = (),
                loop=None) -> None:
        # 第一次看：现在的样子都是「本来就这样」，不算发生了什么
        first = not self.started
        self.started = True
        self._people(world, player, now, first)
        self._learned(world, player, now, first)
        self._conversations(conversations, now, first)
        if loop is not None:
            self._npc_actions(loop, player, now, first)
            self._chronicle(getattr(loop, "chronicle", None) or [], now, first)
        self._time(now, first)

    def _people(self, world, player: str, now: float, first: bool) -> None:
        rows = world.run_query(
            """
            MATCH (p:Person)
            OPTIONAL MATCH (p)-[:LOCATED_AT]->(l:Location)
            OPTIONAL MATCH (p)-[:HAS_ITEM]->(i:Item)
            RETURN p.name AS who, l.name AS loc, collect(i.name) AS items
            """,
        ) or []
        for r in rows:
            who, loc, items = r.get("who"), r.get("loc"), set(r.get("items") or [])
            if not who:
                continue
            before = self.where.get(who)
            if loc and loc != before:
                if before is not None and not first:
                    note(who, "enter", loc, at=now)
                self.where[who] = loc
            held = self.held.get(who)
            if held is not None and not first:
                for name in sorted(items - held):
                    note(who, "pick_up", name, at=now)
                for name in sorted(held - items):
                    note(who, "drop", name, at=now)
            self.held[who] = items

    def _learned(self, world, player: str, now: float, first: bool) -> None:
        rows = world.run_query(
            "MATCH (:Person {name: $p})-[:KNOWS_FACT|BELIEVES]->(c) RETURN collect(c.name) AS names", p=player,
        ) or []
        known = set((rows[0].get("names") if rows else None) or [])
        if self.known is not None and not first:
            for name in sorted(known - self.known):
                note(player, "learn", name, at=now)
        self.known = known

    def _conversations(self, conversations, now: float, first: bool) -> None:
        for c in conversations or []:
            cid = str(c.get("id") or "")
            if not cid or cid in self.talks:
                continue
            self.talks.add(cid)
            a, b = str(c.get("a") or ""), str(c.get("b") or "")
            if first or not a or not b:
                continue
            note(a, "talk", b, at=now)
            note(b, "talk", a, at=now)

    def _npc_actions(self, loop, player: str, now: float, first: bool) -> None:
        for state in getattr(loop, "agents", []) or []:
            name = state.agent.name
            if name == player:
                continue  # 玩家的动作由服务端 note，不从历史里重复记
            history = getattr(state, "history", None) or []
            last = self.history_seen.get(name)
            start = 0
            if last is not None:
                ids = [id(e) for e in history]
                start = ids.index(last) + 1 if last in ids else 0
            if history:
                self.history_seen[name] = id(history[-1])
            if first:
                continue
            for e in history[start:]:
                if e.get("kind") != "done" or not e.get("action"):
                    continue
                args = list(e.get("args") or [])
                note(name, "do", str(e["action"]), at=now, target=str(args[-1]) if len(args) > 1 else "")

    def _chronicle(self, chronicle: List[Dict[str, Any]], now: float, first: bool) -> None:
        if not chronicle:
            return
        ids = [id(c) for c in chronicle]
        start = ids.index(self.news) + 1 if self.news in ids else (0 if self.news is not None else len(ids))
        if not first:
            for c in chronicle[start:]:
                note("", "news", str(c.get("text") or ""), at=now)
        self.news = ids[-1]

    def _time(self, now: float, first: bool) -> None:
        from routine import DAY_LENGTH_SECONDS, hour_of_day

        from .spec import TIMES_OF_DAY, in_hours

        hour = hour_of_day(now)
        day = int(now // DAY_LENGTH_SECONDS) + 1
        # 时段会重叠（day 包含 morning）：每个刚开始的时段各记一条
        periods = frozenset(p for p, span in TIMES_OF_DAY.items() if in_hours(hour, span))
        clock = (day, int(hour), periods)
        if self.clock is not None and not first:
            if day != self.clock[0]:
                note("", "new_day", str(day), at=now)
            if int(hour) != self.clock[1]:
                note("", "hour", str(int(hour)), at=now)
            for p in sorted(periods - self.clock[2]):
                note("", "time", p, at=now)
        self.clock = clock
