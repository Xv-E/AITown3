"""
让 LLM 为现有世界写一条任务线：生成 → 校验 → PDDL 验证可完成 → 有问题就把原因回给它改。

模型调用（llm）和验证（verify_fn）都由调用方传入，这里不碰网络和数据库。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

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

Rules: CamelCase names, new names must not exist yet; values are plain JSON (no expressions);
places are relations, never a "location" property; new regions sit next to an existing region;
"archetype" is optional and must be one of the listed archetype names (otherwise leave it out and
use "item_kind"); "giver" is just a person's name; every stage needs the player to DO something;
3-5 stages."""


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
    from content import get_content

    ask = f"Theme from the designer: {theme}" if theme.strip() else "Pick a theme that fits this world."
    package = f"\n\n{COMPLETE}" if complete else ""
    if get_content().conservation:
        package += f"\n\n{CONSERVATION}"
    return f"""{feedback}{render_digest(digest)}

{ask}{package}
Write a questline of {MIN_STAGES + 1}-{min(5, MAX_STAGES)} stages for the player ({digest.player}).

{VOCAB}

{SCHEMA}"""


def parse(raw: str) -> Optional[QuestlinePatch]:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return QuestlinePatch.from_dict(data) if isinstance(data, dict) else None


def author_questline(
    digest: WorldDigest,
    theme: str,
    *,
    llm: LLM,
    verify_fn: VerifyFn,
    max_attempts: int = 4,
    complete: bool = False,
) -> AuthorResult:
    known = digest.known()
    fixes: List[str] = []
    feedback = ""
    problems: List[str] = []
    patch: Optional[QuestlinePatch] = None
    verification: Optional[Verification] = None
    raw = ""
    for attempt in range(1, max_attempts + 1):
        raw = llm(SYSTEM, build_prompt(digest, theme, feedback, complete=complete))
        patch = parse(raw)
        if patch is None:
            problems = ["the answer was not a JSON object"]
        else:
            fixes = normalize(patch, known)
            problems = validate(patch, known) + (completeness_problems(patch) if complete else [])
            if not problems:
                verification = verify_fn(patch)
                if verification.ok:
                    return AuthorResult(patch, verification, attempt, [], raw, fixes)
                problems = [verification.reason]
        # 带上它上一版：只改出问题的地方，不要整条重写（重写会冒出新错误，收敛不了）
        previous = json.dumps(patch.to_dict(), ensure_ascii=False) if patch else (raw or "")[:6000]
        feedback = (
            f"Your previous answer:\n{previous}\n\n"
            "It was rejected for these reasons:\n- "
            + "\n- ".join(problems[:8])
            + "\nKeep the story and everything that was fine; change only what is needed to fix these, "
            "and return the complete corrected JSON.\n\n"
        )
    return AuthorResult(patch, verification, max_attempts, problems, raw, fixes)
