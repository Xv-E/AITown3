"""
目标可行性门闩 — 校验实体类型、已满足、可规划性后再选目标。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from goal_planner import plan_for_goal

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

SOCIAL_GOALS = frozenset({"Gain information", "Defeat enemy"})
LOCATION_GOALS = frozenset({"Go to location", "Explore unknown location"})
ITEM_GOALS = frozenset({
    "Collect item",
    "Accumulate items",
    "Strengthen abilities",
    "Enhance capabilities",
})
USE_ON_GOALS = frozenset({"Use item on"})
USE_SELF_GOALS = frozenset({"Use item"})
INTERACT_GOALS = frozenset({"Interact with"})
# target_entity = 内容包里只需本人参与的动作名（如 sleep）
PERFORM_GOALS = frozenset({"Perform action"})


@dataclass
class FeasibilityResult:
    ok: bool
    goal_data: Dict[str, Any]
    reason: str = ""
    already_satisfied: bool = False
    plan: Optional[List[Tuple[str, List[str]]]] = None
    engine: str = ""


def _entity_exists(world: "Neo4jWorld", name: str, label: str) -> bool:
    names = world.list_entities(label) or []
    return name in names


def _is_dead(world: "Neo4jWorld", person: str) -> bool:
    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})
        RETURN coalesce(p.dead, false) AS dead, coalesce(p.hp, 0) AS hp
        LIMIT 1
        """,
        n=person,
    )
    if not rows:
        return True
    return bool(rows[0].get("dead")) or int(rows[0].get("hp") or 0) <= 0


def _person_location(world: "Neo4jWorld", person: str) -> Optional[str]:
    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1",
        n=person,
    )
    return rows[0]["loc"] if rows else None


def _holds_item(world: "Neo4jWorld", person: str, item: str) -> bool:
    rows = world.run_query(
        "MATCH (p:Person {name: $c})-[:HAS_ITEM]->(i:Item {name: $i}) RETURN i.name AS n LIMIT 1",
        c=person,
        i=item,
    )
    return bool(rows)


def get_accepts_from(world: "Neo4jWorld", name: str, label: str) -> List[str]:
    rows = world.run_query(
        f"MATCH (e:{label} {{name: $n}}) RETURN e.accepts_from AS a LIMIT 1",
        n=name,
    )
    raw = rows[0].get("a") if rows else None
    if isinstance(raw, str):
        try:
            import json

            raw = json.loads(raw)
        except Exception:
            raw = []
    return [str(x) for x in (raw or []) if x]


def _holds_kind(world: "Neo4jWorld", person: str, kind: str) -> bool:
    rows = world.run_query(
        """
        MATCH (p:Person {name: $c})-[:HAS_ITEM]->(i:Item)
        WHERE toLower(coalesce(i.item_kind, '')) = $k
        RETURN i.name AS n LIMIT 1
        """,
        c=person,
        k=kind.lower(),
    )
    return bool(rows)


def _visited(world: "Neo4jWorld", person: str, loc: str) -> bool:
    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:VISITED]->(l:Location {name: $loc}) RETURN l.name AS n LIMIT 1",
        n=person,
        loc=loc,
    )
    return bool(rows)


def sanitize_goal_target(
    world: "Neo4jWorld",
    character: str,
    goal: str,
    target_entity: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """
    校验并修正 target_entity。
    Returns: (sanitized_target_or_None, error_or_None)
    """
    if not target_entity:
        return None, None
    t = target_entity.strip()
    if not t:
        return None, None

    if goal in SOCIAL_GOALS:
        if t == character:
            return None, "Social/combat goals cannot target yourself"
        if not _entity_exists(world, t, "Person"):
            # 尝试地点（Gain information 允许探索地点）
            if goal == "Gain information" and _entity_exists(world, t, "Location"):
                return t, None
            return None, f"Unknown person: {t}"
        if _is_dead(world, t) and goal == "Defeat enemy":
            return None, f"{t} is dead and cannot be a defeat target"
        if _is_dead(world, t) and goal == "Gain information":
            return None, f"{t} is dead and cannot be talked to"
        return t, None

    if goal in LOCATION_GOALS:
        if not _entity_exists(world, t, "Location"):
            return None, f"Unknown location: {t}"
        return t, None

    if t.startswith("kind:") and goal in ITEM_GOALS | USE_SELF_GOALS:
        return t, None

    if goal in PERFORM_GOALS:
        from registry import get_registry

        action = get_registry().get(t)
        if action is None:
            return None, f"Unknown action: {t}"
        if len(action.parameters) != 1:
            return None, f"{t} needs more than the actor; it cannot be a self action"
        return t, None

    if goal in USE_SELF_GOALS:
        if not _entity_exists(world, t, "Item"):
            return None, f"Unknown item: {t}"
        return t, None

    if goal in ITEM_GOALS:
        if not _entity_exists(world, t, "Item"):
            return None, f"Unknown item: {t}"
        rows = world.run_query(
            """
            MATCH (i:Item {name: $n})
            RETURN coalesce(i.item_kind, '') AS kind, coalesce(i.portable, true) AS portable
            LIMIT 1
            """,
            n=t,
        )
        if rows:
            kind = str(rows[0].get("kind") or "").lower()
            if kind == "door" or rows[0].get("portable") is False:
                return None, f"{t} is not pickable; use Use item on instead"
        return t, None

    if goal in USE_ON_GOALS:
        # 世界交互表中的目标（门 / 人物 / 物品）优先
        try:
            from world_interactions import tools_for_target

            if tools_for_target(t):
                if _entity_exists(world, t, "Person") or _entity_exists(world, t, "Item"):
                    return t, None
        except Exception:
            pass
        for label in ("Item", "Person"):
            if _entity_exists(world, t, label) and get_accepts_from(world, t, label):
                return t, None
        if not _entity_exists(world, t, "Item"):
            return None, f"Unknown interaction target: {t}"
        rows = world.run_query(
            """
            MATCH (i:Item {name: $n})
            RETURN coalesce(i.item_kind, '') AS kind
            LIMIT 1
            """,
            n=t,
        )
        if not rows or str(rows[0].get("kind") or "").lower() != "door":
            return None, f"{t} is not a valid interaction target (no effects_on / accepts_from)"
        return t, None

    if goal in INTERACT_GOALS:
        if not _entity_exists(world, t, "Item"):
            return None, f"Unknown mechanism/item: {t}"
        try:
            from item_interact import is_interactive_item

            if not is_interactive_item(world, t):
                return None, f"{t} has no interactive script (behavior/interactive)"
        except Exception:
            return None, f"{t} interaction script unavailable"
        return t, None

    # 通用：任一类实体
    for label in ("Person", "Location", "Item"):
        if _entity_exists(world, t, label):
            if label == "Person" and t == character and goal in SOCIAL_GOALS:
                return None, "Cannot target yourself"
            return t, None
    return None, f"Unknown entity: {t}"


def is_goal_satisfied(
    world: "Neo4jWorld",
    character: str,
    goal: str,
    target_entity: Optional[str],
    *,
    interact_action: Optional[str] = None,
) -> bool:
    if goal in LOCATION_GOALS and target_entity:
        if goal == "Go to location":
            return _person_location(world, character) == target_entity
        if goal == "Explore unknown location":
            return _visited(world, character, target_entity)
    if goal in ITEM_GOALS and target_entity:
        if target_entity.startswith("kind:"):
            return _holds_kind(world, character, target_entity[len("kind:"):])
        return _holds_item(world, character, target_entity)
    if goal in USE_ON_GOALS and target_entity:
        # 人物目标：交互仍可用则未满足（例如尚未用药）
        try:
            from world_interactions import tools_for_target

            tools = tools_for_target(target_entity)
        except Exception:
            tools = []
        if tools and _entity_exists(world, target_entity, "Person"):
            return False
        rows = world.run_query(
            """
            MATCH (i:Item {name: $n})
            RETURN coalesce(i.locked, false) AS locked,
                   coalesce(i.item_kind, '') AS kind
            LIMIT 1
            """,
            n=target_entity,
        )
        if not rows:
            return False
        return str(rows[0].get("kind") or "").lower() == "door" and not bool(rows[0].get("locked"))
    if goal in INTERACT_GOALS and target_entity:
        # 具体临时 action（chop / turn_…）是一次性候选，不当作「谜题已解」
        opt = (interact_action or "").strip()
        if opt and opt not in ("auto", "_", "-"):
            return False
        try:
            from item_interact import interaction_success

            return bool(interaction_success(world, target_entity))
        except Exception:
            return False
    if goal == "Gain information" and target_entity:
        # 地点目标：已到达即满足
        if _entity_exists(world, target_entity, "Location"):
            return _person_location(world, character) == target_entity
        # 人物目标：不能偷看 NPC 私有知识图来判断「还有没有情报」。
        # 是否再问，交给情境（记忆 / 已知线索）由 LLM 决定；此处永不预判满足。
        if _entity_exists(world, target_entity, "Person"):
            return False
        return False
    if goal == "Defeat enemy" and target_entity:
        return _is_dead(world, target_entity)
    if goal in PERFORM_GOALS and target_entity:
        # 进入持续状态的动作：已在该状态即满足
        from action import get_property
        from registry import get_registry

        enter = getattr(get_registry().get(target_entity), "enter_state", None)
        return bool(enter) and get_property(world, character, "Person", "state") == enter
    return False


def assess_goal(
    world: "Neo4jWorld",
    character: str,
    goal_data: Dict[str, Any],
    *,
    engines: Optional[List[str]] = None,
    skip_plan: bool = False,
) -> FeasibilityResult:
    """校验目标并可选预规划。"""
    cleaned = dict(goal_data or {})
    if not cleaned.get("goal") and cleaned.get("final_goal"):
        cleaned["goal"] = cleaned.get("final_goal")
    # 规范化 interact_action
    ia = (
        cleaned.get("interact_action")
        or (cleaned.get("metadata") or {}).get("interact_action")
        or cleaned.get("option")
        or ""
    )
    ia = str(ia or "").strip()
    if ia:
        cleaned["interact_action"] = ia
        meta = dict(cleaned.get("metadata") or {})
        meta["interact_action"] = ia
        cleaned["metadata"] = meta

    goal = (cleaned.get("goal") or "").strip()
    if not goal:
        return FeasibilityResult(ok=False, goal_data=cleaned, reason="Missing goal")

    raw_target = cleaned.get("target_entity")
    target, err = sanitize_goal_target(world, character, goal, raw_target)
    cleaned["target_entity"] = target
    if err and raw_target:
        cleaned["confidence"] = float(cleaned.get("confidence") or 0.5) * 0.5
        if goal in (
            SOCIAL_GOALS | LOCATION_GOALS | ITEM_GOALS | USE_ON_GOALS | USE_SELF_GOALS
            | INTERACT_GOALS | PERFORM_GOALS
        ):
            return FeasibilityResult(ok=False, goal_data=cleaned, reason=err)

    if is_goal_satisfied(
        world, character, goal, target, interact_action=cleaned.get("interact_action")
    ):
        return FeasibilityResult(
            ok=True,
            goal_data=cleaned,
            reason="Goal already satisfied",
            already_satisfied=True,
            plan=[],
            engine="satisfied",
        )

    if skip_plan:
        return FeasibilityResult(ok=True, goal_data=cleaned, reason="ok")

    plan_result = plan_for_goal(
        world,
        character,
        cleaned,
        engines=engines or ["unified_planning", "bfs"],
    )
    if not plan_result.success:
        return FeasibilityResult(
            ok=False,
            goal_data=cleaned,
            reason=plan_result.error or "Planning failed",
            engine=plan_result.engine,
        )
    return FeasibilityResult(
        ok=True,
        goal_data=cleaned,
        reason="ok",
        already_satisfied=not plan_result.plan,
        plan=list(plan_result.plan),
        engine=plan_result.engine,
    )


def _candidate_score(cand: Dict[str, Any]) -> float:
    return (
        float(cand.get("priority") or 0.5) * 0.6
        + float(cand.get("confidence") or 0.5) * 0.4
    )


def filter_feasible_candidates(
    world: "Neo4jWorld",
    character: str,
    candidates: List[Dict[str, Any]],
    *,
    engines: Optional[List[str]] = None,
    exclude_goals: Optional[List[str]] = None,
    stop_after_first: bool = False,
) -> List[FeasibilityResult]:
    """按分数从高到低做可行性规划。

    stop_after_first=True 时找到第一个可执行计划即停（避免 3 个候选各跑一遍 PDDL）。
    """
    exclude = set(exclude_goals or [])
    normalized: List[Dict[str, Any]] = []
    for cand in candidates:
        c = dict(cand or {})
        if not c.get("goal") and c.get("final_goal"):
            c["goal"] = c.get("final_goal")
        ia = (
            c.get("interact_action")
            or (c.get("metadata") or {}).get("interact_action")
            or ""
        )
        key = f"{c.get('goal')}|{c.get('target_entity')}|{ia}"
        # 兼容旧 exclude 键（无 interact_action 后缀）
        legacy = f"{c.get('goal')}|{c.get('target_entity')}"
        if key in exclude or legacy in exclude:
            continue
        normalized.append(c)
    normalized.sort(key=_candidate_score, reverse=True)

    out: List[FeasibilityResult] = []
    for c in normalized:
        result = assess_goal(world, character, c, engines=engines)
        if result.ok and not result.already_satisfied:
            out.append(result)
            if stop_after_first:
                break
        elif result.ok and result.already_satisfied:
            continue
    return out


def pick_best_feasible(
    world: "Neo4jWorld",
    character: str,
    candidates: List[Dict[str, Any]],
    *,
    engines: Optional[List[str]] = None,
    exclude_goals: Optional[List[str]] = None,
) -> Optional[FeasibilityResult]:
    feasible = filter_feasible_candidates(
        world,
        character,
        candidates,
        engines=engines,
        exclude_goals=exclude_goals,
        stop_after_first=True,
    )
    return feasible[0] if feasible else None
