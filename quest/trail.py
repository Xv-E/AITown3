"""
玩家足迹：玩家最近做过的事，按时间排成一串，给任务线的 start.pattern 匹配
（「在森林和营地之间来回走了三趟」「夜里连着摸了两次老井」……）。

事件：{"who", "event", "what", "target", "t"}
  enter    走进某个地点          （每拍对比位置，见 Observer）
  pick_up  拿到某样东西 / drop 放下（每拍对比背包）
  talk     和某人说上了话        （从 loop.conversations 里看）
  do       做了某个动作          （服务端执行玩家动作后调用 note，what = 动作名，target = 对象）

只在内存里（重启 / 读档后从头记），不碰数据库结构。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set

TRAIL_LIMIT = 300
EVENTS = ("enter", "pick_up", "drop", "talk", "do")

_events: Deque[Dict[str, Any]] = deque(maxlen=TRAIL_LIMIT)
_lock = threading.Lock()


def note(who: str, event: str, what: str, *, at: float, target: str = "") -> None:
    if not who or event not in EVENTS or not what:
        return
    with _lock:
        _events.append({"who": who, "event": event, "what": what, "target": target or "", "t": float(at)})


def events(who: str, since: float = float("-inf")) -> List[Dict[str, Any]]:
    with _lock:
        return [e for e in _events if e["who"] == who and e["t"] >= since]


def clear() -> None:
    with _lock:
        _events.clear()


def expand(pattern: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """{"times": 3} 展开成三步。"""
    out = []
    for step in pattern or []:
        out += [step] * max(1, int(step.get("times") or 1))
    return out


def _hit(step: Dict[str, Any], e: Dict[str, Any]) -> bool:
    if step.get("event") != e["event"] or step.get("what") != e["what"]:
        return False
    return not step.get("target") or step["target"] == e["target"]


def matches(pattern: List[Dict[str, Any]], seen: List[Dict[str, Any]], *, within: float = 0.0) -> bool:
    """seen 里按顺序出现了 pattern 的每一步（中间可以夹别的事）；within > 0 时首尾不超过这么多模拟秒。"""
    steps = expand(pattern)
    if not steps:
        return False
    for start, first in enumerate(seen):
        if not _hit(steps[0], first):
            continue
        k = 1
        for e in seen[start + 1:]:
            if k == len(steps):
                break
            if within and e["t"] - first["t"] > within:
                break
            if _hit(steps[k], e):
                k += 1
        if k == len(steps):
            return True
    return False


class Observer:
    """每拍看一眼玩家：换了地方、背包变了、跟人说上了话，就记一笔。"""

    def __init__(self) -> None:
        self.where: Optional[str] = None
        self.held: Optional[Set[str]] = None
        self.talks: Set[str] = set()

    def observe(self, world, player: str, *, now: float, conversations: List[Dict[str, Any]] = ()) -> None:
        # 第一次看：现在的位置、背包、已有的对话都是「本来就这样」，不算发生了什么
        first = self.held is None
        rows = world.run_query(
            """
            MATCH (p:Person {name: $p})
            OPTIONAL MATCH (p)-[:LOCATED_AT]->(l:Location)
            OPTIONAL MATCH (p)-[:HAS_ITEM]->(i:Item)
            RETURN l.name AS loc, collect(i.name) AS items
            """,
            p=player,
        ) or []
        if rows:
            loc, items = rows[0].get("loc"), set(rows[0].get("items") or [])
            if loc and loc != self.where:
                if self.where is not None:
                    note(player, "enter", loc, at=now)
                self.where = loc
            if self.held is not None:
                for name in sorted(items - self.held):
                    note(player, "pick_up", name, at=now)
                for name in sorted(self.held - items):
                    note(player, "drop", name, at=now)
            self.held = items
        for c in conversations or []:
            cid = str(c.get("id") or "")
            if not cid or cid in self.talks:
                continue
            self.talks.add(cid)
            if not first and player in (c.get("a"), c.get("b")):
                other = c.get("b") if c.get("a") == player else c.get("a")
                note(player, "talk", str(other or ""), at=now)
