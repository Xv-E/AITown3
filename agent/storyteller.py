"""
Storyteller — 定期读小镇现状，由 LLM 决定故事往哪走，并用少量受控手段推动：

  announce  公开事件：进大事记，所有人收到 news 触发器
  whisper   私下给某个 NPC 的消息（流言、念头、请求）；urgent 会打断其当前计划
  adjust    在限定幅度内改物品数值属性（风暴吹灭火堆、商队补货……），让事件真的影响世界

LLM 输出只经过这三种 op、且逐条校验后才生效；没有 LLM 时用模板推进（规则模式 / 测试）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from game_loop import GameLoop

STORY_HOURS = float(os.environ.get("AITOWN3_STORY_HOURS", "4"))
MAX_BEATS = 2
MAX_TEXT = 220
URGENT_PRIORITY = 0.85
WHISPER_PRIORITY = 0.6
# 数值属性里这些是布局 / 坐标，不给剧情改
_LAYOUT_PROPS = {"x", "y", "w", "h", "map_x", "map_y", "grid_w", "grid_h", "value"}

SYSTEM_PROMPT = (
    "You are the storyteller of a small simulated settlement whose people plan and act on their own. "
    "Each time you are called you nudge the story: create tension, opportunities and consequences that "
    "grow from what is already happening (conflicts, shortages, grudges, goals). Keep it grounded in this "
    "world; never invent people or places. Prefer small, concrete beats over grand ones."
)


@dataclass
class Storyteller:
    interval: float = 0.0  # 模拟秒；0 = 按 STORY_HOURS 换算
    use_llm: bool = True
    arc: str = ""
    beats: List[Dict[str, Any]] = field(default_factory=list)
    last_at: float = 0.0
    _template_turn: int = 0

    def __post_init__(self) -> None:
        if self.interval <= 0:
            from routine import DAY_LENGTH_SECONDS

            self.interval = STORY_HOURS * DAY_LENGTH_SECONDS / 24.0

    # ----- 调度 -----

    def due(self, sim_time: float) -> bool:
        return sim_time - self.last_at >= self.interval

    def mark_started(self, sim_time: float) -> None:
        self.last_at = sim_time

    def to_dict(self, limit: int = 6) -> Dict[str, Any]:
        return {"arc": self.arc, "beats": list(reversed(self.beats[-limit:]))}

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
        return {
            "clock": format_clock(loop.sim_time),
            "sim_time": loop.sim_time,
            "arc": self.arc,
            "people": people,
            "items": items[:16],
            "conflicts": loop.conflicts[-6:],
            "tensions": all_tensions(world, 6),
            "chronicle": [c.get("text") for c in loop.chronicle[-5:]],
            "recent_beats": self.beats[-3:],
        }

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

            raw = invoke_primary(prompt, system=SYSTEM_PROMPT, max_tokens=500, model=creative_model_name())
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
        """无 LLM：从最近的冲突或物品状态里找一个小事件。"""
        self._template_turn += 1
        npcs = [p["name"] for p in digest["people"] if not p.get("player")]
        conflicts = digest.get("conflicts") or []
        fresh = [c for c in conflicts if c.get("t", 0) >= digest["sim_time"] - self.interval]
        if fresh:
            c = fresh[-1]
            return {
                "arc": f"{c['loser']} resents {c['culprit']} over {c['item']}.",
                "beats": [{
                    "op": "whisper",
                    "to": c["loser"],
                    "text": f"You keep thinking about how {c['culprit']} took {c['item']}. "
                            f"Maybe it is time to have a word with {c['culprit']}.",
                    "urgent": False,
                }],
            }
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

        if plan.get("arc"):
            self.arc = str(plan["arc"])[:MAX_TEXT]
        applied: List[Dict[str, Any]] = []
        for beat in plan.get("beats") or []:
            op = beat.get("op")
            rec = {"t": round(loop.sim_time, 2), "clock": format_clock(loop.sim_time), **beat}
            if op == "announce":
                loop._record_announcements({"events": [{"type": "announce", "text": beat["text"]}]})
            elif op == "whisper":
                state = loop.get_agent_state(beat["to"])
                if state is None:
                    continue
                prio = URGENT_PRIORITY if beat.get("urgent") else WHISPER_PRIORITY
                loop._push_triggers(state, [Trigger("event", "story", beat["text"], prio, loop.sim_time)])
                loop._log(state, "story", beat["text"], urgent=bool(beat.get("urgent")))
            elif op == "adjust":
                cur = get_property(loop.world, beat["item"], "Item", beat["prop"])
                if not isinstance(cur, (int, float)):
                    continue
                new = max(0, cur + beat["delta"])
                new = int(new) if isinstance(cur, int) else round(float(new), 2)
                set_property(loop.world, beat["item"], "Item", beat["prop"], new)
                rec.update({"from": cur, "to": new})
            else:
                continue
            applied.append(rec)
            if loop.verbose:
                print(f"[STORY] {op}: {beat}")
        self.beats.extend(applied)
        del self.beats[:-50]
        return applied


# ---------- prompt / 解析（纯函数，便于测试） ----------

def build_prompt(d: Dict[str, Any]) -> str:
    people = "\n".join(
        f"- {p['name']}{' (player)' if p.get('player') else ''} at {p.get('location')}: "
        f"{p.get('status')}; goal {p.get('goal')} -> {p.get('target')}. {p.get('reason') or ''}"
        for p in d["people"]
    )
    items = "\n".join(f"- {i['name']}: {i['props']}" for i in d["items"]) or "- (none)"
    conflicts = "\n".join(
        f"- {c.get('clock')}: {c['culprit']} took {c['item']} from {c['loser']} ({c.get('how')})"
        for c in d["conflicts"]
    ) or "- (none yet)"
    tensions = "\n".join(
        f"- {t['who']} -> {t['toward']}: level {t['level']} ({t.get('reason')})" for t in d["tensions"]
    ) or "- (none)"
    chronicle = "\n".join(f"- {c}" for c in d["chronicle"] if c) or "- (nothing yet)"
    recent = "\n".join(f"- {b.get('op')}: {b.get('text') or b}" for b in d["recent_beats"]) or "- (none)"
    return f"""Time: {d['clock']}
Story so far: {d['arc'] or '(just beginning)'}

=== People ===
{people}

=== Items with numbers you may adjust ===
{items}

=== Recent conflicts between plans ===
{conflicts}

=== Grudges ===
{tensions}

=== Public news ===
{chronicle}

=== Your recent beats (do not repeat) ===
{recent}

Choose at most {MAX_BEATS} beats that move the story forward from here. Tools:
- announce: something everyone notices. {{"op": "announce", "text": "..."}}
- whisper: a private message, rumour or thought for ONE person. Set urgent true only if it should
  make them drop what they are doing. {{"op": "whisper", "to": "<name>", "text": "...", "urgent": false}}
- adjust: change a number on an item listed above (e.g. weather drains fuel). Keep it modest.
  {{"op": "adjust", "item": "<item>", "prop": "<prop>", "delta": -10, "why": "..."}}
Never whisper to the player. Build on grudges and conflicts when there are any.
Vary your beats: do not whisper to the same person as last time, do not adjust the same number twice in
a row, and use announce for things the whole camp would notice. Each beat should add something new.

Respond with JSON only:
{{"arc": "1-2 sentences: where the story is heading now", "beats": [ ... ]}}"""


def parse_plan(raw: str, digest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """LLM 文本 → 校验过的 {arc, beats}；不合法的 beat 丢弃，全都不合法返回 None。"""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    npcs = {p["name"] for p in digest["people"] if not p.get("player")}
    items = {i["name"]: i["props"] for i in digest["items"]}
    # 上一轮刚用过的：不许原样再来（LLM 很容易每次都私下找同一个人）
    last = _last_round(digest.get("recent_beats") or [])
    used_whisper = {b.get("to") for b in last if b.get("op") == "whisper"}
    used_adjust = {(b.get("item"), b.get("prop")) for b in last if b.get("op") == "adjust"}
    beats: List[Dict[str, Any]] = []
    for b in data.get("beats") or []:
        if not isinstance(b, dict) or len(beats) >= MAX_BEATS:
            continue
        clean = _validate_beat(b, npcs, items)
        if not clean:
            continue
        if clean["op"] == "whisper" and clean["to"] in used_whisper:
            continue
        if clean["op"] == "adjust" and (clean["item"], clean["prop"]) in used_adjust:
            continue
        beats.append(clean)
    arc = str(data.get("arc") or "").strip()[:MAX_TEXT]
    if not beats and not arc:
        return None
    return {"arc": arc, "beats": beats}


def _last_round(recent: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not recent:
        return []
    t = max(b.get("t", 0) for b in recent)
    return [b for b in recent if b.get("t", 0) == t]


def _validate_beat(b: Dict[str, Any], npcs: set, items: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    op = b.get("op")
    text = str(b.get("text") or "").strip()[:MAX_TEXT]
    if op == "announce" and text:
        return {"op": "announce", "text": text}
    if op == "whisper" and text and b.get("to") in npcs:
        return {"op": "whisper", "to": b["to"], "text": text, "urgent": bool(b.get("urgent"))}
    if op == "adjust":
        props = items.get(b.get("item")) or {}
        prop = b.get("prop")
        try:
            delta = float(b.get("delta"))
        except (TypeError, ValueError):
            return None
        if prop not in props or delta == 0:
            return None
        cur = float(props[prop])
        cap = max(10.0, abs(cur) * 0.5)
        delta = max(-cap, min(cap, delta))
        if isinstance(props[prop], int):
            delta = int(round(delta))
        return {"op": "adjust", "item": b["item"], "prop": prop, "delta": delta,
                "why": str(b.get("why") or "")[:120]}
    return None
