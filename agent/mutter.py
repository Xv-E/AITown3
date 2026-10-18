"""
嘀咕：NPC 走在路上、站着干活或发呆时，偶尔自言自语一句，或者只冒一个心情图标（正在跟人说话时不会）。

  mood     只有心情图标（开心 / 生气 / 害怕……，客户端画像素图标）
  chatter  有的没的：大模型写一句（后台线程，不卡模拟）；规则角色 / 没模型时用模板
  leak     说漏嘴：他知道的、休眠任务线要玩家「听说」的那条线索。玩家就在附近就算听到了
           （连 KNOWS_FACT / BELIEVES，任务线随之被接下，见 quest.runtime.came_across）

只通过 GameLoop.hooks 接入（on_tick），结果放在 loop.mutters 给客户端。
"""

from __future__ import annotations

import os
import random
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

MUTTER_LIMIT = 12
# 真实秒：同一个人两句之间、全镇任意两句之间至少隔这么久
PERSON_GAP = float(os.environ.get("AITOWN3_MUTTER_PERSON_GAP", "40"))
GLOBAL_GAP = float(os.environ.get("AITOWN3_MUTTER_GAP", "6"))
# 每次有空档时，挑中的那个人真开口的概率
CHANCE = 0.5
# 开口时：知道要紧事的人说漏嘴的概率；只冒心情图标的概率
LEAK_CHANCE = 0.3
MOOD_ONLY_CHANCE = 0.35
# 同一地点、相距不超过这么多格就算听得见
HEAR_RADIUS = 6
LLM_TIMEOUT = 20.0

# 心情词 -> 图标（客户端按这些关键词画像素图标）
MOOD_WORDS: Dict[str, Tuple[str, ...]] = {
    "happy": ("happy", "cheer", "glad", "content", "hopeful", "proud", "excited", "relieved", "joy", "pleased"),
    "angry": ("angry", "furious", "annoyed", "irritat", "bitter", "resent", "grumpy", "frustrat", "mad"),
    "sad": ("sad", "gloomy", "lonely", "grief", "melanchol", "down", "miserable", "homesick"),
    "scared": ("scared", "afraid", "fear", "nervous", "anxious", "wary", "worried", "uneasy", "paranoid"),
    "tired": ("tired", "weary", "exhaust", "sleepy", "bored", "drained"),
    "love": ("love", "smitten", "fond", "affection", "romantic"),
    "surprised": ("surpris", "shock", "startl", "amazed"),
    "thinking": ("curious", "watchful", "thought", "pensive", "suspicious", "puzzled", "restless", "calm", "neutral"),
}
ICON_EMOJI = {
    "happy": "😊", "angry": "😠", "sad": "😢", "scared": "😨", "tired": "😪", "love": "❤️",
    "surprised": "❗", "thinking": "💭", "hurt": "🤕", "secret": "🤫",
}

TEMPLATES = {
    "happy": ["What a day.", "Hm-hm-hm...", "Things are looking up."],
    "angry": ["Unbelievable...", "If they think I'll let that go...", "Tch."],
    "sad": ["Why does it always go like this...", "Sigh.", "I miss the old days."],
    "scared": ["Did something move over there?", "Keep your head down...", "I don't like this."],
    "tired": ["My feet are killing me.", "Just one more thing, then rest.", "*yawn*"],
    "love": ["I wonder if they noticed me...", "Maybe I'll bring them something."],
    "surprised": ["Huh? That's new.", "Wait, what?"],
    "thinking": ["Now where did I leave it...", "Let me think...", "Hmm."],
    "hurt": ["Ow... that still hurts.", "Need to patch this up."],
}


def mood_icon(mood: str, hp: Optional[float] = None) -> str:
    if hp is not None and hp < 40:
        return "hurt"
    low = (mood or "").lower()
    for icon, words in MOOD_WORDS.items():
        if any(w in low for w in words):
            return icon
    return "thinking"


class Mutterer:
    """GameLoop 每拍回调：挑一个没在跟人说话的人嘀咕一句。"""

    def __init__(self, *, rng: Optional[random.Random] = None, use_llm: bool = True,
                 clock=time.monotonic):
        self.rng = rng or random.Random()
        self.use_llm = use_llm
        self.clock = clock
        self._last_any = -1e9
        self._last: Dict[str, float] = {}
        self._seq = 0
        self._pool: Optional[ThreadPoolExecutor] = None
        self._pending: List[Tuple[Future, Dict[str, Any]]] = []

    # ---------- 每拍 ----------

    def on_tick(self, loop) -> None:
        self._collect(loop)
        now = self.clock()
        if now - self._last_any < GLOBAL_GAP:
            return
        free = [s for s in self._candidates(loop) if now - self._last.get(s.agent.name, -1e9) >= PERSON_GAP]
        if not free or self.rng.random() >= CHANCE:
            return
        state = self.rng.choice(free)
        is_dead = getattr(loop, "_is_dead", None)
        if is_dead is not None and is_dead(state):
            return
        self._last_any = self._last[state.agent.name] = now
        self.mutter(loop, state)

    def mutter(self, loop, state) -> None:
        name = state.agent.name
        info = self._person(loop.world, name)
        icon = mood_icon(info.get("mood") or getattr(state.agent, "mood", ""), info.get("hp"))
        clue = self._leakable_clue(loop.world, name)
        if clue and self.rng.random() < LEAK_CHANCE:
            self._leak(loop, name, clue)
            return
        if self.rng.random() < MOOD_ONLY_CHANCE:
            self._emit(loop, name, "", icon)
            return
        if self.use_llm and self._llm_allowed(loop, state):
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="npc-mutter")
            future = self._pool.submit(_llm_line, name, info, _doing(state), icon)
            self._pending.append((future, {"who": name, "icon": icon, "t0": self.clock()}))
            return
        self._emit(loop, name, self.rng.choice(TEMPLATES.get(icon) or TEMPLATES["thinking"]), icon)

    # ---------- 内部 ----------

    def _candidates(self, loop) -> List[Any]:
        """走路的、站着干活的、发呆的都行；正在跟人说话（或被人叫住等着说话）的不行。"""
        from registry import act

        out = []
        for s in loop.agents:
            if loop._is_player(s):  # noqa: SLF001
                continue
            p = s.pending_action
            if getattr(s, "status", "") == "talking" or getattr(s, "engaged_by", None):
                continue
            if p is not None and p.name == act("converse"):
                continue
            out.append(s)
        return out

    def _llm_allowed(self, loop, state) -> bool:
        if getattr(loop, "force_mock", False) or loop._rule_based(state):  # noqa: SLF001
            return False
        try:
            from model_instances import is_primary_configured

            return is_primary_configured()
        except Exception:  # noqa: BLE001
            return False

    def _collect(self, loop) -> None:
        keep = []
        for future, meta in self._pending:
            if not future.done():
                if self.clock() - meta["t0"] < LLM_TIMEOUT:
                    keep.append((future, meta))
                continue
            try:
                text = future.result()
            except Exception:  # noqa: BLE001
                text = ""
            text = text or self.rng.choice(TEMPLATES.get(meta["icon"]) or TEMPLATES["thinking"])
            self._emit(loop, meta["who"], text, meta["icon"])
        self._pending = keep

    def _emit(self, loop, who: str, text: str, icon: str, *, important: bool = False,
              overheard: bool = False) -> Dict[str, Any]:
        self._seq += 1
        entry = {
            "id": f"m{self._seq}:{who}",
            "t": round(float(getattr(loop, "sim_time", 0.0)), 2),
            "who": who,
            "text": text,
            "icon": icon,
            "emoji": ICON_EMOJI.get(icon, ""),
            "important": important,
            "overheard": overheard,
        }
        feed = getattr(loop, "mutters", None)
        if feed is None:
            feed = loop.mutters = []
        feed.append(entry)
        del feed[:-MUTTER_LIMIT]
        return entry

    def _person(self, world, name: str) -> Dict[str, Any]:
        rows = world.run_query(
            "MATCH (p:Person {name: $n}) RETURN coalesce(p.mood, '') AS mood, p.hp AS hp, "
            "coalesce(p.personality, '') AS personality, coalesce(p.goal, '') AS goal",
            n=name,
        ) or []
        return dict(rows[0]) if rows else {}

    def _leakable_clue(self, world, name: str) -> Optional[Dict[str, Any]]:
        """他知道、而且休眠任务线等着玩家听说的线索。"""
        try:
            from quest.runtime import active_questlines
        except Exception:  # noqa: BLE001
            return None
        wanted = {c for q in active_questlines(world) if q.get("state") == "dormant"
                  for c in (q.get("start") or {}).get("hear") or []}
        if not wanted:
            return None
        rows = world.run_query(
            """
            MATCH (:Person {name: $n})-[:KNOWS_FACT|BELIEVES]->(c)
            WHERE c.name IN $wanted
            RETURN c.name AS name, coalesce(c.text, c.name) AS text, labels(c) AS labels
            """,
            n=name, wanted=sorted(wanted),
        ) or []
        return self.rng.choice(rows) if rows else None

    def _leak(self, loop, name: str, clue: Dict[str, Any]) -> None:
        world = loop.world
        text = f"...{str(clue['text']).rstrip('.')}... no, keep it to yourself."
        player = _player_near(world, name)
        if player:
            rel = "BELIEVES" if "Rumor" in (clue.get("labels") or []) else "KNOWS_FACT"
            label = "Rumor" if rel == "BELIEVES" else "Fact"
            world.link_entities(player, "Person", clue["name"], label, rel)
            memory = getattr(loop, "memory", None)
            if memory is not None:
                try:
                    memory.add(player, f"Overheard {name} muttering: {clue['text']}", type="fact",
                               actor=name, facts=[clue["name"]])
                except Exception:  # noqa: BLE001
                    pass
        self._emit(loop, name, text, "secret", important=True, overheard=bool(player))


def _doing(state) -> str:
    """说给模型听的「此刻在干嘛」。"""
    p = state.pending_action
    if p is None or (p.hold and not p.path):
        return "standing around with nothing in particular to do"
    try:
        from game_loop import _step_text

        step = _step_text((p.name, list(p.args)))
    except Exception:  # noqa: BLE001
        step = f"{p.name} {' '.join(p.args)}"
    return f"on your way to: {step}" if p.path and len(p.path) >= 2 else f"busy with: {step}"


def _player_near(world, name: str) -> Optional[str]:
    rows = world.run_query(
        """
        MATCH (n:Person {name: $n})-[:LOCATED_AT]->(l:Location)<-[:LOCATED_AT]-(p:Person)
        WHERE coalesce(p.is_player, false) OR p.role = 'player'
        RETURN p.name AS player, n.x AS nx, n.y AS ny, p.x AS px, p.y AS py LIMIT 1
        """,
        n=name,
    ) or []
    if not rows:
        return None
    r = rows[0]
    if None in (r.get("nx"), r.get("ny"), r.get("px"), r.get("py")):
        return r["player"]  # 没有格子坐标：同一地点就算
    near = max(abs(r["nx"] - r["px"]), abs(r["ny"] - r["py"])) <= HEAR_RADIUS
    return r["player"] if near else None


def _llm_line(name: str, info: Dict[str, Any], doing: str, icon: str) -> str:
    from llm_router import invoke_primary

    prompt = (
        f"You are {name}. Personality: {info.get('personality') or '-'}. Mood: {info.get('mood') or icon}. "
        f"Goal: {info.get('goal') or '-'}. Right now you are {doing}.\n"
        "Mutter ONE short line to yourself (at most 12 words): a passing thought, a gripe, "
        "something you noticed or are worried about. No quotes, no name prefix, no narration."
    )
    raw = invoke_primary(prompt, max_tokens=40, timeout=LLM_TIMEOUT) or ""
    line = str(raw).strip().strip('"').strip()
    return line.splitlines()[0][:120] if line else ""
