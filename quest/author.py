"""
让 LLM 为现有世界写一条任务线：生成 → 校验 → PDDL 验证可完成 → 有问题就把原因回给它改。

分两段写，每段的输出都短、改错时不牵连另一段：
  1. 骨架：新地点 / 人物 / 物品 / 线索 / 阶段，验证能玩通后定下来；
  2. 活起来：玩家怎么接下（start）和监听器（listeners），在定下的骨架上补，只重写这一段。
每段都保留问题最少的那一版：重写变差了，就拿更好的那版回去改，最后也交出最好的那版。

模型调用（llm）和验证（verify_fn）都由调用方传入，这里不碰网络和数据库。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .digest import WorldDigest
from .spec import MAX_STAGES, MIN_STAGES, QuestlinePatch, completeness_problems, normalize, validate
from .verify import Verification

LLM = Callable[[str, str], str]  # (system, prompt) -> text
VerifyFn = Callable[[QuestlinePatch], Verification]

SYSTEM = """You are the quest designer of a small living settlement where NPCs plan and act on their own.
You add ONE questline to the existing world: a short story the player can actually play through,
built on the people, places and tensions that are already there, plus a few new things.

Good questlines:
- start from something already going on (a grudge, a shortage, someone's goal or secret);
- send the player to talk to people, go somewhere new, find or make something, and bring it back;
- have a turn: a stage that reveals something and changes what the player thought was going on;
- reward each stage with something that matters later (an item, a path opening, a person's trust).

Everything is data, checked by a planner: completion conditions must be things the player can make
true with the world's actions (walking, picking up, giving, talking, using items on things, and the
object actions you write). If you need something to exist, put it in the world or reward it."""

SCHEMA = """Respond with ONE JSON object:
{
  "title": "...", "premise": "1-2 sentences", "hook": "how the player first hears of it",
  "start": {"hear": ["<clue>"], "find": ["<item>"], "visit": ["<region>"], "when": [ <conditions> ],
            "pattern": [ <steps> ], "within_hours": 2, "at": "night", "omen": {"text": "...", "effects": [ ]}},
  "listeners": [ <listeners: the world reacting to what happens, see below> ],
  "regions": [{"name": "OldMine", "next_to": "<existing region>", "description": "...", "tags": "a,b", "w": 18, "h": 14}],
  "persons": [{"name": "Hollis", "located_at": "<region>", "personality": "...", "background": "...",
               "goal": "...", "secret": "...", "mood": "wary", "speech_style": "...",
               "routine": [{"start": 6, "end": 18, "location": "<region>", "activity": "dig for ore"},
                           {"start": 18, "end": 6, "location": "<region>", "activity": "rest"}]}],
  "person_updates": [{"name": "<existing NPC>", "goal": "...", "secret": "..."}],
  "items": [{"name": "MineKey", "located_at": "<region>" OR "held_by": "<NPC>", "item_kind": "key",
             "description": "...", "portable": true, "value": 5, "archetype": "<optional>",
             "actions": [ <object actions, only on fixtures the player interacts with> ]}],
  "clues": [{"name": "MineRumor", "text": "...", "known_by": ["<NPC>"], "kind": "fact" | "rumor"}],
  "stages": [{"id": "s1", "title": "...", "objective": "what the player is told to do",
              "giver": "<person>", "complete_when": [ <conditions> ], "reward": [ <effects> ],
              "news": "what everyone hears when it is done"}]
}"""

VOCAB = """Conditions (complete_when; @player is the player):
  {"op": "has_item", "person": "@player", "item": "MineKey"}           player holds it
  {"op": "has_item", "person": "Hollis", "item": "OldLedger"}           an NPC holds it (deliver it)
  {"op": "person_at", "person": "@player", "location": "OldMine"}       player went there
  {"op": "item_at", "entity": "OldLedger", "location": "CampfireClearing"}
  {"op": "has_rel", "a": "@player", "b": "Hollis", "rel": "KNOWS"}      player talked to them (only if they
                                                                        have never met before)
  {"op": "prop_eq" | "prop_gte" | "prop_lte", "entity": "Campfire", "prop": "fuel", "value": 60}
  {"op": "any", "of": [ ... ]}
Effects (reward; and object action effects):
  {"op": "spawn_item", "name": "Lantern", "item_kind": "tool", "held_by": "@player"}   (or "archetype": ..)
  {"op": "set_prop" | "add_prop", "entity": "...", "prop": "...", "value"/"delta": ...}
  {"op": "link", "a": "OldMine", "b": "Forest", "rel": "ADJACENT_TO", "bidirectional": true}
  {"op": "delete_entity", "entity": "..."}    {"op": "relocate", "entity": "...", "to": "<region>"}
Object actions (on an item, the player uses them on the spot; placeholders @actor @target @here):
  {"id": "pry_open", "label": "Pry the lid open", "requires": [{"op": "has_item", "person": "@actor",
   "item": "Crowbar"}], "effects": [{"op": "spawn_item", "name": "OldLedger", "item_kind": "document",
   "held_by": "@actor"}, {"op": "set_prop", "entity": "@target", "prop": "opened", "value": true}],
   "narrative": "@actor forces the lid; inside lies a ledger."}

How things reach the player:
- Every item a condition or action names must be listed in "items" with where it starts:
  "located_at" a region, or "held_by" a person (the player too). Nothing appears out of nowhere.
- NPCs never hand things over on their own. To give the player an NPC's item, make it the reward of
  the stage where the player talks to them:
  {"op": "unlink", "a": "Hollis", "a_label": "Person", "b": "MineKey", "b_label": "Item", "rel": "HAS_ITEM"},
  {"op": "link", "a": "@player", "a_label": "Person", "b": "MineKey", "b_label": "Item", "rel": "HAS_ITEM"}
- Use spawn_item only for new things no condition refers to by name.

How the player comes across it ("start"): the questline sleeps until ANY way in happens, then
its stages begin. Anything can be a way in - make it feel natural and a little surprising, not a
quest board:
  "hear":    clues the player hears from whoever knows them (one of your clues, known_by an NPC;
             NPCs who know it will look for the player, or let it slip while muttering nearby)
  "find":    things the player picks up - a torn note, a dropped token (an item located_at a region)
  "visit":   places where the player stumbles onto it
  "when":    any conditions (same vocabulary as complete_when), all true at once - e.g. the well's
             "water" drops below 10, the player carries both halves of a medallion
  "pattern": things the player does in order (other things may happen in between):
             {"event": "enter", "what": "<region>"}   {"event": "pick_up" | "drop", "what": "<item>"}
             {"event": "talk", "what": "<NPC>"}        {"event": "do", "what": "<action>", "target": "<thing>"}
             add "times": N to repeat a step. Example - pacing back and forth wakes mysterious footsteps:
             "pattern": [{"event": "enter", "what": "Forest"}, {"event": "enter", "what": "Camp"},
                         {"event": "enter", "what": "Forest"}], "within_hours": 2
Optional for all of them:
  "at":   only counts at this time of day: night, dawn, morning, day, afternoon, dusk, evening, or "22-4"
  "omen": what happens the moment it triggers - a line the player notices, and effects that change the
          world (@player, @here = where the player is), e.g.
          {"text": "Footsteps echo yours... and stop a beat after you do.",
           "effects": [{"op": "relocate", "entity": "MuddyBootprints", "to": "@here"}]}
          (an item an omen moves must be listed in "items" somewhere out of the way)
Give at least two different ways in when you can, so the player doesn't miss it. None of them may
be true already (the player must not start out knowing / holding / standing there).

Listeners ("listeners"): the world reacting to what happens - this is what makes a questline feel
alive. Each listens for something, and when it happens (and its conditions hold) does something:
  {"id": "Footsteps", "active": "dormant",
   "sequence": [{"event": "enter", "who": "@player", "what": "Forest"},
                {"event": "enter", "who": "@player", "what": "Camp"},
                {"event": "enter", "who": "@player", "what": "Forest"}], "within_hours": 2, "at": "night",
   "then": {"say": "Footsteps echo yours... and stop a beat after you do.",
            "effects": [{"op": "relocate", "entity": "MuddyBootprints", "to": "@here"}], "start": true}}
  {"id": "Knocking", "active": "stage:s2", "on": {"event": "enter", "who": "@player", "what": "OldMine"},
   "at": "night", "cooldown_hours": 6, "then": {"say": "Three knocks from deep in the rock."}}
  {"id": "ReedSneaks", "active": "active", "on": {"event": "enter", "who": "Reed", "what": "Storehouse"},
   "then": {"effects": [{"op": "relocate", "entity": "TornGlove", "to": "Storehouse"}],
            "nudge": {"who": "Mara", "text": "You saw Reed slip into the storehouse. Tell @player?"}}}
Fields:
  "active":   when it listens - "dormant" (before the player takes the questline up), "active" (while it
              runs), "stage:<stage id>" (only during that stage), "always"
  "on":       one kind of event, "count": N times (optionally "within_hours"); or "sequence": several in order
              (steps may have "times"); or neither, to fire the moment "when" becomes true
  events:     enter (region) / pick_up, drop (item) / talk (person) / do (action, "target") / learn (clue,
              who @player) / time (night, dawn, morning, day, afternoon, dusk, evening) / hour ("0".."23") /
              new_day / news ("contains": "some words") / stage_done (stage id) / quest_started / listener (id)
  "who":      @player, npc, anyone, or a person's name (who did it)
  "when":     extra conditions (@player, @here = where the player is, @actor = who did it); "at": time of day
  "then":     "say" (a line everyone hears), "effects" (change the world), "start": true (take the
              questline up - only while dormant), "nudge": {"who": <NPC>, "text": what they now feel like doing}
  "once": true to fire only once (default for "start"); "cooldown_hours" between repeats
A listener's effects can also set a flag a stage checks, e.g. effects
[{"op": "set_prop", "entity": "OldMine", "prop": "knocked", "value": true}] and a stage condition
{"op": "prop_eq", "entity": "OldMine", "prop": "knocked", "value": true}.
Use 1-4 listeners; at least one "start" way the player can reach on their own (enter / pick_up / time).

Rules: CamelCase names, new names must not exist yet; values are plain JSON (no expressions);
places are relations, never a "location" property; new regions sit next to an existing region;
"archetype" is optional and must be one of the listed archetype names (otherwise leave it out and
use "item_kind"); "giver" is just a person's name; every stage needs the player to DO something;
stages can't check the time of day (there is no clock in the world) - timing ("at night") belongs to
listeners; "talked to someone" is {"op": "has_rel", "a": "@player", "b": "<NPC>", "rel": "KNOWS"};
3-5 stages."""


# 第一段（骨架）只看条件 / 效果 / 物品动作 / 规则；第二段（活起来）只看接下的途径和监听器
CORE_VOCAB = VOCAB[:VOCAB.index('How the player comes across it ("start")')] + VOCAB[VOCAB.index("Rules:"):]
AWAKEN_VOCAB = VOCAB[VOCAB.index('How the player comes across it ("start")'):VOCAB.index("Rules:")].rstrip()
OPS_VOCAB = VOCAB[:VOCAB.index("Object actions")].rstrip()
CORE_SCHEMA = "\n".join(
    line for line in SCHEMA.splitlines()
    if not line.lstrip().startswith(('"start"', '"pattern"', '"listeners"'))
)
AWAKEN_SCHEMA = """Respond with ONE JSON object:
{
  "start": {"hear": [...], "find": [...], "visit": [...], "when": [...], "pattern": [...], "within_hours": 2,
            "at": "night", "omen": {"text": "...", "effects": [ ]}},
  "listeners": [ <1-4 listeners> ],
  "items": [ only NEW things an omen or listener moves or needs, e.g.
             {"name": "MuddyBootprints", "located_at": "<region, out of the way>", "item_kind": "sign"} ],
  "clues": [ only NEW clues for "hear", {"name": "...", "text": "...", "known_by": ["<NPC>"], "kind": "rumor"} ]
}"""


@dataclass
class AuthorResult:
    patch: Optional[QuestlinePatch]
    verification: Optional[Verification]
    attempts: int
    problems: List[str] = field(default_factory=list)
    raw: str = ""
    # 自动修掉的笔误（不需要模型重写的那种）
    fixes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.patch is not None and self.verification is not None and self.verification.ok


def render_digest(d: WorldDigest) -> str:
    regions = "\n".join(
        f"- {r['name']} (next to {', '.join(sorted(n for n in r.get('next_to') or [] if n)) or 'nothing'}): "
        f"{r.get('description') or ''}"
        for r in d.regions
    )
    persons = "\n".join(
        f"- {p['name']}{' (THE PLAYER)' if p['name'] == d.player else ''} at {p.get('location')}: "
        f"{p.get('personality') or ''}. Goal: {p.get('goal') or '-'}. {p.get('background') or ''}"
        for p in d.persons
    )
    items = "\n".join(
        f"- {i['name']} ({i.get('kind') or 'thing'}{'' if i.get('portable') else ', fixed'}) "
        + (f"held by {i['holder']}" if i.get("holder") else f"at {i.get('location')}")
        for i in d.items
    )
    tensions = "\n".join(
        f"- {t['who']} resents {t['toward']} (level {t['level']}): {t.get('reason') or ''}" for t in d.tensions
    ) or "- none yet"
    quests = "\n".join(f"- {q['name']}: {q.get('objective')} [{q.get('state')}]" for q in d.quests) or "- none"
    archetypes = "\n".join(
        f"- {name}: kind {spec.get('item_kind') or '?'}; {str(spec.get('description') or '')[:60]}"
        for name, spec in sorted(d.archetypes.items())
        if spec.get("portable") is not False
    )
    return f"""=== Regions ===
{regions}

=== People ===
{persons}

=== Things ===
{items}

=== Grudges ===
{tensions}

=== Existing quests ===
{quests}

=== Story so far ===
{d.story_arc or '(nothing notable yet)'}

=== Item archetypes you may spawn ===
{archetypes}"""


COMPLETE = """This questline must bring its own cast and setting (a complete package):
- at least one NEW region where the story happens, next to an existing region;
- 1-3 NEW people who belong to the story, each with personality, goal, a secret and a daily routine
  of 2-4 slots (hours 0-24, a region, an activity) so they live their own lives;
- the NEW items the story needs, each starting somewhere (a region or a person);
- tie it to the existing world: someone already here is involved, or it grows from a grudge/shortage."""


CONSERVATION = """World law: nothing appears from nowhere. Everything is gathered, made or already exists.
- Never use spawn_item. Items the story needs are listed in "items" with where they start.
- A reward hands over something the giver really has: {"op": "unlink", "a": "<giver>", "a_label": "Person",
  "b": "<item>", "b_label": "Item", "rel": "HAS_ITEM"}, {"op": "link", "a": "@player", "a_label": "Person",
  "b": "<item>", "b_label": "Item", "rel": "HAS_ITEM"}
- Or pays from the giver's purse: {"op": "add_prop", "entity": "<giver>", "label": "Person", "prop": "coins",
  "delta": -5}, {"op": "add_prop", "entity": "@player", "label": "Person", "prop": "coins", "delta": 5}"""


def build_prompt(digest: WorldDigest, theme: str, feedback: str = "", *, complete: bool = False) -> str:
    """第一段：骨架（地点 / 人物 / 物品 / 线索 / 阶段）。"""
    from content import get_content

    ask = f"Theme from the designer: {theme}" if theme.strip() else "Pick a theme that fits this world."
    package = f"\n\n{COMPLETE}" if complete else ""
    if get_content().conservation:
        package += f"\n\n{CONSERVATION}"
    return f"""{feedback}{render_digest(digest)}

{ask}{package}
Write a questline of {MIN_STAGES + 1}-{min(5, MAX_STAGES)} stages for the player ({digest.player}).
(How the player comes across it and how the world reacts are added in a second step - leave them out.)

{CORE_VOCAB}

{CORE_SCHEMA}"""


def build_awaken_prompt(digest: WorldDigest, core: QuestlinePatch, feedback: str = "") -> str:
    """第二段：在定下的骨架上补「怎么接下」和监听器。"""
    settled = core.to_dict()
    settled.pop("start", None)
    settled.pop("listeners", None)
    return f"""{feedback}{render_digest(digest)}

This questline is settled and already proven playable - do NOT change it:
{json.dumps(settled, ensure_ascii=False)}

Now bring it to life: decide how the player comes across it ("start") and how the world reacts while it
runs ("listeners"). Use the names above (its regions, people, items, clues, stage ids) and the world's.

{OPS_VOCAB}

{AWAKEN_VOCAB}

{AWAKEN_SCHEMA}"""


def parse(raw: str) -> Optional[QuestlinePatch]:
    data = _json(raw)
    return QuestlinePatch.from_dict(data) if data is not None else None


def _json(raw: str) -> Optional[Dict[str, Any]]:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def merge_awaken(core: QuestlinePatch, data: Dict[str, Any]) -> QuestlinePatch:
    """骨架 + 第二段的回答 -> 完整的任务线（骨架里已有的名字不重复加）。"""
    from .spec import ClueSpec, ItemSpec, _fields, _listener, _start

    out = QuestlinePatch.from_dict(core.to_dict())
    out.start = _start(data.get("start"))
    out.listeners = [_listener(x) for x in data.get("listeners") or [] if isinstance(x, dict)]
    taken = set(out.new_names())
    for key, cls, group in (("items", ItemSpec, out.items), ("clues", ClueSpec, out.clues)):
        for raw in data.get(key) or []:
            if isinstance(raw, dict) and raw.get("name") and raw["name"] not in taken:
                group.append(cls(**{k: v for k, v in raw.items() if k in _fields(cls)}))
                taken.add(raw["name"])
    return out


@dataclass
class _Try:
    patch: Optional[QuestlinePatch]
    problems: List[str]
    raw: str
    fixes: List[str]
    verification: Optional[Verification] = None

    @property
    def score(self) -> float:
        """越小越好：0 = 通过；0.5 = 只差规划器那一关；否则是校验问题的条数。"""
        if self.patch is None:
            return 1e9
        if not self.problems:
            return 0.0
        return 0.5 if self.verification is not None else float(len(self.problems))


def _rounds(
    llm: LLM,
    prompt_for: Callable[[str], str],
    build: Callable[[str], Optional[QuestlinePatch]],
    check: Callable[[QuestlinePatch], _Try],
    max_attempts: int,
    shown: Callable[[QuestlinePatch], str],
) -> Tuple[_Try, int]:
    """写 → 查 → 带着问题重写，保留最好的一版。返回 (最好的那次, 用了几轮)。"""
    best: Optional[_Try] = None
    feedback = ""
    for attempt in range(1, max_attempts + 1):
        raw = llm(SYSTEM, prompt_for(feedback))
        patch = build(raw)
        now = check(patch) if patch is not None else _Try(None, ["the answer was not a JSON object"], raw, [])
        now.raw = raw
        if not now.problems:
            return now, attempt
        worse = best is not None and now.score > best.score
        if best is None or not worse:
            best = now
        # 带上最好的那一版：只改出问题的地方，不要整条重写（重写会冒出新错误，收敛不了）
        previous = shown(best.patch) if best.patch else (best.raw or "")[:6000]
        note = (f"(Your last try was worse - {len(now.problems)} problems. Start again from this better version.)\n"
                if worse else "")
        feedback = (
            f"{note}Your previous answer:\n{previous}\n\n"
            "It was rejected for these reasons:\n- "
            + "\n- ".join(best.problems[:8])
            + "\nKeep the story and everything that was fine; change only what is needed to fix these, "
            "and return the complete corrected JSON.\n\n"
        )
    return best, max_attempts  # type: ignore[return-value]


def author_questline(
    digest: WorldDigest,
    theme: str,
    *,
    llm: LLM,
    verify_fn: VerifyFn,
    max_attempts: int = 4,
    complete: bool = False,
    awaken_attempts: int = 3,
) -> AuthorResult:
    known = digest.known()

    def checked(patch: QuestlinePatch, *, need_start: bool) -> _Try:
        fixes = normalize(patch, known)
        problems = validate(patch, known) + (completeness_problems(patch, need_start=need_start) if complete else [])
        if problems:
            return _Try(patch, problems, "", fixes)
        verification = verify_fn(patch)
        return _Try(patch, [] if verification.ok else [verification.reason], "", fixes, verification)

    def core_only(patch: QuestlinePatch) -> QuestlinePatch:
        patch.start, patch.listeners = type(patch.start)(), []
        return patch

    # 第一段：骨架
    core, used = _rounds(
        llm, lambda fb: build_prompt(digest, theme, fb, complete=complete),
        lambda raw: (lambda p: core_only(p) if p else None)(parse(raw)),
        lambda p: checked(p, need_start=False), max_attempts,
        lambda p: json.dumps(p.to_dict(), ensure_ascii=False),
    )
    if core.problems or core.patch is None:
        return AuthorResult(core.patch, core.verification, used, core.problems, core.raw, core.fixes)

    # 第二段：怎么接下 + 监听器（骨架不动，只重写这一段）
    def shown(p: QuestlinePatch) -> str:
        return json.dumps({"start": p.to_dict()["start"], "listeners": p.to_dict()["listeners"]}, ensure_ascii=False)

    alive, more = _rounds(
        llm, lambda fb: build_awaken_prompt(digest, core.patch, fb),
        lambda raw: (lambda d: merge_awaken(core.patch, d) if d is not None else None)(_json(raw)),
        lambda p: checked(p, need_start=complete), awaken_attempts, shown,
    )
    if not alive.problems:
        return AuthorResult(alive.patch, alive.verification, used + more, [], alive.raw, core.fixes + alive.fixes)

    # 第二段没写成：骨架照样能用 —— 退回最朴素的接下方式（走到它的第一个新地点 / 捡到一样东西）
    fallback = _plain_start(core.patch, known, lambda p: checked(p, need_start=complete))
    if fallback is not None:
        note = (f"couldn't bring the questline to life in {more} tries ({alive.problems[0][:160]}); "
                "it starts plainly instead")
        return AuthorResult(fallback.patch, fallback.verification, used + more, [], alive.raw,
                            core.fixes + [note] + fallback.fixes)
    if not complete:
        return AuthorResult(core.patch, core.verification, used + more, [], core.raw,
                            core.fixes + ["no start or listeners: the questline begins at once"])
    return AuthorResult(alive.patch, alive.verification, used + more, alive.problems, alive.raw,
                        core.fixes + alive.fixes)


def _plain_start(core: QuestlinePatch, known, check: Callable[[QuestlinePatch], _Try]) -> Optional[_Try]:
    tries = [{"visit": [r.name]} for r in core.regions] + [
        {"find": [i.name]} for i in core.items if i.located_at and i.portable]
    for start in tries[:4]:
        candidate = merge_awaken(core, {"start": start})
        result = check(candidate)
        if not result.problems:
            return result
    return None
