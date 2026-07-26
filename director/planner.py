"""
导演规划器 — 根据事件描述规划人物/地点/物品/任务蓝图。

流程（多步 LLM，失败再回退模板）：
  1) skeleton  — 标题/前提/卡司名册/任务/主线物/谜题类型
  2) world     — 地点邻接、物品放置、任务骨架（人物仅占位）
  3) cast      — NPC/玩家完整人设、事实传闻、社交关系
  4) mechanics — effects_on × accepts_from（可玩交互）
  → ensure_cast_quality + ensure_playable_mechanics + EventBlueprint.normalized()
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from .blueprint import EventBlueprint
from .validate import (
    ensure_cast_quality,
    ensure_playable_mechanics,
    is_minimally_playable,
)
from .solvability import check_solvable
from .world_loader import list_worlds, load_world, pick_world

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage prompts
# ---------------------------------------------------------------------------

SKELETON_SYSTEM = """You are the Director of a small playable RPG town simulation.
Step 1/4: extract a STORY SKELETON only. Do not invent full map, NPC profiles, or item mechanics yet.

Rules:
- English CamelCase names for entities (TownSquare, StolenLedger, VaultDoor, InnkeeperMara).
- cast MUST include exactly one role=player (Traveler unless user named another).
- cast SHOULD include 1-3 NPCs (role=npc) and usually one antagonist when the plot needs opposition.
- Each cast entry needs a short function (why they matter to the quest).
- One clear quest objective and one macguffin CamelCase item name.
- puzzle_kind must be one of: key_door | deliver | use_on_person | talk_info | custom
- If the draft implies a locked door/vault/gate, use puzzle_kind=key_door and name both key and door.
- talk_info / use_on_person MUST name the informant or target NPC in cast.
- EVERY string field <= 12 words. Output ONLY valid JSON.
"""

SKELETON_SCHEMA = """
Required JSON keys:
title, premise,
cast: [{name, role, function}]  role: player|npc|antagonist,
player_name (must match the cast player),
antagonist_name (empty if none),
npc_names (other npc names; may be derived from cast),
quest: {name, objective, macguffin},
puzzle_kind: key_door|deliver|use_on_person|talk_info|custom,
key_name (string, empty unless key_door),
door_name (string, empty unless key_door),
goal_location (CamelCase place the player ultimately needs),
start_location (CamelCase hub)
"""

WORLD_SYSTEM = """You are the Director of a small playable RPG town simulation.
Step 2/4: expand the FIXED skeleton into map topology, item placement, and a thin quest.
Persons are PLACEHOLDERS only (name/role/located_at). Full NPC profiles come in step 3.

Rules:
- Keep skeleton cast / macguffin / key / door / quest names EXACTLY.
- 3-5 locations (include start_location and goal_location), connected adjacencies.
- For key_door: do NOT put goal_location in open adjacencies; goal is reached via unlocking later.
- persons: one stub per cast member; put them at fitting locations (player at start).
- Items: place macguffin/key/door correctly. Doors: item_kind=door, locked=true, portable=false,
  located_at start-side room, accepts_from:[KeyName], connects_to=goal_location.
  Keys: item_kind=key, portable=true, quest_flag=main_macguffin when it is the macguffin.
- 1 quest; involves must list existing Person/Location/Item names only.
- Leave facts/rumors/social empty ([]) — cast step owns those.
- EVERY string <= 12 words. Output ONLY valid JSON.
"""

WORLD_SCHEMA = """
Required JSON keys:
locations[{name,description,tags}],
adjacencies[{a,b}],
persons[{name,located_at,role}],
items[{name,value,located_at,held_by,owned_by,quest_flag,description,item_kind,locked,portable,
       accepts_from,connects_to,effects_on}],
quests[{name,description,stage,state,involves,giver,objective}],
facts (must be []), rumors (must be []), social (must be [])
role: npc|player|antagonist
item_kind: door|key|tool|"" ; effects_on MUST be [] in this step
"""

CAST_SYSTEM = """You are the Director of a small playable RPG town simulation.
Step 3/4: flesh out the CAST — full NPC/player profiles, knowledge, and social links.
Do NOT rename cast members. Do NOT add/remove locations or items. Do NOT write item effects_on.

Rules:
- Keep every cast name/role from the world stubs EXACTLY.
- Every person needs: personality, background, goal, values, mood, hp, power_level,
  speech_style, secret_policy, trust_toward_player, located_at (must be an existing location).
- Optional: works_at, accepts_from (tool names that may be used ON this person).
- secret_policy: share_gossip | guarded | lie | open
- trust_toward_player: integer -5..10 (antagonist usually <= 1; helpful npc >= 2)
- Player goal should match the quest objective; NPC goals should be local motives.
- 1-3 facts with known_by listing who already knows them (plot-critical info on the right NPC).
- 0-2 rumors with believed_by.
- social: KNOWS between player and each NPC; FRIENDS_WITH/LIKES sparingly.
- For talk_info: at least one fact known_by an NPC but NOT the player.
- For use_on_person: target person.accepts_from must list the tool item name.
- EVERY string <= 12 words. Output ONLY valid JSON.
"""

CAST_SCHEMA = """
JSON shape:
{
  "persons":[{
    "name","located_at","works_at","role",
    "personality","background","goal","values","mood","hp","power_level",
    "speech_style","secret_policy","trust_toward_player","accepts_from"
  }],
  "facts":[{name,text,secret_level,known_by}],
  "rumors":[{name,text,about,believed_by}],
  "social":[{a,rel,b}]
}
rel: KNOWS|FRIENDS_WITH|LIKES
"""

MECHANICS_SYSTEM = """You are the Director of a small playable RPG town simulation.
Step 4/4: add PLAYABLE item mechanics only. Output a JSON object with key "items"
listing ONLY items that need effects_on and/or accepts_from updates.
You may also return "persons" patches that only update accepts_from.

Rules:
- ops whitelist ONLY: set_prop|link|unlink|delete_entity
- preconditions: prop_eq|prop_neq|has_item|person_at|item_at|has_rel|not_has_rel
- placeholders: @actor @tool @target @target_loc
- For key_door: key.effects_on targets the door; door.accepts_from includes the key;
  ops must set_prop locked=false AND link the two Locations with ADJACENT_TO bidirectional.
- Tool use on person: person.accepts_from includes tool; tool.effects_on targets person.
- Do not rename entities. Do not add new locations/persons.
- EVERY string <= 12 words. Output ONLY valid JSON.
"""

MECHANICS_SCHEMA = """
JSON shape:
{"items":[
  {"name":"DoorKey","effects_on":[{"target":"VaultDoor","consume":true,
    "preconditions":[...],"ops":[...]}],"accepts_from":[]},
  {"name":"VaultDoor","accepts_from":["DoorKey"],"effects_on":[]}
],
 "persons":[{"name":"GuardRenn","accepts_from":["BlankWarrant"]}]
}
"""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = text.strip()
    for pat in (
        r"<think>[\s\S]*?</think>",
        r"<thinking>[\s\S]*?</thinking>",
    ):
        text = re.sub(pat, "", text, flags=re.I).strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    match = re.search(r"\{[\s\S]*", text)
    if not match:
        return None
    frag = match.group().strip()
    try:
        data = json.loads(frag)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    repaired = _repair_truncated_object(frag)
    if repaired is not None:
        return repaired
    for i in range(len(frag) - 1, 0, -1):
        if frag[i] != "}":
            continue
        try:
            data = json.loads(frag[: i + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _repair_truncated_object(frag: str) -> Optional[Dict[str, Any]]:
    """尽力闭合被 max_tokens 截断的 JSON 对象。"""
    if not frag.startswith("{"):
        return None
    s = frag.rstrip()
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
    if in_str:
        s += '"'
    s = re.sub(r",\s*$", "", s)
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
    while stack:
        opener = stack.pop()
        s += "}" if opener == "{" else "]"
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _llm_timeout() -> float:
    return float(os.getenv("DIRECTOR_LLM_TIMEOUT", "22"))


def _invoke_json(
    *,
    system: str,
    user: str,
    max_tokens: int,
    stage: str,
) -> Optional[Dict[str, Any]]:
    try:
        from llm_router import primary_available, invoke_primary
    except Exception as e:
        logger.info("Director LLM imports unavailable: %s", e)
        return None
    if not primary_available():
        logger.info("Director primary LLM not configured")
        return None

    raw = ""
    try:
        logger.info("Director stage=%s start", stage)
        raw = invoke_primary(
            user,
            system=system,
            max_tokens=max_tokens,
            timeout=_llm_timeout(),
        )
        text = str(raw)
        logger.info("Director stage=%s raw_len=%s", stage, len(text))
    except Exception as e:
        logger.warning("Director stage=%s LLM failed: %s", stage, e)
        return None

    data = _extract_json(text) if text else None
    if not data:
        logger.warning(
            "Director stage=%s non-JSON (len=%s)",
            stage,
            len(raw or text or ""),
        )
        return None
    return data


def _draft_block(draft: Optional[Dict[str, Any]]) -> str:
    if not draft or not isinstance(draft, dict):
        return ""
    try:
        return (
            "\nUser partial blueprint (JSON). Keep every name/field they specified:\n"
            f"{json.dumps(draft, ensure_ascii=False, indent=2)}\n"
        )
    except (TypeError, ValueError):
        return ""


def _normalize_cast_roster(
    raw: Dict[str, Any],
    *,
    player: str,
    antagonist: str,
    npcs: List[str],
) -> List[Dict[str, str]]:
    cast_raw = raw.get("cast") if isinstance(raw.get("cast"), list) else []
    roster: List[Dict[str, str]] = []
    seen = set()

    def _add(name: str, role: str, function: str = "") -> None:
        name = (name or "").strip()
        if not name or name in seen:
            return
        role_n = (role or "npc").strip().lower()
        if role_n not in ("player", "npc", "antagonist"):
            role_n = "npc"
        seen.add(name)
        roster.append(
            {
                "name": name,
                "role": role_n,
                "function": (function or "").strip() or f"{role_n} in the story",
            }
        )

    for entry in cast_raw:
        if isinstance(entry, dict):
            _add(
                str(entry.get("name") or ""),
                str(entry.get("role") or "npc"),
                str(entry.get("function") or ""),
            )
        elif isinstance(entry, str):
            _add(entry, "npc")

    _add(player, "player", "Player avatar")
    if antagonist:
        _add(antagonist, "antagonist", "Opposes or obstructs the player")
    for n in npcs:
        _add(n, "npc")

    # 保证唯一玩家
    player_hit = False
    for e in roster:
        if e["name"] == player:
            e["role"] = "player"
            player_hit = True
        elif e["role"] == "player":
            e["role"] = "npc"
    if not player_hit:
        roster.insert(0, {"name": player, "role": "player", "function": "Player avatar"})

    # talk/info 类至少要有一个非玩家
    return roster[:5]


def _normalize_skeleton(raw: Dict[str, Any], prompt: str) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    quest = raw.get("quest") if isinstance(raw.get("quest"), dict) else {}
    puzzle = str(raw.get("puzzle_kind") or "custom").strip().lower()
    if puzzle not in ("key_door", "deliver", "use_on_person", "talk_info", "custom"):
        puzzle = "custom"
    title = str(raw.get("title") or "Untitled Event").strip() or "Untitled Event"
    premise = str(raw.get("premise") or prompt).strip()
    player = str(raw.get("player_name") or "Traveler").strip() or "Traveler"
    antagonist = str(raw.get("antagonist_name") or "").strip()
    npcs = [str(x).strip() for x in (raw.get("npc_names") or []) if str(x).strip()]
    mac = str(quest.get("macguffin") or raw.get("macguffin") or "").strip()
    qname = str(quest.get("name") or "MainQuest").strip() or "MainQuest"
    qobj = str(quest.get("objective") or premise).strip()
    key_name = str(raw.get("key_name") or "").strip()
    door_name = str(raw.get("door_name") or "").strip()
    if puzzle == "key_door":
        key_name = key_name or mac or "DoorKey"
        door_name = door_name or "VaultDoor"
        if not mac:
            mac = key_name
    elif not mac:
        mac = "QuestObject"
    start = str(raw.get("start_location") or "TownSquare").strip() or "TownSquare"
    goal = str(raw.get("goal_location") or "").strip()
    if puzzle == "key_door" and not goal:
        goal = "Vault"

    cast = _normalize_cast_roster(
        raw, player=player, antagonist=antagonist, npcs=npcs
    )
    # 从 cast 回填名字列表
    player = next((c["name"] for c in cast if c["role"] == "player"), player)
    antagonist = next(
        (c["name"] for c in cast if c["role"] == "antagonist"), antagonist
    )
    npcs = [c["name"] for c in cast if c["role"] == "npc"]

    # 信息/对人使用谜题：若没有 NPC，补一个默认线人
    if puzzle in ("talk_info", "use_on_person", "deliver") and not npcs and not antagonist:
        informant = "TownInformant"
        cast.append(
            {
                "name": informant,
                "role": "npc",
                "function": "Holds a lead the player needs",
            }
        )
        npcs = [informant]

    return {
        "title": title,
        "premise": premise,
        "player_name": player,
        "antagonist_name": antagonist,
        "npc_names": npcs[:3],
        "cast": cast,
        "quest": {"name": qname, "objective": qobj, "macguffin": mac},
        "puzzle_kind": puzzle,
        "key_name": key_name,
        "door_name": door_name,
        "goal_location": goal,
        "start_location": start,
    }


def _stage_skeleton(
    prompt: str,
    draft: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    user = (
        "Extract a story skeleton JSON from this draft.\n\n"
        f"Draft:\n{prompt.strip()}\n"
        f"{_draft_block(draft)}\n"
        f"{SKELETON_SCHEMA}\n"
        "JSON:"
    )
    data = _invoke_json(
        system=SKELETON_SYSTEM,
        user=user,
        max_tokens=550,
        stage="skeleton",
    )
    if not data:
        return None
    return _normalize_skeleton(data, prompt)


def _inject_skeleton_constraints(world: Dict[str, Any], sk: Dict[str, Any]) -> Dict[str, Any]:
    """强制骨架命名进入世界稿，避免第二步改名。"""
    out = dict(world)
    out["title"] = sk["title"]
    out["premise"] = sk["premise"]

    start = sk["start_location"]
    goal = sk.get("goal_location") or ""
    locs = [x for x in (out.get("locations") or []) if isinstance(x, dict)]
    loc_names = {str(x.get("name", "")) for x in locs}
    if start and start not in loc_names:
        locs.insert(0, {"name": start, "description": "Starting hub.", "tags": "hub"})
        loc_names.add(start)
    if goal and goal not in loc_names:
        locs.append({"name": goal, "description": "Goal area.", "tags": "goal,secret"})
        loc_names.add(goal)
    out["locations"] = locs

    # persons — 按 cast 名册强制占位；完整人设留给 cast 阶段
    persons = [x for x in (out.get("persons") or []) if isinstance(x, dict)]
    by_p = {str(x.get("name", "")): x for x in persons}
    loc_list = [str(x.get("name", "")) for x in locs if x.get("name")]
    start = sk["start_location"]

    def _pick_loc_for_role(role: str, hint: str = "") -> str:
        if hint and hint in loc_names:
            return hint
        if role == "player":
            return start
        # 非目标地点优先
        for n in loc_list:
            if n != sk.get("goal_location"):
                return n
        return start if start in loc_names else (loc_list[0] if loc_list else start)

    def _ensure_person(name: str, role: str, function: str = "") -> None:
        if not name:
            return
        if name in by_p:
            cur = by_p[name]
            if role == "player":
                cur["role"] = "player"
            else:
                cur["role"] = role if role == "antagonist" else (cur.get("role") or role)
            if not cur.get("located_at") or cur.get("located_at") not in loc_names:
                cur["located_at"] = _pick_loc_for_role(role, str(cur.get("located_at") or ""))
            if function and not cur.get("background"):
                cur["background"] = function
            return
        by_p[name] = {
            "name": name,
            "located_at": _pick_loc_for_role(role),
            "role": role,
            "personality": "",
            "background": function or "Town resident.",
            "goal": sk["quest"]["objective"] if role == "player" else "",
            "values": "",
            "mood": "neutral",
            "hp": 100,
            "power_level": 10,
            "speech_style": "",
            "secret_policy": "share_gossip",
            "trust_toward_player": 0,
            "accepts_from": [],
        }

    cast = sk.get("cast") if isinstance(sk.get("cast"), list) else []
    if cast:
        for entry in cast:
            if not isinstance(entry, dict):
                continue
            _ensure_person(
                str(entry.get("name") or ""),
                str(entry.get("role") or "npc"),
                str(entry.get("function") or ""),
            )
    else:
        _ensure_person(sk["player_name"], "player", "Player avatar")
        if sk.get("antagonist_name"):
            _ensure_person(sk["antagonist_name"], "antagonist")
        for n in sk.get("npc_names") or []:
            _ensure_person(n, "npc")

    for name, p in by_p.items():
        if name != sk["player_name"] and str(p.get("role", "")).lower() == "player":
            p["role"] = "npc"
    out["persons"] = list(by_p.values())

    # items for key_door / macguffin
    items = [x for x in (out.get("items") or []) if isinstance(x, dict)]
    by_i = {str(x.get("name", "")): x for x in items}
    mac = sk["quest"]["macguffin"]
    puzzle = sk["puzzle_kind"]

    if puzzle == "key_door":
        key_name = sk["key_name"] or mac
        door_name = sk["door_name"] or "VaultDoor"
        if key_name not in by_i:
            by_i[key_name] = {
                "name": key_name,
                "value": 50,
                "located_at": start,
                "quest_flag": "main_macguffin",
                "item_kind": "key",
                "portable": True,
                "description": f"Opens {door_name}.",
                "effects_on": [],
                "accepts_from": [],
            }
        else:
            by_i[key_name].setdefault("item_kind", "key")
            by_i[key_name].setdefault("quest_flag", "main_macguffin")
            by_i[key_name]["effects_on"] = by_i[key_name].get("effects_on") or []
        if door_name not in by_i:
            by_i[door_name] = {
                "name": door_name,
                "value": 0,
                "located_at": start,
                "item_kind": "door",
                "locked": True,
                "portable": False,
                "accepts_from": [key_name],
                "connects_to": goal or "",
                "description": f"Locked door. Accepts {key_name}.",
                "effects_on": [],
            }
        else:
            d = by_i[door_name]
            d["item_kind"] = "door"
            d["locked"] = True
            d["portable"] = False
            accepts = list(d.get("accepts_from") or [])
            if key_name not in accepts:
                accepts.append(key_name)
            d["accepts_from"] = accepts
            if goal and not d.get("connects_to"):
                d["connects_to"] = goal
            d["effects_on"] = d.get("effects_on") or []
    elif mac and mac not in by_i:
        by_i[mac] = {
            "name": mac,
            "value": 100,
            "located_at": start,
            "quest_flag": "main_macguffin",
            "description": "Central quest object.",
            "effects_on": [],
            "accepts_from": [],
        }
    # strip mechanics from this stage (mechanics stage / ensure will fill)
    for it in by_i.values():
        if not isinstance(it.get("effects_on"), list):
            it["effects_on"] = []
    out["items"] = list(by_i.values())

    # quest
    involves = [sk["player_name"], mac]
    if puzzle == "key_door":
        involves = [sk["key_name"] or mac, sk["door_name"] or "VaultDoor", sk["player_name"]]
        if goal:
            involves.append(goal)
    if sk.get("antagonist_name"):
        involves.append(sk["antagonist_name"])
    quests = [x for x in (out.get("quests") or []) if isinstance(x, dict)]
    q0 = quests[0] if quests else {}
    out["quests"] = [
        {
            "name": sk["quest"]["name"],
            "description": q0.get("description") or sk["quest"]["objective"],
            "stage": q0.get("stage") or "intro",
            "state": "active",
            "involves": involves,
            "giver": q0.get("giver"),
            "objective": sk["quest"]["objective"],
        }
    ]

    # key_door: remove open adjacency into goal
    if puzzle == "key_door" and goal:
        edges = []
        for e in out.get("adjacencies") or []:
            if not isinstance(e, dict):
                continue
            a, b = str(e.get("a", "")), str(e.get("b", ""))
            if goal in (a, b) and not e.get("locked"):
                continue
            edges.append(e)
        out["adjacencies"] = edges

    out.setdefault("facts", out.get("facts") or [])
    out.setdefault("rumors", out.get("rumors") or [])
    out.setdefault("social", out.get("social") or [])
    out.setdefault("adjacencies", out.get("adjacencies") or [])
    return out


def _stage_world(
    prompt: str,
    skeleton: Dict[str, Any],
    draft: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    user = (
        "Expand this FIXED skeleton into world JSON (no effects_on yet).\n\n"
        f"Original draft:\n{prompt.strip()}\n\n"
        f"Skeleton (DO NOT rename):\n{json.dumps(skeleton, ensure_ascii=False, indent=2)}\n"
        f"{_draft_block(draft)}\n"
        f"{WORLD_SCHEMA}\n"
        "JSON:"
    )
    data = _invoke_json(
        system=WORLD_SYSTEM,
        user=user,
        max_tokens=1100,
        stage="world",
    )
    if not data:
        return None
    if not data.get("locations") or not data.get("persons"):
        logger.warning("Director world stage missing locations/persons")
        return None
    return _inject_skeleton_constraints(data, skeleton)


def _apply_cast_patch(
    world: Dict[str, Any],
    patch: Dict[str, Any],
    skeleton: Dict[str, Any],
) -> Dict[str, Any]:
    """合并 cast 阶段的人设/知识/社交；不允许改名或删人。"""
    out = dict(world)
    loc_names = {
        str(x.get("name", ""))
        for x in (out.get("locations") or [])
        if isinstance(x, dict) and x.get("name")
    }
    persons = [dict(x) for x in (out.get("persons") or []) if isinstance(x, dict)]
    by_p = {str(x.get("name", "")): x for x in persons}
    allowed = set(by_p.keys())

    for upd in patch.get("persons") or []:
        if not isinstance(upd, dict):
            continue
        name = str(upd.get("name", "")).strip()
        if name not in allowed:
            continue
        cur = by_p[name]
        for key in (
            "personality",
            "background",
            "goal",
            "values",
            "mood",
            "speech_style",
            "secret_policy",
            "parent_goal",
        ):
            if key in upd and upd[key] is not None and str(upd[key]).strip():
                cur[key] = str(upd[key]).strip()
        if "hp" in upd and upd["hp"] is not None:
            try:
                cur["hp"] = int(upd["hp"])
            except (TypeError, ValueError):
                pass
        if "power_level" in upd and upd["power_level"] is not None:
            try:
                cur["power_level"] = int(upd["power_level"])
            except (TypeError, ValueError):
                pass
        if "trust_toward_player" in upd and upd["trust_toward_player"] is not None:
            try:
                cur["trust_toward_player"] = int(upd["trust_toward_player"])
            except (TypeError, ValueError):
                pass
        if "located_at" in upd and str(upd.get("located_at") or "") in loc_names:
            cur["located_at"] = str(upd["located_at"])
        if "works_at" in upd:
            w = str(upd.get("works_at") or "")
            cur["works_at"] = w if w in loc_names else None
        if "accepts_from" in upd and isinstance(upd["accepts_from"], list):
            cur["accepts_from"] = [str(x) for x in upd["accepts_from"] if x]
        # role 以骨架为准
        role_sk = next(
            (
                str(c.get("role"))
                for c in (skeleton.get("cast") or [])
                if isinstance(c, dict) and str(c.get("name")) == name
            ),
            None,
        )
        if role_sk:
            cur["role"] = role_sk
        elif name == skeleton.get("player_name"):
            cur["role"] = "player"

    out["persons"] = list(by_p.values())

    if isinstance(patch.get("facts"), list):
        out["facts"] = [x for x in patch["facts"] if isinstance(x, dict)]
    if isinstance(patch.get("rumors"), list):
        out["rumors"] = [x for x in patch["rumors"] if isinstance(x, dict)]
    if isinstance(patch.get("social"), list):
        out["social"] = list(patch["social"])
    return out


def _stage_cast(
    prompt: str,
    world: Dict[str, Any],
    skeleton: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    compact = {
        "title": world.get("title"),
        "premise": world.get("premise"),
        "puzzle_kind": skeleton.get("puzzle_kind"),
        "quest": skeleton.get("quest"),
        "cast": skeleton.get("cast"),
        "locations": [
            {"name": x.get("name"), "tags": x.get("tags")}
            for x in (world.get("locations") or [])
            if isinstance(x, dict)
        ],
        "persons": [
            {
                "name": p.get("name"),
                "role": p.get("role"),
                "located_at": p.get("located_at"),
                "background": p.get("background") or "",
            }
            for p in (world.get("persons") or [])
            if isinstance(p, dict)
        ],
        "items": [
            {
                "name": i.get("name"),
                "item_kind": i.get("item_kind"),
                "quest_flag": i.get("quest_flag"),
                "held_by": i.get("held_by"),
                "located_at": i.get("located_at"),
            }
            for i in (world.get("items") or [])
            if isinstance(i, dict)
        ],
    }
    user = (
        "Flesh out cast profiles, facts, rumors, and social links.\n"
        "Keep every person name/role. Do not invent new people.\n\n"
        f"Draft intent:\n{prompt.strip()}\n\n"
        f"World context:\n{json.dumps(compact, ensure_ascii=False, indent=2)}\n"
        f"{CAST_SCHEMA}\n"
        "JSON:"
    )
    return _invoke_json(
        system=CAST_SYSTEM,
        user=user,
        max_tokens=900,
        stage="cast",
    )


def _apply_mechanics_patch(
    world: Dict[str, Any],
    patch: Dict[str, Any],
) -> Dict[str, Any]:
    out = dict(world)
    items = [dict(x) for x in (out.get("items") or []) if isinstance(x, dict)]
    by_name = {str(x.get("name", "")): x for x in items}
    for upd in patch.get("items") or []:
        if not isinstance(upd, dict):
            continue
        name = str(upd.get("name", "")).strip()
        if not name or name not in by_name:
            continue
        cur = by_name[name]
        if "effects_on" in upd and isinstance(upd["effects_on"], list):
            cur["effects_on"] = upd["effects_on"]
        if "accepts_from" in upd and isinstance(upd["accepts_from"], list):
            cur["accepts_from"] = upd["accepts_from"]
        if "item_kind" in upd and upd["item_kind"]:
            cur["item_kind"] = upd["item_kind"]
        if "locked" in upd:
            cur["locked"] = bool(upd["locked"])
        if "connects_to" in upd and upd["connects_to"]:
            cur["connects_to"] = upd["connects_to"]
    out["items"] = list(by_name.values())

    # 机制阶段可给人补 accepts_from（use_on_person）
    persons = [dict(x) for x in (out.get("persons") or []) if isinstance(x, dict)]
    by_p = {str(x.get("name", "")): x for x in persons}
    for upd in patch.get("persons") or []:
        if not isinstance(upd, dict):
            continue
        name = str(upd.get("name", "")).strip()
        if name not in by_p:
            continue
        if "accepts_from" in upd and isinstance(upd["accepts_from"], list):
            by_p[name]["accepts_from"] = [str(x) for x in upd["accepts_from"] if x]
    out["persons"] = list(by_p.values())
    return out


def _stage_mechanics(
    prompt: str,
    world: Dict[str, Any],
    skeleton: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    # talk_info 可不强制机制；其它类型尽量生成
    compact = {
        "title": world.get("title"),
        "premise": world.get("premise"),
        "puzzle_kind": skeleton.get("puzzle_kind"),
        "locations": world.get("locations"),
        "adjacencies": world.get("adjacencies"),
        "persons": [
            {"name": p.get("name"), "role": p.get("role"), "located_at": p.get("located_at"),
             "accepts_from": p.get("accepts_from") or []}
            for p in (world.get("persons") or []) if isinstance(p, dict)
        ],
        "items": [
            {
                "name": i.get("name"),
                "item_kind": i.get("item_kind"),
                "locked": i.get("locked"),
                "located_at": i.get("located_at"),
                "held_by": i.get("held_by"),
                "accepts_from": i.get("accepts_from") or [],
                "connects_to": i.get("connects_to") or "",
                "quest_flag": i.get("quest_flag") or "",
            }
            for i in (world.get("items") or []) if isinstance(i, dict)
        ],
        "quests": world.get("quests"),
    }
    user = (
        "Add playable mechanics for this world. Return items patch JSON only.\n\n"
        f"Draft intent:\n{prompt.strip()}\n\n"
        f"World summary:\n{json.dumps(compact, ensure_ascii=False, indent=2)}\n"
        f"{MECHANICS_SCHEMA}\n"
        "JSON:"
    )
    data = _invoke_json(
        system=MECHANICS_SYSTEM,
        user=user,
        max_tokens=900,
        stage="mechanics",
    )
    return data


def _fallback_world_from_skeleton(sk: Dict[str, Any]) -> Dict[str, Any]:
    """第二步失败时，用骨架生成最小可玩草稿，再交给机制修补。"""
    start = sk["start_location"]
    goal = sk.get("goal_location") or "Vault"
    locs = [
        {"name": start, "description": "Starting hub.", "tags": "hub"},
    ]
    if sk["puzzle_kind"] == "key_door":
        mid = "Storage" if start != "Storage" else "SideRoom"
        locs.append({"name": mid, "description": "Side room with supplies.", "tags": "item"})
        locs.append({"name": goal, "description": "Sealed goal area.", "tags": "goal,secret"})
        adj = [{"a": start, "b": mid}]
        key_name = sk["key_name"] or sk["quest"]["macguffin"]
        door_name = sk["door_name"] or "VaultDoor"
        items = [
            {
                "name": key_name,
                "value": 50,
                "located_at": mid,
                "quest_flag": "main_macguffin",
                "item_kind": "key",
                "portable": True,
                "description": f"Opens {door_name}.",
                "effects_on": [],
                "accepts_from": [],
            },
            {
                "name": door_name,
                "value": 0,
                "located_at": start,
                "item_kind": "door",
                "locked": True,
                "portable": False,
                "accepts_from": [key_name],
                "connects_to": goal,
                "description": f"Locked door to {goal}.",
                "effects_on": [],
            },
        ]
        involves = [key_name, door_name, sk["player_name"], goal]
    else:
        locs.append({"name": goal if goal != start else "Market", "description": "Second place.", "tags": ""})
        adj = [{"a": start, "b": locs[-1]["name"]}]
        mac = sk["quest"]["macguffin"]
        items = [
            {
                "name": mac,
                "value": 100,
                "located_at": locs[-1]["name"],
                "quest_flag": "main_macguffin",
                "description": "Central quest object.",
                "effects_on": [],
                "accepts_from": [],
            }
        ]
        involves = [mac, sk["player_name"]]

    persons = [
        {
            "name": sk["player_name"],
            "located_at": start,
            "role": "player",
            "personality": "",
            "background": "A traveler drawn into local trouble.",
            "goal": sk["quest"]["objective"],
            "values": "",
            "mood": "determined",
            "hp": 100,
            "power_level": 10,
        }
    ]
    for entry in sk.get("cast") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        role = str(entry.get("role") or "npc")
        if not name or name == sk["player_name"]:
            continue
        persons.append(
            {
                "name": name,
                "located_at": locs[-1]["name"] if role == "antagonist" else start,
                "role": role,
                "personality": "",
                "background": str(entry.get("function") or "Town resident."),
                "goal": "Keep the secret" if role == "antagonist" else "Daily work",
                "values": "",
                "mood": "tense" if role == "antagonist" else "neutral",
                "hp": 100,
                "power_level": 12 if role == "antagonist" else 10,
            }
        )
        if name not in involves:
            involves.append(name)
    if sk.get("antagonist_name") and sk["antagonist_name"] not in {
        p["name"] for p in persons
    }:
        persons.append(
            {
                "name": sk["antagonist_name"],
                "located_at": locs[-1]["name"],
                "role": "antagonist",
                "personality": "secretive",
                "background": "Opposes the traveler.",
                "goal": "Keep the secret",
                "values": "ambition",
                "mood": "tense",
                "hp": 100,
                "power_level": 12,
            }
        )
        involves.append(sk["antagonist_name"])

    return _inject_skeleton_constraints(
        {
            "title": sk["title"],
            "premise": sk["premise"],
            "locations": locs,
            "adjacencies": adj,
            "persons": persons,
            "items": items,
            "quests": [
                {
                    "name": sk["quest"]["name"],
                    "description": sk["quest"]["objective"],
                    "stage": "intro",
                    "state": "active",
                    "involves": involves,
                    "objective": sk["quest"]["objective"],
                }
            ],
            "facts": [
                {
                    "name": "Hook",
                    "text": sk["premise"][:80],
                    "secret_level": 0,
                    "known_by": [sk["player_name"]],
                }
            ],
            "rumors": [],
            "social": [],
        },
        sk,
    )


def _llm_plan(
    prompt: str,
    *,
    draft: Optional[Dict[str, Any]] = None,
) -> Optional[EventBlueprint]:
    """四步规划：skeleton → world → cast → mechanics，再确定性修补。"""
    try:
        from llm_router import primary_available
    except Exception as e:
        logger.info("Director LLM imports unavailable: %s", e)
        return None
    if not primary_available():
        logger.info("Director primary LLM not configured")
        return None

    sk = _stage_skeleton(prompt, draft)
    if not sk:
        logger.warning("Director skeleton stage failed")
        return None

    world = _stage_world(prompt, sk, draft)
    if not world:
        logger.warning("Director world stage failed; using skeleton fallback world")
        world = _fallback_world_from_skeleton(sk)

    cast_patch = _stage_cast(prompt, world, sk)
    if cast_patch:
        world = _apply_cast_patch(world, cast_patch, sk)
    else:
        logger.warning("Director cast stage failed; relying on ensure_cast_quality")

    mech = _stage_mechanics(prompt, world, sk)
    if mech:
        world = _apply_mechanics_patch(world, mech)
    else:
        logger.warning("Director mechanics stage failed; relying on ensure_playable_mechanics")

    try:
        bp = EventBlueprint.from_dict(world, source="llm")
        bp = ensure_cast_quality(bp, puzzle_kind=str(sk.get("puzzle_kind") or ""))
        bp = ensure_playable_mechanics(bp)
        bp = EventBlueprint.from_dict(
            {k: v for k, v in bp.to_dict().items() if k != "counts"},
            source="llm",
        )
        bp = ensure_cast_quality(bp, puzzle_kind=str(sk.get("puzzle_kind") or ""))
        bp = ensure_playable_mechanics(bp)

        ok, issues = is_minimally_playable(bp)
        if issues:
            logger.info("Director playability issues: %s", issues)
        if not ok:
            logger.warning("Director blueprint failed hard playability: %s", issues)
            return None
        if not bp.locations or not bp.persons:
            return None
        if bp.quests and not bp.items:
            return None

        solv = check_solvable(bp, puzzle_kind=str(sk.get("puzzle_kind") or ""))
        if not solv.ok:
            logger.warning(
                "Director blueprint unsolvable (%s): %s goals=%s",
                solv.reason,
                solv.puzzle_kind,
                solv.win_goals,
            )
            return None
        logger.info(
            "Director solvability ok kind=%s steps=%s sketch=%s",
            solv.puzzle_kind,
            solv.steps,
            solv.plan_sketch[:12],
        )
        return bp
    except Exception as e:
        logger.warning("Director blueprint parse failed: %s", e)
        return None


def plan_event(
    prompt: str,
    *,
    force_mock: bool = False,
    template_id: Optional[str] = None,
    world_id: Optional[str] = None,
    draft: Optional[Dict[str, Any]] = None,
) -> EventBlueprint:
    """
    规划事件蓝图。

    优先：指定 world_id / template_id → 文本配置的模板世界
    其次：LLM 四步补全（除非 force_mock）
    最后：按关键词选模板世界
    """
    prompt = (prompt or "").strip() or (
        "A traveler must fetch a key and unlock a door."
    )
    wid = (world_id or template_id or "").strip() or None

    if wid:
        known = {w["id"] for w in list_worlds()}
        if wid not in known:
            raise ValueError(f"Unknown world_id: {wid}")
        out = load_world(wid)
        if prompt and len(prompt) > 12:
            out.premise = prompt
        return out

    if not force_mock:
        llm_bp = _llm_plan(prompt, draft=draft)
        if llm_bp is not None:
            return llm_bp

    return pick_world(prompt)
