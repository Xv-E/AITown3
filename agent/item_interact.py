"""
物品交互 — LLM 用 Cypher 定义临时 Action，运行时挂到物品上再执行。

物品只需 behavior/lore 描述「这是什么」；
「能做什么」由 LLM 写成 ephemeral Cypher action。

PDDL：规划已有动作序列；交互语义在 Cypher 临时 Action 里发明。
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

logger = logging.getLogger(__name__)

_LAST_RESULT: Optional[Dict[str, Any]] = None
FORCE_MOCK: bool = False


def take_last_interact_result() -> Optional[Dict[str, Any]]:
    global _LAST_RESULT
    r = _LAST_RESULT
    _LAST_RESULT = None
    return r


def peek_last_interact_result() -> Optional[Dict[str, Any]]:
    return _LAST_RESULT


def is_interactive_item(world: "Neo4jWorld", name: str) -> bool:
    from item_scripts import load_item_script

    script = load_item_script(world, name)
    if not script:
        return False
    if script.get("interactive") is True:
        return True
    if (script.get("behavior") or "").strip():
        return True
    kind = str(script.get("kind") or "").lower()
    return kind in ("interactive", "puzzle", "mechanism", "device", "tree")


def item_state_snapshot(world: "Neo4jWorld", name: str) -> Dict[str, Any]:
    from action import get_property
    from item_scripts import load_item_script

    script = load_item_script(world, name)
    props = list(script.get("state_props") or [])
    for p in ("fuel", "lit", "locked", "solved", "facing", "steps", "phase", "unlocked"):
        if p not in props:
            props.append(p)
    snap: Dict[str, Any] = {"name": name}
    rows = world.run_query(
        """
        MATCH (i:Item {name: $n})
        RETURN coalesce(i.item_kind, '') AS item_kind,
               coalesce(i.description, '') AS description
        LIMIT 1
        """,
        n=name,
    )
    if rows:
        snap["item_kind"] = rows[0].get("item_kind")
        snap["description"] = rows[0].get("description")
    for p in props:
        snap[p] = get_property(world, name, "Item", p)
    snap["lore"] = (script.get("lore") or "").strip()
    snap["behavior"] = (script.get("behavior") or script.get("lore") or "").strip()
    return snap


def generate_interaction_options(
    world: "Neo4jWorld",
    actor: str,
    item: str,
    *,
    force_mock: bool = False,
    memory=None,
    refresh: bool = False,
) -> Dict[str, Any]:
    """
    发现/刷新该物品上的临时 actions（类代码），返回选项列表。
    兼容旧 API：options[].id / label；并附带 actions[].source_code。
    """
    from ephemeral_action_dsl import load_actions_from_item
    from trace_util import record_trace

    if not is_interactive_item(world, item):
        return {"ok": False, "error": f"{item} 不是可交互物品", "options": [], "actions": []}

    existing = load_actions_from_item(world, item)
    if existing and not refresh:
        options = [{"id": a.id, "label": a.label, "hint": "", "source_code": a.source_code} for a in existing]
        return {
            "ok": True,
            "options": options,
            "actions": [a.to_dict() for a in existing],
            "source": "cached",
            "state": item_state_snapshot(world, item),
        }

    bundled = synthesize_ephemeral_actions(
        world, actor, item, force_mock=force_mock or FORCE_MOCK, memory=memory
    )
    actions = bundled.get("actions") or []
    options = [
        {"id": a["id"], "label": a.get("label") or a["id"], "hint": "", "source_code": a.get("source_code")}
        for a in actions
        if a.get("id")
    ]
    record_trace(
        kind="item_ephemeral_actions",
        prompt=f"synthesize actions for {item}",
        response=(bundled.get("raw_code") or "")[:4000],
        meta={"actor": actor, "item": item, "source": bundled.get("source"), "n": len(actions)},
    )
    return {
        "ok": True,
        "options": options,
        "actions": actions,
        "source": bundled.get("source"),
        "raw_code": bundled.get("raw_code"),
        "state": item_state_snapshot(world, item),
        "error": bundled.get("error"),
    }


def ensure_ephemeral_actions(
    world: "Neo4jWorld",
    actor: str,
    item: str,
    *,
    force_mock: bool = False,
    memory=None,
    refresh: bool = False,
) -> List[Any]:
    """确保物品上已有临时 Cypher actions（有缓存则复用）。"""
    from ephemeral_action_dsl import EphemeralAction, load_actions_from_item

    if not refresh:
        existing = load_actions_from_item(world, item)
        if existing:
            return existing
    synth = synthesize_ephemeral_actions(
        world, actor, item, force_mock=force_mock or FORCE_MOCK, memory=memory
    )
    return [
        EphemeralAction.from_dict(a)
        for a in (synth.get("actions") or [])
        if a.get("id")
    ]


def ephemeral_action_goal_candidates(
    world: "Neo4jWorld",
    actor: str,
    *,
    force_mock: bool = False,
    memory=None,
    same_location_only: bool = True,
    prune: bool = True,
    max_per_item: int = 2,
    max_total: int = 4,
    context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    每个临时 action 变成一条规划候选；默认剪枝，避免同地动作爆炸。
    """
    from action import get_person_location

    actor_loc = get_person_location(world, actor)
    items: List[str] = []
    for name in world.list_entities("Item") or []:
        if not is_interactive_item(world, name):
            continue
        if same_location_only:
            loc = _item_loc(world, name)
            if not loc or loc != actor_loc:
                continue
        items.append(name)

    ctx = dict(context or {})
    ctx.setdefault("actor", actor)
    ctx.setdefault("location", actor_loc)
    need_hints = _actor_need_hints(world, actor, ctx)

    out: List[Dict[str, Any]] = []
    for item in items:
        try:
            actions = ensure_ephemeral_actions(
                world,
                actor,
                item,
                force_mock=force_mock or FORCE_MOCK,
                memory=memory,
            )
        except Exception as e:
            logger.warning("ensure ephemeral for %s failed: %s", item, e)
            continue
        for a in actions:
            aid = getattr(a, "id", None) or (a.get("id") if isinstance(a, dict) else "")
            label = getattr(a, "label", None) or (a.get("label") if isinstance(a, dict) else "") or aid
            if not aid:
                continue
            pri, tags = _score_ephemeral_action(
                aid=str(aid),
                label=str(label),
                item=item,
                need_hints=need_hints,
            )
            out.append(
                {
                    "goal": "Interact with",
                    "target_entity": item,
                    "interact_action": aid,
                    "priority": pri,
                    "confidence": 0.75,
                    "source": "ephemeral_action",
                    "reason": f"{label} — temporary action on {item}",
                    "reasoning": f"{label} — temporary action on {item}",
                    "metadata": {
                        "interact_action": aid,
                        "action_label": label,
                        "host_item": item,
                        "prune_tags": tags,
                    },
                }
            )

    if prune:
        out = prune_ephemeral_candidates(
            out,
            max_per_item=max_per_item,
            max_total=max_total,
            need_hints=need_hints,
        )
    return out


# 实用 / 观赏 动词
_UTIL_KEYS = (
    "chop", "harvest", "gather", "cut", "fuel", "solve", "unlock", "open",
    "cook", "roast", "light", "feed", "fill", "craft", "repair", "turn",
    "rotate", "press", "pull", "push", "activate", "use",
)
_FLAVOR_KEYS = ("examine", "look", "inspect", "study", "observe", "read", "admire")


def _actor_need_hints(
    world: "Neo4jWorld",
    actor: str,
    ctx: Dict[str, Any],
) -> Dict[str, Any]:
    """从目标 / 篝火 / 背包抽一点剪枝信号（不跑 LLM）。"""
    from action import get_property

    hints: Dict[str, Any] = {
        "want_wood": False,
        "want_puzzle": False,
        "want_food": False,
        "text": "",
    }
    texts: List[str] = []
    for key in ("goal", "current_goal", "reason", "routine_activity"):
        v = ctx.get(key)
        if v:
            texts.append(str(v))
    try:
        g = get_property(world, actor, "Person", "goal")
        if g:
            texts.append(str(g))
    except Exception:
        pass

    blob = " ".join(texts).lower()
    hints["text"] = blob
    if any(k in blob for k in ("wood", "柴", "砍", "伐", "fuel", "fire", "篝火", "campfire", "chop")):
        hints["want_wood"] = True
    if any(k in blob for k in ("dial", "puzzle", "solve", "unlock", "机关", "谜")):
        hints["want_puzzle"] = True
    if any(k in blob for k in ("food", "meal", "berry", "吃", "餐", "hunger")):
        hints["want_food"] = True

    # 火势紧急 → 偏向取木 / 有用交互
    try:
        from world_systems import get_campfire_status

        st = get_campfire_status(world)
        if st and (st.get("low") or st.get("out")):
            hints["want_wood"] = True
            hints["fire_urgent"] = True
    except Exception:
        pass

    # 已有木头则略降 chop 优先级（仍可保留）
    try:
        rows = world.run_query(
            """
            MATCH (p:Person {name: $n})-[:HAS_ITEM]->(i:Item)
            WHERE toLower(coalesce(i.item_kind, '')) = 'wood'
               OR toLower(i.name) CONTAINS 'wood'
            RETURN count(i) AS n
            """,
            n=actor,
        )
        if rows and int(rows[0].get("n") or 0) > 0:
            hints["has_wood"] = True
    except Exception:
        pass
    return hints


def _score_ephemeral_action(
    *,
    aid: str,
    label: str,
    item: str,
    need_hints: Dict[str, Any],
) -> tuple:
    low = f"{aid} {label} {item}".lower()
    tags: List[str] = []
    pri = 0.55

    is_util = any(k in low for k in _UTIL_KEYS)
    is_flavor = any(k in low for k in _FLAVOR_KEYS) and not is_util
    if is_util:
        pri = 0.68
        tags.append("util")
    if is_flavor:
        pri = 0.40
        tags.append("flavor")

    if any(k in low for k in ("chop", "harvest", "gather", "cut", "wood")):
        tags.append("wood")
        if need_hints.get("want_wood"):
            pri += 0.18
        if need_hints.get("has_wood") and not need_hints.get("fire_urgent"):
            pri -= 0.08
    if any(k in low for k in ("turn", "rotate", "solve", "unlock", "dial")):
        tags.append("puzzle")
        if need_hints.get("want_puzzle"):
            pri += 0.14
    if need_hints.get("fire_urgent") and "wood" in tags:
        pri += 0.08

    # 文本里直接出现 action id
    blob = need_hints.get("text") or ""
    if aid and aid.lower() in blob:
        pri += 0.12
        tags.append("goal_match")

    pri = max(0.15, min(0.95, pri))
    return pri, tags


def prune_ephemeral_candidates(
    candidates: List[Dict[str, Any]],
    *,
    max_per_item: int = 2,
    max_total: int = 4,
    need_hints: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    剪枝规则：
    1) 同物品有实用动作时，丢掉 examine/look 等观赏类
    2) 每物品最多 max_per_item（按 priority）
    3) 全局最多 max_total
    """
    if not candidates:
        return []

    by_item: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        item = str(c.get("target_entity") or "")
        by_item.setdefault(item, []).append(c)

    kept: List[Dict[str, Any]] = []
    for item, rows in by_item.items():
        has_util = any(
            "util" in (r.get("metadata") or {}).get("prune_tags") or []
            or _is_util_id(str(r.get("interact_action") or ""))
            for r in rows
        )
        filtered = []
        for r in rows:
            aid = str(r.get("interact_action") or "").lower()
            tags = (r.get("metadata") or {}).get("prune_tags") or []
            flavor = "flavor" in tags or _is_flavor_id(aid)
            if has_util and flavor:
                continue
            filtered.append(r)
        if not filtered:
            filtered = list(rows)  # 全是 flavor 时至少留
        filtered.sort(
            key=lambda x: float(x.get("priority") or 0),
            reverse=True,
        )
        kept.extend(filtered[: max(1, max_per_item)])

    kept.sort(key=lambda x: float(x.get("priority") or 0), reverse=True)
    return kept[: max(1, max_total)]


def _is_util_id(aid: str) -> bool:
    low = aid.lower()
    return any(k in low for k in _UTIL_KEYS)


def _is_flavor_id(aid: str) -> bool:
    low = aid.lower()
    return any(k in low for k in _FLAVOR_KEYS) and not _is_util_id(low)


def synthesize_ephemeral_actions(
    world: "Neo4jWorld",
    actor: str,
    item: str,
    *,
    force_mock: bool = False,
    memory=None,
) -> Dict[str, Any]:
    """让 LLM（或 mock）写出临时 Cypher action 并挂到物品上。"""
    from ephemeral_action_dsl import (
        parse_action_script,
        store_actions_on_item,
    )
    from item_scripts import load_item_script

    script = load_item_script(world, item)
    state = item_state_snapshot(world, item)
    actor_ctx = _actor_context(world, actor, memory=memory)

    if force_mock:
        code = _mock_action_code(item, script, state)
        source = "mock"
    else:
        code, source, err = _llm_action_code(
            actor=actor,
            item=item,
            script=script,
            state=state,
            actor_ctx=actor_ctx,
        )
        if err and not code.strip():
            code = _mock_action_code(item, script, state)
            source = "error_fallback"

    actions = parse_action_script(code, host_item=item)
    if not actions:
        code = _mock_action_code(item, script, state)
        actions = parse_action_script(code, host_item=item)
        source = "parse_fallback"

    store_actions_on_item(world, item, actions)
    return {
        "ok": True,
        "actions": [a.to_dict() for a in actions],
        "raw_code": code,
        "source": source,
    }


def resolve_interaction(
    world: "Neo4jWorld",
    actor: str,
    item: str,
    option_id: str,
    *,
    option_label: str = "",
    force_mock: bool = False,
    memory=None,
    pick_if_auto: bool = True,
) -> Dict[str, Any]:
    """执行临时 action：必要时先合成脚本，再按 id 跑 DSL。"""
    global _LAST_RESULT
    from ephemeral_action_dsl import (
        EphemeralAction,
        execute_ephemeral,
        find_action,
        load_actions_from_item,
    )
    from trace_util import record_trace

    if not is_interactive_item(world, item):
        out = {"ok": False, "error": f"{item} 不是可交互物品"}
        _LAST_RESULT = out
        return out

    force = force_mock or FORCE_MOCK
    actions = load_actions_from_item(world, item)
    if not actions:
        synth = synthesize_ephemeral_actions(
            world, actor, item, force_mock=force, memory=memory
        )
        actions = [
            EphemeralAction.from_dict(a)
            for a in (synth.get("actions") or [])
            if a.get("id")
        ]
        for a in actions:
            a.host_item = item

    opt_id = (option_id or "").strip()
    if opt_id in ("", "auto", "_", "-"):
        if not pick_if_auto:
            out = {
                "ok": False,
                "error": "需要指定临时 action id",
                "options": [{"id": a.id, "label": a.label} for a in actions],
            }
            _LAST_RESULT = out
            return out
        chosen = _pick_action_id(world, actor, item, actions, force_mock=force, memory=memory)
        opt_id = chosen or (actions[0].id if actions else "")
    action = find_action(actions, opt_id)
    if action is None:
        out = {
            "ok": False,
            "error": f"未知临时 action: {opt_id}",
            "options": [{"id": a.id, "label": a.label} for a in actions],
        }
        _LAST_RESULT = out
        return out

    result = execute_ephemeral(world, action, actor=actor, target=item)
    out = {
        "ok": result.ok,
        "error": result.error,
        "narrative": result.narrative,
        "memory": result.memory,
        "option_id": action.id,
        "option_label": action.label or option_label or action.id,
        "ops": result.ops,
        "source_code": action.source_code,
        "source": "ephemeral_dsl",
        "state_after": item_state_snapshot(world, item),
    }
    _LAST_RESULT = out

    if memory is not None and out.get("memory") and result.ok:
        try:
            memory.add(
                actor,
                out["memory"],
                type="interaction",
                location=_item_loc(world, item) or "",
                success=True,
                result=out["narrative"],
            )
        except Exception:
            pass

    record_trace(
        kind="item_ephemeral_exec",
        prompt=action.source_code,
        response=json.dumps(
            {"ok": result.ok, "narrative": result.narrative, "ops": result.ops},
            ensure_ascii=False,
        )[:3000],
        meta={"actor": actor, "item": item, "action_id": action.id},
    )
    return out


def interaction_success(world: "Neo4jWorld", item: str) -> bool:
    from action import get_property
    from item_scripts import load_item_script

    script = load_item_script(world, item)
    when = script.get("success_when")
    if isinstance(when, dict) and when:
        try:
            from item_scripts import _when_true

            return bool(_when_true(world, item, when))
        except Exception:
            pass
    if get_property(world, item, "Item", "solved") is True:
        return True
    if get_property(world, item, "Item", "unlocked") is True:
        return True
    return False


# ----- LLM / mock code gen -----

_ACTION_SYSTEM = """You invent temporary interaction ACTIONS as Neo4j Cypher.
Output ONLY action blocks (no markdown fences unless needed).
Each action is registered at runtime on the item (not hardcoded).

Format:
action <id>(actor, target):
    \"\"\"short human label\"\"\"
    <Cypher here>
    RETURN <text> AS narrative, <text> AS memory

Parameters (always available): $actor, $target, $target_loc
Graph conventions in this world:
- (:Person {name})-[:LOCATED_AT]->(:Location {name})
- (:Item {name})-[:LOCATED_AT]->(:Location) when on ground
- (:Person)-[:HAS_ITEM]->(:Item) when held
- Item props: item_kind, value, portable, description, fuel, lit, facing, steps, solved, ...

Rules:
- 2-4 actions that fit the object's behavior.
- Prefer concrete verbs (chop, turn_clockwise, examine).
- Use MATCH ... WHERE with $actor/$target; do not hardcode actor/item names if params exist.
- For trees: chop CREATEs a wood Item and HAS_ITEM to the person.
- For dials: SET t.facing / t.steps / t.solved as needed.
- Always RETURN narrative (and optionally memory) as strings.
- Forbidden: DROP DATABASE, LOAD CSV, CALL dbms.*, whole-graph DETACH DELETE.
"""


def _llm_action_code(
    *,
    actor: str,
    item: str,
    script: Dict[str, Any],
    state: Dict[str, Any],
    actor_ctx: str,
) -> tuple:
    try:
        from llm_router import invoke_primary

        prompt = f"""Object: {item}
Behavior: {state.get('behavior') or script.get('behavior') or ''}
Lore: {state.get('lore') or ''}
State JSON: {json.dumps(state, ensure_ascii=False)}

Actor:
{actor_ctx}

Write temporary actions for THIS object now.
"""
        raw = invoke_primary(prompt, system=_ACTION_SYSTEM, max_tokens=900)
        return str(raw or ""), "llm", ""
    except Exception as e:
        logger.warning("synthesize ephemeral actions failed: %s", e)
        return "", "error", str(e)


def _mock_action_code(item: str, script: Dict[str, Any], state: Dict[str, Any]) -> str:
    behavior = (script.get("behavior") or state.get("behavior") or "").lower()
    kind = str(state.get("item_kind") or "").lower()
    chunks: List[str] = []
    if "tree" in kind or any(k in behavior for k in ("tree", "wood", "chop", "伐", "树")):
        chunks.append(
            '''action chop(actor, target):
    """Chop for wood"""
    MATCH (p:Person {name: $actor})-[:LOCATED_AT]->(loc:Location)<-[:LOCATED_AT]-(t:Item {name: $target})
    CREATE (w:Item {
      name: 'Wood',
      item_kind: 'wood',
      value: 8,
      portable: true,
      description: 'A freshly chopped log'
    })
    CREATE (p)-[:HAS_ITEM]->(w)
    RETURN $actor + ' chops a dry log from ' + $target AS narrative,
           $actor + ' got wood from ' + $target AS memory
'''
        )
    if any(k in behavior for k in ("dial", "turn", "rotate", "旋", "转")):
        chunks.append(
            '''action turn_clockwise(actor, target):
    """Turn clockwise"""
    MATCH (p:Person {name: $actor})-[:LOCATED_AT]->(:Location)<-[:LOCATED_AT]-(t:Item {name: $target})
    SET t.facing = 'east', t.steps = coalesce(t.steps, 0) + 1
    RETURN $actor + ' turns ' + $target + ' clockwise.' AS narrative
'''
        )
        chunks.append(
            '''action turn_counterclockwise(actor, target):
    """Turn counterclockwise"""
    MATCH (p:Person {name: $actor})-[:LOCATED_AT]->(:Location)<-[:LOCATED_AT]-(t:Item {name: $target})
    SET t.facing = 'west', t.steps = coalesce(t.steps, 0) + 1
    RETURN $actor + ' turns ' + $target + ' counterclockwise.' AS narrative
'''
        )
    chunks.append(
        '''action examine(actor, target):
    """Examine closely"""
    MATCH (p:Person {name: $actor}), (t:Item {name: $target})
    RETURN $actor + ' examines ' + $target + ' carefully.' AS narrative
'''
    )
    return "\n".join(chunks)


def _pick_action_id(
    world: "Neo4jWorld",
    actor: str,
    item: str,
    actions: List[Any],
    *,
    force_mock: bool,
    memory=None,
) -> str:
    if not actions:
        return ""
    if force_mock:
        # 优先 chop
        for a in actions:
            if "chop" in a.id:
                return a.id
        return actions[0].id
    ids = [a.id for a in actions]
    labels = [{"id": a.id, "label": a.label} for a in actions]
    prompt = f"""Pick ONE temporary action for {actor} on {item}.
Actions: {json.dumps(labels, ensure_ascii=False)}
Respond JSON only: {{"id": "one_id"}}
Allowed: {ids}
"""
    try:
        from llm_router import invoke_primary

        raw = invoke_primary(prompt, system="Choose one action id. JSON only.", max_tokens=60)
        data = _parse_json_obj(raw)
        oid = str((data or {}).get("id") or "").strip()
        if oid in ids:
            return oid
    except Exception:
        pass
    for a in actions:
        if "chop" in a.id:
            return a.id
    return actions[0].id


def _actor_context(world: "Neo4jWorld", actor: str, *, memory=None) -> str:
    from action import get_person_location, get_property

    loc = get_person_location(world, actor) or "?"
    lines = [
        f"Name: {actor}",
        f"Location: {loc}",
        f"Personality: {get_property(world, actor, 'Person', 'personality') or ''}",
        f"Mood: {get_property(world, actor, 'Person', 'mood') or ''}",
        f"Goal: {get_property(world, actor, 'Person', 'goal') or ''}",
    ]
    return "\n".join(lines)


def _item_loc(world: "Neo4jWorld", item: str) -> Optional[str]:
    rows = world.run_query(
        "MATCH (i:Item {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1",
        n=item,
    )
    return rows[0]["loc"] if rows else None


def _parse_json_obj(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
