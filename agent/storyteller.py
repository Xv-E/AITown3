"""
Storyteller — 定期读小镇现状，由 LLM 决定故事往哪走，并用少量受控手段推动。

剧情主要靠「对话」往前走：
  talk      让某个 NPC 去找某人（NPC 或玩家）当面说一件事；这段话用大模型写，说了什么记进剧情线索
  ask       让某个 NPC 去向玩家求助（要一样东西）；当面开口后落成人情任务
  whisper   私下给某个 NPC 的消息（流言、念头、请求）；挂在他心上，之后的对话里会带出来
  announce  公开事件：进大事记，所有人收到 news 触发器
  adjust    在限定幅度内改物品数值属性（风暴吹灭火堆、商队补货……），让事件真的影响世界

剧情线索（threads）：几条正在发展的故事，每条有标题、利害、相关的人和一串进展。进展主要来自对话：
被派去说话的人说了什么、玩家怎么回、谁向谁求了助。一条线索攒够了进展、玩家也卷进来了，就请任务设计师
据此写一条能玩通的任务线（后台写，验证过才放进世界）。

LLM 输出只经过这些 op、且逐条校验后才生效；没有 LLM 时用模板推进（规则模式 / 测试），
模板同样从世界里真实发生的事出发：冲突、势力大事、欠下的保护费、人的心愿。
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from game_loop import GameLoop

STORY_HOURS = float(os.environ.get("AITOWN3_STORY_HOURS", "4"))
MAX_BEATS = 3
MAX_TEXT = 220
URGENT_PRIORITY = 0.85
WHISPER_PRIORITY = 0.6
TALK_PRIORITY = 0.75
# 剧情线索：同时最多几条在发展；每条留多少条进展；攒够几条进展（且玩家卷进来）就写成任务线
MAX_OPEN_THREADS = 4
THREAD_NOTES = 10
MATURE_NOTES = 3
# 被派去说话 / 求助的差事，多少游戏小时没办成就作罢
ERRAND_HOURS = float(os.environ.get("AITOWN3_ERRAND_HOURS", "18"))
# 线索写成任务线：两次之间至少隔多少游戏小时；可以整个关掉
QUESTLINE_GAP_HOURS = float(os.environ.get("AITOWN3_STORY_QUESTLINE_HOURS", "48"))
AUTO_QUESTLINES = os.environ.get("AITOWN3_STORY_QUESTLINES", "1").strip().lower() not in ("0", "false", "no")
# 数值属性里这些是布局 / 坐标，不给剧情改
_LAYOUT_PROPS = {"x", "y", "w", "h", "map_x", "map_y", "grid_w", "grid_h", "value"}
_THREAD_ID = re.compile(r"[^A-Za-z0-9]+")

SYSTEM_PROMPT = (
    "You are the storyteller of a small simulated settlement whose people plan and act on their own. "
    "Each time you are called you nudge the story: create tension, opportunities and consequences that "
    "grow from what is already happening (factions, debts, grudges, longings, favors, what people just said). "
    "Stories move forward through conversations: send people to talk to each other and to the player. "
    "Keep it grounded in this world; never invent people or places. Prefer small, concrete beats over grand ones."
)


def _hours(h: float) -> float:
    from routine import DAY_LENGTH_SECONDS

    return h * DAY_LENGTH_SECONDS / 24.0


def _phrase(text: Any) -> str:
    """心愿之类的句子塞进别的句子里：去掉句号，首字母小写。"""
    t = str(text or "").strip().rstrip(".")
    first = t.split(" ", 1)[0]
    # 第一个词里再没有大写字母（不是 AhKit 这种人名）才改小写
    return t[:1].lower() + t[1:] if first[1:] == first[1:].lower() else t


def thread_id(title: str) -> str:
    """标题 → 稳定的 id（CamelCase，只留字母数字）。"""
    parts = [p for p in _THREAD_ID.split(str(title or "")) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts)[:40]


# ---------------------------------------------------------------------------
# 差事：谁要去找谁说什么（存在 Person 上，对话 prompt 和决策都读它）
# ---------------------------------------------------------------------------

def _story_props(world, who: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    rows = world.run_query(
        "MATCH (p:Person {name: $n}) RETURN p.story_concern AS c, p.story_errand AS e LIMIT 1", n=who
    ) or []
    if not rows:
        return "", None
    errand = None
    raw = rows[0].get("e")
    if raw:
        try:
            errand = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError):
            errand = None
    return str(rows[0].get("c") or ""), errand if isinstance(errand, dict) else None


def read_errand(world, who: str) -> Optional[Dict[str, Any]]:
    try:
        return _story_props(world, who)[1]
    except Exception:  # noqa: BLE001
        return None


def set_errand(world, who: str, errand: Dict[str, Any]) -> None:
    world.run_query(
        "MATCH (p:Person {name: $n}) SET p.story_errand = $e, p.story_concern = $c",
        n=who, e=json.dumps(errand, ensure_ascii=False), c=str(errand.get("topic") or "")[:MAX_TEXT],
    )


def clear_errand(world, who: str) -> None:
    """差事办完（或作罢）；心事留着，之后的对话还会提。"""
    world.run_query("MATCH (p:Person {name: $n}) REMOVE p.story_errand", n=who)


def set_concern(world, who: str, text: str) -> None:
    world.run_query("MATCH (p:Person {name: $n}) SET p.story_concern = $c", n=who, c=str(text)[:MAX_TEXT])


def format_story_context(concern: str, errand: Optional[Dict[str, Any]], other: str) -> Tuple[str, Optional[str]]:
    """对话 prompt 用：心事 +（对方正是差事对象时）要说的事 / 要求的东西。返回 (文字, 要求的东西)。"""
    lines: List[str] = []
    ask_item: Optional[str] = None
    if concern:
        lines.append(f"On your mind lately: {concern}")
    if errand and other and errand.get("to") == other:
        topic = str(errand.get("topic") or "").strip()
        if topic and topic != concern:
            lines.append(f"You came to talk to {other} about this: {topic}")
        elif topic:
            lines.append(f"You came to talk to {other} about this.")
        ask = errand.get("ask") or {}
        if ask.get("item"):
            ask_item = str(ask["item"])
            reward = f" You can offer: {ask['reward']}." if ask.get("reward") else ""
            lines.append(
                f"You want to ask {other} for {ask_item}: {ask.get('why') or ''}.{reward} "
                "Ask for it plainly, in your own words, and fill in the favor."
            )
    return "\n".join(lines), ask_item


def story_context(world, me: str, other: str = "") -> Tuple[str, Optional[str]]:
    if world is None or not me:
        return "", None
    try:
        concern, errand = _story_props(world, me)
    except Exception:  # noqa: BLE001
        return "", None
    return format_story_context(concern, errand, other)


def on_favor_registered(world, giver: str, helper: str, favor: Dict[str, Any], quest: str) -> None:
    """对话里开口求助、落成了人情任务：挂到那条剧情线索上，差事里的「要东西」算办成了。"""
    errand = read_errand(world, giver)
    if not errand:
        return
    if errand.get("thread"):
        world.run_query("MATCH (q:Quest {name: $q}) SET q.thread = $t", q=quest, t=errand["thread"])
    if errand.get("ask") and errand.get("to") == helper:
        errand["ask"] = None
        errand["asked"] = quest
        world.run_query(
            "MATCH (p:Person {name: $n}) SET p.story_errand = $e", n=giver, e=json.dumps(errand, ensure_ascii=False)
        )


def _has_open_favor(world, giver: str, helper: str, item: str) -> bool:
    rows = world.run_query(
        """
        MATCH (:Person {name: $g})-[:GIVES_QUEST]->(q:Quest {kind: 'favor'})
        WHERE q.helper = $h AND toLower(coalesce(q.state, 'active')) = 'active'
          AND toLower(coalesce(q.item, '')) = toLower($i)
        RETURN count(q) AS n
        """,
        g=giver, h=helper, i=item,
    ) or []
    return bool(rows and int(rows[0].get("n") or 0))


@dataclass
class Storyteller:
    interval: float = 0.0  # 模拟秒；0 = 按 STORY_HOURS 换算
    use_llm: bool = True
    arc: str = ""
    beats: List[Dict[str, Any]] = field(default_factory=list)
    threads: List[Dict[str, Any]] = field(default_factory=list)
    last_at: float = 0.0
    last_questline_at: float = -1e9
    _template_turn: int = 0
    _quest_future: Any = field(default=None, repr=False)
    _quest_thread: str = ""
    _quest_pool: Optional[ThreadPoolExecutor] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.interval <= 0:
            self.interval = _hours(STORY_HOURS)

    # ----- 调度 -----

    def due(self, sim_time: float) -> bool:
        return sim_time - self.last_at >= self.interval

    def mark_started(self, sim_time: float) -> None:
        self.last_at = sim_time

    def to_dict(self, limit: int = 6) -> Dict[str, Any]:
        return {"arc": self.arc, "beats": list(reversed(self.beats[-limit:])), "threads": self.thread_views()}

    # ----- 剧情线索 -----

    def open_threads(self) -> List[Dict[str, Any]]:
        return [t for t in self.threads if t.get("status") == "open"]

    def thread(self, tid: str) -> Optional[Dict[str, Any]]:
        return next((t for t in self.threads if t.get("id") == tid), None) if tid else None

    def note(self, tid: str, text: str, clock: str = "", t: float = 0.0) -> None:
        th = self.thread(tid)
        if th is None or not text:
            return
        th.setdefault("notes", []).append({"clock": clock, "text": str(text)[:300]})
        del th["notes"][:-THREAD_NOTES]
        th["updated"] = t

    def merge_threads(self, updates: List[Dict[str, Any]], clock: str = "", t: float = 0.0) -> None:
        for u in updates or []:
            tid = u.get("id") or thread_id(u.get("title", ""))
            if not tid:
                continue
            th = self.thread(tid)
            if th is None:
                if u.get("status", "open") != "open":
                    continue
                opened = self.open_threads()
                if len(opened) >= MAX_OPEN_THREADS:
                    oldest = min(opened, key=lambda x: x.get("updated", 0.0))
                    oldest["status"] = "faded"
                th = {"id": tid, "title": u.get("title") or tid, "stakes": "", "people": [], "status": "open",
                      "opened": clock, "updated": t, "notes": [], "questline": ""}
                self.threads.append(th)
            if u.get("title"):
                th["title"] = u["title"]
            if u.get("stakes"):
                th["stakes"] = u["stakes"]
            for p in u.get("people") or []:
                if p not in th["people"]:
                    th["people"].append(p)
            if u.get("status") in ("open", "resolved"):
                th["status"] = u["status"]
            if u.get("note"):
                self.note(tid, u["note"], clock, t)
            th["updated"] = t
        # 已经结束的线索只留最近几条
        done = [x for x in self.threads if x.get("status") != "open"]
        for old in done[:-6]:
            self.threads.remove(old)

    def thread_views(self) -> List[Dict[str, Any]]:
        out = []
        for t in sorted(self.threads, key=lambda x: (x.get("status") != "open", -float(x.get("updated") or 0))):
            out.append({
                "id": t.get("id"), "title": t.get("title"), "stakes": t.get("stakes", ""),
                "status": t.get("status"), "people": list(t.get("people") or []),
                "notes": [f"{n.get('clock', '')} {n.get('text', '')}".strip() for n in (t.get("notes") or [])[-5:]],
                "questline": t.get("questline", ""),
            })
        return out

    # ----- 现状摘要 -----

    def digest(self, loop: "GameLoop") -> Dict[str, Any]:
        from conflicts import all_tensions
        from routine import format_clock

        world = loop.world
        people = []
        for s in loop.agents:
            goal = s.last_goal or {}
            people.append({
                "name": s.agent.name,
                "location": s.agent.location,
                "status": s.status,
                "goal": goal.get("goal"),
                "target": goal.get("target_entity"),
                "reason": str(goal.get("reason") or "")[:160],
                "player": loop._is_player(s),
                "routine": loop._routine_only(s),
            })
        items = []
        for r in world.run_query("MATCH (i:Item) RETURN i.name AS name, properties(i) AS p") or []:
            props = r.get("p") or {}
            # 只给物品脚本里真正用到的数值（告警条件 / 状态），别的改了也没人在乎
            script = str(props.get("script") or "")
            numeric = {
                k: v for k, v in props.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                and k not in _LAYOUT_PROPS and f'"{k}"' in script
            }
            if numeric and r.get("name"):
                items.append({"name": r["name"], "props": numeric})
        player = next((p["name"] for p in people if p["player"]), "")
        npcs = [p["name"] for p in people if not p["player"] and not p["routine"]]
        out = {
            "clock": format_clock(loop.sim_time),
            "sim_time": loop.sim_time,
            "arc": self.arc,
            "people": people,
            "items": items[:16],
            "conflicts": loop.conflicts[-6:],
            "tensions": all_tensions(world, 6),
            "chronicle": [c.get("text") for c in loop.chronicle[-5:]],
            "recent_beats": self.beats[-3:],
            "player": player,
            "threads": [
                {k: t.get(k) for k in ("id", "title", "stakes", "people", "questline")}
                | {"notes": [n.get("text") for n in (t.get("notes") or [])[-4:]]}
                for t in self.open_threads()
            ],
            "talks": [
                {"a": c.get("a"), "b": c.get("b"), "location": c.get("location"),
                 "lines": [f"{ln.get('speaker')}: {str(ln.get('text') or '')[:140]}" for ln in (c.get("lines") or [])[:3]]}
                for c in loop.conversations[-6:]
            ],
        }
        out.update(_world_story_signals(world, npcs, loop.sim_time))
        return out

    # ----- 生成 -----

    def compose_llm(self, digest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """在工作线程里调用：只读 digest，不碰 GameLoop。"""
        import time

        from llm_router import invoke_primary
        from trace_util import record_trace

        prompt = build_prompt(digest)
        t0 = time.perf_counter()
        raw, error = "", ""
        try:
            from model_instances import creative_model_name

            raw = invoke_primary(prompt, system=SYSTEM_PROMPT, max_tokens=900, model=creative_model_name())
        except Exception as e:  # noqa: BLE001
            error = str(e)
        plan = parse_plan(raw, digest) if raw else None
        record_trace(
            kind="storyteller",
            prompt=prompt,
            raw=raw or error,
            ok=plan is not None,
            error=error,
            meta={"clock": digest.get("clock"), "beats": len((plan or {}).get("beats") or [])},
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )
        return plan or self.compose_template(digest)

    def compose_template(self, digest: Dict[str, Any]) -> Dict[str, Any]:
        """无 LLM：从世界里真实发生的事找一个小事件 —— 冲突、势力大事、欠的保护费、物品状态、心愿。"""
        self._template_turn += 1
        people = digest.get("people") or []
        npcs = [p["name"] for p in people if not p.get("player")]
        talkers = [p["name"] for p in people if not p.get("player") and not p.get("routine")]
        player = digest.get("player") or ""
        busy = {e.get("who") for e in digest.get("errands") or []}
        favor_items = {str(i).lower(): str(i) for i in digest.get("favor_items") or []}
        since = digest.get("sim_time", 0.0) - self.interval

        # 1) 刚发生的抢东西：吃亏的人放不下
        conflicts = digest.get("conflicts") or []
        fresh = [c for c in conflicts if c.get("t", 0) >= since]
        if fresh:
            c = fresh[-1]
            title = f"{c['loser']} and {c['culprit']} over {c['item']}"
            return {
                "arc": f"{c['loser']} resents {c['culprit']} over {c['item']}.",
                "threads": [{"id": thread_id(title), "title": title, "people": [c["loser"], c["culprit"]],
                             "stakes": f"Whether {c['loser']} lets it go", "status": "open",
                             "note": f"{c['culprit']} took {c['item']} from {c['loser']}."}],
                "beats": [{
                    "op": "whisper",
                    "to": c["loser"],
                    "text": f"You keep thinking about how {c['culprit']} took {c['item']}. "
                            f"Maybe it is time to have a word with {c['culprit']}.",
                    "urgent": False,
                    "thread": thread_id(title),
                }],
            }

        # 2) 欠了保护费：摊主去找玩家借钱（会落成人情任务）
        cash = favor_items.get("cash")
        for o in digest.get("owed") or []:
            owner = o.get("owner")
            if not (cash and player and owner in talkers and owner not in busy):
                continue
            title = f"{owner} owes {o['faction']}"
            tid = thread_id(title)
            if (self.thread(tid) or {}).get("status") == "open" and self._template_turn % 3:
                continue  # 这条线已经开着了：别每拍都催
            why = f"{o['faction']} want the ${o['dues']:.0f} I owe for {o['business']}, and I can't pay"
            return {
                "arc": f"{o['faction']} are squeezing {owner}; someone has to help or it ends badly.",
                "threads": [{"id": tid, "title": title, "people": [owner, o["faction"], player], "status": "open",
                             "stakes": f"{o['faction']} smash {o['business']} if {owner} can't pay",
                             "note": f"{owner} is ${o['dues']:.0f} behind with {o['faction']}."}],
                "beats": [{"op": "ask", "who": owner, "item": cash, "why": why,
                           "reward": "I'll owe you, and I don't forget", "thread": tid}],
            }

        # 3) 刚发生的势力大事：被卷进去的人去找玩家说
        for e in reversed(digest.get("faction_news") or []):
            if float(e.get("t") or 0.0) < since:
                continue
            text = str(e.get("text") or "")
            named = [n for n in talkers if n in text and n not in busy]
            if not named or not player:
                continue
            who = named[-1]
            title = text[:70]
            tid = thread_id(title)
            if self.thread(tid):
                continue
            return {
                "arc": text,
                "threads": [{"id": tid, "title": title, "people": named + [player], "status": "open",
                             "stakes": "What happens next on these streets", "note": text}],
                "beats": [{"op": "talk", "who": who, "to": player, "thread": tid,
                           "topic": f"What just happened ({text}) and what it means for the street"}],
            }

        # 4) 物品数值（风吹火堆之类）
        fuel = next(
            (i for i in digest.get("items") or [] if "fuel" in i["props"]), None
        )
        if fuel and self._template_turn % 2 == 1:
            return {
                "arc": "The weather turns and the camp has to hold together.",
                "beats": [
                    {"op": "announce", "text": "A cold wind sweeps through the camp."},
                    {"op": "adjust", "item": fuel["name"], "prop": "fuel", "delta": -20,
                     "why": "the wind"},
                ],
            }

        # 5) 心愿：轮流让一个人把心里的事说给在意的人听
        hearts = [h for h in digest.get("hearts") or [] if h.get("name") in talkers and h.get("name") not in busy]
        if hearts:
            h = hearts[self._template_turn % len(hearts)]
            to = player if player and self._template_turn % 2 == 0 else (h.get("bond") or player)
            if to and to != h["name"]:
                title = f"What {h['name']} wants"
                tid = thread_id(title)
                want = _phrase(h.get("desire"))
                return {
                    "arc": self.arc or f"{h['name']} can't stop thinking about it: {want}.",
                    "threads": [{"id": tid, "title": title, "people": [h["name"], to], "status": "open",
                                 "stakes": h.get("fear") or "", "note": f"{h['name']} longs for this: {want}."}],
                    "beats": [{"op": "talk", "who": h["name"], "to": to, "thread": tid,
                               "topic": f"what you really want ({want}) and what scares you about it"}],
                }

        if npcs:
            who = npcs[self._template_turn % len(npcs)]
            return {
                "arc": self.arc or "Supplies feel thin and people start to worry.",
                "beats": [{
                    "op": "whisper",
                    "to": who,
                    "text": "You overheard someone saying supplies are running low.",
                    "urgent": False,
                }],
            }
        return {"arc": self.arc, "beats": []}

    # ----- 生效（主线程） -----

    def apply(self, loop: "GameLoop", plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        from action import get_property, set_property
        from routine import format_clock
        from triggers import Trigger

        world = loop.world
        clock = format_clock(loop.sim_time)
        if plan.get("arc"):
            self.arc = str(plan["arc"])[:MAX_TEXT]
        self.merge_threads(plan.get("threads") or [], clock, loop.sim_time)
        player = next((s.agent.name for s in loop.agents if loop._is_player(s)), "")
        applied: List[Dict[str, Any]] = []
        for beat in plan.get("beats") or []:
            op = beat.get("op")
            tid = beat.get("thread") or ""
            rec = {"t": round(loop.sim_time, 2), "clock": clock, **beat}
            if op == "announce":
                loop._record_announcements({"events": [{"type": "announce", "text": beat["text"]}]})
                self.note(tid, f"Everyone heard: {beat['text']}", clock, loop.sim_time)
            elif op == "whisper":
                state = loop.get_agent_state(beat["to"])
                if state is None:
                    continue
                prio = URGENT_PRIORITY if beat.get("urgent") else WHISPER_PRIORITY
                loop._push_triggers(state, [Trigger("event", "story", beat["text"], prio, loop.sim_time)])
                loop._log(state, "story", beat["text"], urgent=bool(beat.get("urgent")))
                try:
                    set_concern(world, beat["to"], beat["text"])  # 挂在心上，说话时会带出来
                except Exception:  # noqa: BLE001
                    pass
                self.note(tid, f"{beat['to']} heard: {beat['text']}", clock, loop.sim_time)
            elif op in ("talk", "ask"):
                who = beat["who"]
                state = loop.get_agent_state(who)
                to = beat.get("to") if op == "talk" else player
                if state is None or not to or to == who:
                    continue
                ask = None
                if op == "ask":
                    ask = {"item": beat["item"], "why": beat["why"], "reward": beat.get("reward") or ""}
                    topic = f"ask {to} for {beat['item']}: {beat['why']}"
                else:
                    topic = beat["topic"]
                try:
                    set_errand(world, who, {"to": to, "topic": topic, "thread": tid, "ask": ask,
                                            "set_at": loop.sim_time})
                except Exception:  # noqa: BLE001
                    continue
                text = f"You want to talk to {to}: {topic}"
                loop._push_triggers(state, [Trigger("event", "story", text, TALK_PRIORITY, loop.sim_time)])
                loop._log(state, "story", text)
                rec["to"] = to
                self.note(tid, f"{who} means to {'ask' if ask else 'talk to'} {to}: {topic}", clock, loop.sim_time)
            elif op == "adjust":
                cur = get_property(world, beat["item"], "Item", beat["prop"])
                if not isinstance(cur, (int, float)):
                    continue
                new = max(0, cur + beat["delta"])
                new = int(new) if isinstance(cur, int) else round(float(new), 2)
                set_property(world, beat["item"], "Item", beat["prop"], new)
                rec.update({"from": cur, "to": new})
            else:
                continue
            applied.append(rec)
            if loop.verbose:
                print(f"[STORY] {op}: {beat}")
        self.beats.extend(applied)
        del self.beats[:-50]
        return applied

    # ----- 对话推动剧情 -----

    def on_conversation(self, loop: "GameLoop", a: str, b: str, location: str, lines: List[Dict[str, str]]) -> None:
        """一段对话说完：差事办成了就记进线索（模型没把求助写进台词也照样落成人情）；
        玩家和线索里的人说的话也算进展。"""
        from routine import format_clock

        world = loop.world
        clock = format_clock(loop.sim_time)
        said = " / ".join(f"{ln.get('speaker')}: \"{str(ln.get('text') or '')[:140]}\"" for ln in lines[:3])
        noted = set()
        for speaker, listener in ((a, b), (b, a)):
            errand = read_errand(world, speaker)
            if not errand or errand.get("to") != listener:
                continue
            tid = errand.get("thread") or ""
            self.note(tid, f"{speaker} talked to {listener}: {said}", clock, loop.sim_time)
            noted.add(tid)
            ask = errand.get("ask") or {}
            spoke = any(ln.get("speaker") == speaker for ln in lines)
            if ask.get("item") and spoke:
                try:
                    if not _has_open_favor(world, speaker, listener, ask["item"]):
                        from inner_life import register_favor

                        quest = register_favor(world, speaker, listener, ask, location=location)
                        if quest and tid:
                            world.run_query("MATCH (q:Quest {name: $q}) SET q.thread = $t", q=quest, t=tid)
                        if quest:
                            self.note(tid, f"{speaker} asked {listener} for {ask['item']}.", clock, loop.sim_time)
                except Exception:  # noqa: BLE001
                    pass
            try:
                clear_errand(world, speaker)
            except Exception:  # noqa: BLE001
                pass
        state_a, state_b = loop.get_agent_state(a), loop.get_agent_state(b)
        player = a if state_a is not None and loop._is_player(state_a) else (
            b if state_b is not None and loop._is_player(state_b) else "")
        if not player:
            return
        other = b if player == a else a
        for th in self.open_threads():
            if th["id"] not in noted and other in (th.get("people") or []):
                self.note(th["id"], f"{a} and {b} talked: {said}", clock, loop.sim_time)

    # ----- 线索写成任务线 -----

    def tick_questlines(self, loop: "GameLoop") -> None:
        """线索攒够了进展、玩家也卷进来了：后台请任务设计师写任务线，写好验证过就放进世界（主线程）。"""
        if not AUTO_QUESTLINES:
            return
        from routine import format_clock

        fut = self._quest_future
        if fut is not None:
            if not fut.done():
                return
            self._quest_future = None
            th = self.thread(self._quest_thread)
            try:
                result = fut.result()
            except Exception as e:  # noqa: BLE001
                result = None
                if loop.verbose:
                    print(f"[WARN] story questline: {e}")
            clock = format_clock(loop.sim_time)
            if result is not None and getattr(result, "ok", False):
                from quest import apply_questline

                apply_questline(loop.world, result.patch)
                title = getattr(result.patch, "title", "") or getattr(result.patch, "name", "") or "a new questline"
                if th is not None:
                    th["questline"] = title
                    self.note(th["id"], f"It became a questline: {title}", clock, loop.sim_time)
                loop._record_announcements({"events": [{"type": "announce", "text": f"A new story takes shape: {title}"}]})
            elif th is not None:
                th["questline"] = "-"  # 没写成：这条线索不再重试
            return
        if loop.force_mock or not self.use_llm:
            return
        try:
            from game_loop import _llm_configured

            if not _llm_configured():
                return
        except Exception:  # noqa: BLE001
            return
        if loop.sim_time - self.last_questline_at < _hours(QUESTLINE_GAP_HOURS):
            return
        player = next((s.agent.name for s in loop.agents if loop._is_player(s)), "")
        if not player:
            return
        for th in self.open_threads():
            notes = th.get("notes") or []
            if th.get("questline") or len(notes) < MATURE_NOTES:
                continue
            if player not in (th.get("people") or []) and not any(player in n.get("text", "") for n in notes):
                continue
            self.last_questline_at = loop.sim_time
            self._quest_thread = th["id"]
            th["questline"] = "(being written)"
            theme = (f"{th['title']}. Stakes: {th.get('stakes') or '?'}. What has happened so far, mostly in "
                     f"conversations: " + " | ".join(n.get("text", "") for n in notes[-6:]))[:1200]
            from quest import draft_questline

            if self._quest_pool is None:
                self._quest_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="story-quest")
            self._quest_future = self._quest_pool.submit(draft_questline, loop.world, theme, story_arc=self.arc)
            return


def _world_story_signals(world, npcs: List[str], now: float) -> Dict[str, Any]:
    """剧情素材：势力大事、欠的保护费、谁身无分文、心愿、人情、强烈的好恶、谁正带着差事。每样单独兜底。"""
    out: Dict[str, Any] = {"faction_news": [], "owed": [], "broke": [], "hearts": [], "favors": [],
                           "feelings": [], "errands": [], "favor_items": []}
    try:
        from faction_sim import recent_events

        out["faction_news"] = [
            {"t": e.get("t"), "day": e.get("day"), "kind": e.get("kind"), "text": e.get("text")}
            for e in reversed(recent_events(world, None, 6))
        ]
    except Exception:  # noqa: BLE001
        pass
    try:
        from street_economy import businesses, cash_of, has_economy

        if has_economy(world):
            for biz in businesses(world):
                fee = float(biz.get("fee") or 0)
                for r in biz.get("rackets") or []:
                    if fee and float(r.get("dues") or 0) >= fee:
                        out["owed"].append({"owner": biz.get("owner"), "business": biz.get("name"),
                                            "location": biz.get("location"), "faction": r.get("faction"),
                                            "dues": float(r.get("dues") or 0), "fee": fee})
            out["owed"].sort(key=lambda o: -o["dues"] / max(1.0, o["fee"]))
            out["broke"] = [n for n in npcs if cash_of(world, n) == 0][:6]
    except Exception:  # noqa: BLE001
        pass
    try:
        from inner_life import load_inner_life

        for n in npcs[:12]:
            inner = load_inner_life(world, n)
            if inner.desire or inner.fear:
                bond = next(iter(inner.bonds), "") if inner.bonds else ""
                out["hearts"].append({"name": n, "desire": inner.desire[:100], "fear": inner.fear[:100], "bond": bond})
    except Exception:  # noqa: BLE001
        pass
    try:
        out["favors"] = [dict(r) for r in world.run_query(
            """
            MATCH (g:Person)-[:GIVES_QUEST]->(q:Quest {kind: 'favor'})
            RETURN g.name AS giver, q.helper AS helper, q.item AS item, coalesce(q.description, '') AS why,
                   toLower(coalesce(q.state, 'active')) AS state, coalesce(q.thread, '') AS thread
            ORDER BY coalesce(q.asked_at, 0) DESC LIMIT 8
            """
        ) or []]
    except Exception:  # noqa: BLE001
        pass
    try:
        out["feelings"] = [dict(r) for r in world.run_query(
            """
            MATCH (a:Person)-[r:FEELS]->(b:Person) WHERE abs(coalesce(r.value, 0)) >= 3
            RETURN a.name AS a, b.name AS b, r.value AS value ORDER BY abs(r.value) DESC LIMIT 8
            """
        ) or []]
    except Exception:  # noqa: BLE001
        pass
    try:
        from inner_life import favor_item_options

        out["favor_items"] = favor_item_options(world)
    except Exception:  # noqa: BLE001
        pass
    for n in npcs:
        errand = read_errand(world, n)
        if not errand:
            continue
        set_at = errand.get("set_at")
        if set_at is not None and now - float(set_at) > _hours(ERRAND_HOURS):
            try:
                clear_errand(world, n)  # 太久没办成：作罢
            except Exception:  # noqa: BLE001
                pass
            continue
        out["errands"].append({"who": n, "to": errand.get("to"), "topic": str(errand.get("topic") or "")[:100]})
    return out


# ---------- prompt / 解析（纯函数，便于测试） ----------

def build_prompt(d: Dict[str, Any]) -> str:
    def lines(rows, fmt, empty="- (none)"):
        out = "\n".join(fmt(r) for r in rows or [])
        return out or empty

    people = lines(d["people"], lambda p: (
        f"- {p['name']}{' (player)' if p.get('player') else ' (background, follows routine; never send)' if p.get('routine') else ''} "
        f"at {p.get('location')}: {p.get('status')}; goal {p.get('goal')} -> {p.get('target')}. {p.get('reason') or ''}"
    ))
    threads = lines(d.get("threads"), lambda t: (
        f"- [{t['id']}] {t.get('title')} (stakes: {t.get('stakes') or '?'}; people: {', '.join(t.get('people') or [])})"
        + (f" -> questline: {t['questline']}" if t.get("questline") else "")
        + "".join(f"\n    . {n}" for n in t.get("notes") or [])
    ), "- (none yet: open one)")
    talks = lines(d.get("talks"), lambda c: f"- {c['a']} -> {c['b']} at {c.get('location')}: " + " / ".join(c.get("lines") or []),
                  "- (nobody has talked yet)")
    news = lines(d.get("faction_news"), lambda e: f"- day {e.get('day')}: {e.get('text')}")
    owed = lines(d.get("owed"), lambda o: f"- {o['owner']}'s {o['business']} owes {o['faction']} ${o['dues']:.0f} (fee ${o['fee']:.0f}/day)")
    hearts = lines(d.get("hearts"), lambda h: f"- {h['name']}: longs for {h.get('desire') or '?'}; fears {h.get('fear') or '?'}")
    favors = lines(d.get("favors"), lambda f: f"- {f['giver']} asked {f['helper']} for {f['item']} ({f['state']}): {f.get('why')}"
                   + (f" [thread {f['thread']}]" if f.get("thread") else ""))
    feelings = lines(d.get("feelings"), lambda f: f"- {f['a']} -> {f['b']}: {f['value']:+}")
    errands = lines(d.get("errands"), lambda e: f"- {e['who']} is on the way to talk to {e['to']}: {e['topic']}")
    items = lines(d.get("items"), lambda i: f"- {i['name']}: {i['props']}")
    conflicts = lines(d.get("conflicts"), lambda c: f"- {c.get('clock')}: {c['culprit']} took {c['item']} from {c['loser']} ({c.get('how')})",
                      "- (none yet)")
    tensions = lines(d.get("tensions"), lambda t: f"- {t['who']} -> {t['toward']}: level {t['level']} ({t.get('reason')})")
    chronicle = "\n".join(f"- {c}" for c in d.get("chronicle") or [] if c) or "- (nothing yet)"
    recent = lines(d.get("recent_beats"), lambda b: f"- {b.get('op')}: {b.get('text') or b.get('topic') or b}")
    player = d.get("player") or "the player"
    broke = ", ".join(d.get("broke") or []) or "nobody"
    favor_items = ", ".join(d.get("favor_items") or []) or "(nothing)"
    return f"""Time: {d['clock']}
Story so far: {d.get('arc') or '(just beginning)'}
The player is {player}.

=== Story threads you are developing ===
{threads}

=== What people said lately ===
{talks}

=== People ===
{people}

=== Who is already on the way to talk to someone ===
{errands}

=== Faction news ===
{news}

=== Money on the street (debts, who is broke: {broke}) ===
{owed}

=== What people long for and fear ===
{hearts}

=== Favors asked ===
{favors}

=== Strong feelings ===
{feelings}

=== Recent conflicts between plans ===
{conflicts}

=== Grudges ===
{tensions}

=== Public news ===
{chronicle}

=== Items with numbers you may adjust ===
{items}

=== Your recent beats (do not repeat) ===
{recent}

Stories move forward through conversations. Choose at most {MAX_BEATS} beats. Tools:
- talk: send ONE person to talk to someone (another person, or {player}) about something that matters to them.
  What they say is written live and becomes part of the thread.
  {{"op": "talk", "who": "<name>", "to": "<name>", "topic": "what they want to raise, in their terms", "thread": "<id>"}}
- ask: send ONE person to ask {player} for help: a thing they need, why, and what they offer. It becomes a favor
  quest once they ask. At most one per round; item must be one of: {favor_items}
  {{"op": "ask", "who": "<name>", "item": "<item>", "why": "...", "reward": "...", "thread": "<id>"}}
- whisper: a private rumour or thought for ONE person; it stays on their mind and colours how they talk.
  {{"op": "whisper", "to": "<name>", "text": "...", "urgent": false, "thread": "<id>"}}
- announce: something everyone notices. {{"op": "announce", "text": "...", "thread": "<id>"}}
- adjust: lower a number on an item listed above. Keep it modest; delta must be negative.
  {{"op": "adjust", "item": "<item>", "prop": "<prop>", "delta": -10, "why": "..."}}
Never send or whisper to {player} (the player decides for themselves); never send background people; do not
send someone who is already on the way. Build on what people just said, on debts, factions, grudges and longings.
Keep 1-{MAX_OPEN_THREADS} threads: open one when something starts, add a note when it moves, resolve it when it is over.
Tag beats with the thread they serve. Vary who you use; each beat should add something new.

Respond with JSON only:
{{"arc": "1-2 sentences: where the story is heading now",
  "threads": [{{"id": "CamelCaseId", "title": "...", "stakes": "what is at risk", "people": ["..."], "status": "open|resolved", "note": "what just changed"}}],
  "beats": [ ... ]}}"""


def parse_plan(raw: str, digest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """LLM 文本 → 校验过的 {arc, threads, beats}；不合法的丢弃，全都不合法返回 None。"""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # 背景 NPC 只照日程，悄悄话 / 差事对他们没用：只能交给 LLM 角色
    npcs = {p["name"] for p in digest["people"] if not p.get("player") and not p.get("routine")}
    everyone = {p["name"] for p in digest["people"]}
    items = {i["name"]: i["props"] for i in digest.get("items") or []}
    favor_items = {str(i).lower(): str(i) for i in digest.get("favor_items") or []}
    busy = {e.get("who") for e in digest.get("errands") or []}
    # 上一轮刚用过的：不许原样再来（LLM 很容易每次都私下找同一个人）
    last = _last_round(digest.get("recent_beats") or [])
    used_whisper = {b.get("to") for b in last if b.get("op") == "whisper"}
    used_send = {b.get("who") for b in last if b.get("op") in ("talk", "ask")}
    used_adjust = {(b.get("item"), b.get("prop")) for b in last if b.get("op") == "adjust"}
    beats: List[Dict[str, Any]] = []
    asks = 0
    for b in data.get("beats") or []:
        if not isinstance(b, dict) or len(beats) >= MAX_BEATS:
            continue
        clean = _validate_beat(b, npcs, items, everyone=everyone, favor_items=favor_items)
        if not clean:
            continue
        if clean["op"] == "whisper" and clean["to"] in used_whisper:
            continue
        if clean["op"] == "adjust" and (clean["item"], clean["prop"]) in used_adjust:
            continue
        if clean["op"] in ("talk", "ask"):
            if clean["who"] in busy or clean["who"] in used_send:
                continue
            if clean["op"] == "ask":
                if asks:
                    continue
                asks += 1
            busy.add(clean["who"])
        beats.append(clean)
    threads = [t for t in (_validate_thread(t, everyone) for t in (data.get("threads") or [])[:4]) if t]
    arc = str(data.get("arc") or "").strip()[:MAX_TEXT]
    if not beats and not arc and not threads:
        return None
    return {"arc": arc, "beats": beats, "threads": threads}


def _last_round(recent: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not recent:
        return []
    t = max(b.get("t", 0) for b in recent)
    return [b for b in recent if b.get("t", 0) == t]


def _clean_thread_ref(value: Any) -> str:
    return thread_id(str(value or ""))


def _validate_thread(t: Any, everyone: set) -> Optional[Dict[str, Any]]:
    if not isinstance(t, dict):
        return None
    title = str(t.get("title") or "").strip()[:80]
    tid = _clean_thread_ref(t.get("id") or title)
    if not tid:
        return None
    status = t.get("status") if t.get("status") in ("open", "resolved") else "open"
    people = [p for p in (t.get("people") or []) if isinstance(p, str) and p in everyone][:6]
    return {"id": tid, "title": title, "stakes": str(t.get("stakes") or "").strip()[:160], "people": people,
            "status": status, "note": str(t.get("note") or "").strip()[:MAX_TEXT]}


def _validate_beat(b: Dict[str, Any], npcs: set, items: Dict[str, Dict[str, Any]], *,
                   everyone: Optional[set] = None, favor_items: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    op = b.get("op")
    text = str(b.get("text") or "").strip()[:MAX_TEXT]
    tag = {"thread": _clean_thread_ref(b.get("thread"))} if b.get("thread") else {}
    if op == "announce" and text:
        return {"op": "announce", "text": text, **tag}
    if op == "whisper" and text and b.get("to") in npcs:
        return {"op": "whisper", "to": b["to"], "text": text, "urgent": bool(b.get("urgent")), **tag}
    if op == "talk":
        topic = str(b.get("topic") or b.get("text") or "").strip()[:MAX_TEXT]
        to = b.get("to")
        if topic and b.get("who") in npcs and to in (everyone or set()) and to != b.get("who"):
            return {"op": "talk", "who": b["who"], "to": to, "topic": topic, **tag}
        return None
    if op == "ask":
        item = (favor_items or {}).get(str(b.get("item") or "").lower())
        why = str(b.get("why") or "").strip()[:MAX_TEXT]
        if item and why and b.get("who") in npcs:
            return {"op": "ask", "who": b["who"], "item": item, "why": why,
                    "reward": str(b.get("reward") or "").strip()[:120], **tag}
        return None
    if op == "adjust":
        props = items.get(b.get("item")) or {}
        prop = b.get("prop")
        try:
            delta = float(b.get("delta"))
        except (TypeError, ValueError):
            return None
        if prop not in props or delta == 0:
            return None
        from content import get_content

        if delta > 0 and get_content().conservation:
            return None  # 守恒：剧情只能消耗（风雨、损耗），不能凭空添东西
        cur = float(props[prop])
        cap = max(10.0, abs(cur) * 0.5)
        delta = max(-cap, min(cap, delta))
        if isinstance(props[prop], int):
            delta = int(round(delta))
        return {"op": "adjust", "item": b["item"], "prop": prop, "delta": delta,
                "why": str(b.get("why") or "")[:120]}
    return None
