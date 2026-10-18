"""
目标规划器 — 将 LLM 目标转为 PDDL 计划或规则计划。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from goal_to_pddl import GoalToPDDLConverter
from registry import act
from trace_util import record_trace

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

PlanStep = Tuple[str, List[str]]


@dataclass
class PlanResult:
    success: bool
    plan: List[PlanStep] = field(default_factory=list)
    engine: str = ""
    error: Optional[str] = None
    goal_facts: List[str] = field(default_factory=list)
    used_rule_based: bool = False
    domain_str: str = ""
    problem_str: str = ""


def _get_person_location(world: "Neo4jWorld", person: str) -> Optional[str]:
    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1",
        n=person,
    )
    return rows[0]["loc"] if rows else None


def _get_entity_location(world: "Neo4jWorld", label: str, name: str) -> Optional[str]:
    if label == "Person":
        return _get_person_location(world, name)
    rows = world.run_query(
        f"MATCH (e:{label} {{name: $n}})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1",
        n=name,
    )
    return rows[0]["loc"] if rows else None


def _item_pickability(world: "Neo4jWorld", item: str) -> Tuple[bool, str, Optional[str]]:
    rows = world.run_query(
        "MATCH (p:Person)-[:HAS_ITEM]->(i:Item {name: $n}) RETURN p.name AS holder LIMIT 1",
        n=item,
    )
    if rows and rows[0].get("holder"):
        return False, f"{item} is held by {rows[0]['holder']}", None

    meta = world.run_query(
        """
        MATCH (i:Item {name: $n})
        RETURN coalesce(i.item_kind, '') AS kind, coalesce(i.portable, true) AS portable
        LIMIT 1
        """,
        n=item,
    )
    if meta:
        kind = str(meta[0].get("kind") or "").lower()
        portable = meta[0].get("portable")
        if kind == "door" or portable is False:
            return False, f"{item} is not pickable", None

    item_loc = _get_entity_location(world, "Item", item)
    if item_loc:
        return True, "", item_loc

    rows = world.run_query(
        "MATCH (p:Person)-[:OWNS]->(i:Item {name: $n}) RETURN p.name AS owner LIMIT 1",
        n=item,
    )
    if rows and rows[0].get("owner"):
        return False, f"{item} is not on the ground ({rows[0]['owner']} owns it); cannot pick up", None
    return False, f"{item} is not at any location; cannot pick up", None


def _plan_collect_item(
    world: "Neo4jWorld",
    character: str,
    item: str,
) -> PlanResult:
    from pddl import build_pathfinding_pddl
    from solver import solve_pddl

    held = world.run_query(
        "MATCH (p:Person {name: $c})-[:HAS_ITEM]->(i:Item {name: $i}) RETURN i.name AS n",
        c=character,
        i=item,
    )
    if held:
        return PlanResult(success=True, plan=[], engine="rule:collect", used_rule_based=True)

    ok, reason, item_loc = _item_pickability(world, item)
    if not ok or item_loc is None:
        return PlanResult(success=False, error=reason, used_rule_based=True)

    cur = _get_person_location(world, character)
    plan: List[PlanStep] = []

    domain_str = ""
    problem_str = ""
    if cur != item_loc:
        domain_str, problem_str = build_pathfinding_pddl(world, character, item_loc)
        # 纯寻路优先 BFS，避免 Fast Downward 挂起导致无 PDDL 日志、角色卡住
        result = solve_pddl(domain_str, problem_str, engines=["bfs"])
        if not result["success"]:
            return PlanResult(
                success=False,
                error=result.get("error", "Pathfinding to item location failed"),
                used_rule_based=True,
            )
        plan.extend(result["plan"])

    plan.append((act("take"), [character, item, item_loc]))
    return PlanResult(
        success=True,
        plan=plan,
        engine="rule:collect",
        used_rule_based=True,
        domain_str=domain_str,
        problem_str=problem_str,
    )


def _door_info(world: "Neo4jWorld", door: str) -> Optional[Dict[str, Any]]:
    rows = world.run_query(
        """
        MATCH (i:Item {name: $n})
        OPTIONAL MATCH (i)-[:LOCATED_AT]->(l:Location)
        RETURN coalesce(i.item_kind, '') AS kind,
               coalesce(i.locked, false) AS locked,
               coalesce(i.key_item, '') AS key_item,
               coalesce(i.connects_to, '') AS connects_to,
               l.name AS loc
        LIMIT 1
        """,
        n=door,
    )
    if not rows:
        return None
    r = rows[0]
    info = {
        "kind": r.get("kind") or "",
        "locked": bool(r.get("locked")),
        "key_item": r.get("key_item") or "",
        "connects_to": r.get("connects_to") or "",
        "loc": r.get("loc"),
    }
    # 世界临时交互优先
    try:
        from world_interactions import (
            destination_from_interaction,
            find_interaction,
            tools_for_target,
        )

        tools = tools_for_target(door)
        if tools:
            info["key_item"] = tools[0]
            inter = find_interaction(tools[0], door)
            if inter and info.get("loc"):
                dest = destination_from_interaction(inter, from_loc=info["loc"])
                if dest:
                    info["connects_to"] = dest
    except Exception:
        pass
    return info


def _plan_use_item_on(
    world: "Neo4jWorld",
    character: str,
    target: str,
) -> PlanResult:
    """规则回退：寻路到目标前 + use_on（工具/效果来自世界临时交互；目标可为门或人物）。"""
    from pddl import build_pathfinding_pddl
    from solver import solve_pddl

    try:
        from world_interactions import tools_for_target
    except Exception:
        tools_for_target = None  # type: ignore

    tools = list(tools_for_target(target) or []) if tools_for_target else []
    is_person = target in (world.list_entities("Person") or [])
    info = None if is_person else _door_info(world, target)

    if is_person:
        target_loc = _get_person_location(world, target)
        if not tools or not target_loc:
            return PlanResult(
                success=False,
                error=f"{target} has no usable tools or location (check accepts_from / effects_on)",
                used_rule_based=True,
            )
        key = next((t for t in tools if t in _held_items(world, character)), None)
        if not key:
            return PlanResult(
                success=False,
                error=f"Must hold {'/'.join(tools)} before using on {target}",
                used_rule_based=True,
            )
        to_loc = target_loc
    else:
        if not info:
            return PlanResult(success=False, error=f"Target not found: {target}", used_rule_based=True)
        can_interact = bool(tools) or str(info["kind"]).lower() == "door"
        if not can_interact:
            return PlanResult(success=False, error=f"{target} is not interactable", used_rule_based=True)
        if not info["locked"] and str(info["kind"]).lower() == "door":
            return PlanResult(success=True, plan=[], engine="rule:use-on", used_rule_based=True)
        key = info["key_item"] or (tools[0] if tools else "")
        target_loc = info["loc"]
        to_loc = info["connects_to"] or target_loc
        if not key or not target_loc:
            return PlanResult(
                success=False,
                error=f"{target} missing usable tool or location (check effects_on / accepts_from)",
                used_rule_based=True,
            )
        if key not in _held_items(world, character):
            return PlanResult(
                success=False,
                error=f"Must hold {key} before using on {target}",
                used_rule_based=True,
            )

    cur = _get_person_location(world, character)
    plan: List[PlanStep] = []
    domain_str = ""
    problem_str = ""
    if cur != target_loc:
        domain_str, problem_str = build_pathfinding_pddl(world, character, target_loc)
        result = solve_pddl(domain_str, problem_str, engines=["bfs"])
        if not result["success"]:
            return PlanResult(
                success=False,
                error=result.get("error", "Pathfinding to target failed"),
                used_rule_based=True,
            )
        plan.extend(result["plan"])

    plan.append(("use_on", [character, key, target, target_loc, to_loc]))
    return PlanResult(
        success=True,
        plan=plan,
        engine="rule:use-on",
        used_rule_based=True,
        domain_str=domain_str,
        problem_str=problem_str,
    )


def _plan_interact_with(
    world: "Neo4jWorld",
    character: str,
    target: str,
    *,
    interact_action: Optional[str] = None,
) -> PlanResult:
    """寻路到脚本机关 + interact(具体临时 action id，缺省才 auto)。"""
    try:
        from item_interact import (
            FORCE_MOCK,
            ensure_ephemeral_actions,
            interaction_success,
            is_interactive_item,
        )
    except Exception:
        return PlanResult(success=False, error="item_interact unavailable", used_rule_based=True)

    if not is_interactive_item(world, target):
        return PlanResult(
            success=False,
            error=f"{target} has no interactive script",
            used_rule_based=True,
        )

    option = (interact_action or "").strip()
    # 绑定了具体临时 action：视为一次性步骤，不因 puzzle solved 提前空计划
    if not option and interaction_success(world, target):
        return PlanResult(success=True, plan=[], engine="rule:interact-done", used_rule_based=True)

    # 若指定了 id，确保已合成；校验 id 存在
    if option and option not in ("auto", "_", "-"):
        try:
            actions = ensure_ephemeral_actions(
                world, character, target, force_mock=bool(FORCE_MOCK)
            )
            ids = {getattr(a, "id", "") for a in actions}
            if option not in ids:
                # 模糊匹配
                hit = next((i for i in ids if option in i or i in option), "")
                if hit:
                    option = hit
                else:
                    return PlanResult(
                        success=False,
                        error=f"{target} has no ephemeral action '{option}' (available: {sorted(ids)})",
                        used_rule_based=True,
                    )
            # 声明式动作自己的条件现在就不满足（走过去也没用）：别把做不了的目标交给执行
            from ephemeral_action_dsl import check_requires, find_action

            chosen = find_action(actions, option)
            if chosen is not None:
                ok, why = check_requires(world, chosen, actor=character, target=target)
                if not ok:
                    return PlanResult(
                        success=False,
                        error=f"Cannot {option} {target}: {why}",
                        used_rule_based=True,
                    )
        except Exception as e:
            return PlanResult(
                success=False,
                error=f"Failed to load ephemeral actions: {e}",
                used_rule_based=True,
            )
    else:
        option = "auto"

    target_loc = _get_entity_location(world, "Item", target)
    if not target_loc:
        return PlanResult(success=False, error=f"{target} is not at any location", used_rule_based=True)

    cur = _get_person_location(world, character)
    plan: List[PlanStep] = []
    domain_str = ""
    problem_str = ""
    if cur != target_loc:
        from pddl import build_pathfinding_pddl
        from solver import solve_pddl

        domain_str, problem_str = build_pathfinding_pddl(world, character, target_loc)
        result = solve_pddl(domain_str, problem_str, engines=["bfs"])
        if not result["success"]:
            return PlanResult(
                success=False,
                error=result.get("error", "Pathfinding to mechanism failed"),
                used_rule_based=True,
            )
        plan.extend(result["plan"])

    plan.append(("interact", [character, target, option]))
    return PlanResult(
        success=True,
        plan=plan,
        engine=f"rule:interact:{option}",
        used_rule_based=True,
        domain_str=domain_str,
        problem_str=problem_str,
    )


def _plan_defeat_enemy(
    world: "Neo4jWorld",
    character: str,
    target: str,
) -> PlanResult:
    """规则计划：寻路到目标 + attack。"""
    from pddl import build_pathfinding_pddl
    from solver import solve_pddl

    target_loc = _get_entity_location(world, "Person", target)
    if target_loc is None:
        return PlanResult(success=False, error=f"Cannot find location of {target}", used_rule_based=True)

    cur = _get_person_location(world, character)
    plan: List[PlanStep] = []

    if cur != target_loc:
        domain, problem = build_pathfinding_pddl(world, character, target_loc)
        result = solve_pddl(domain, problem, engines=["bfs"])
        if not result["success"]:
            return PlanResult(
                success=False,
                error=result.get("error", "Pathfinding failed"),
                used_rule_based=True,
            )
        plan.extend(result["plan"])

    plan.append((act("attack"), [character, target, target_loc]))
    return PlanResult(success=True, plan=plan, engine="rule:defeat", used_rule_based=True)


def _already_knows(world: "Neo4jWorld", a: str, b: str) -> bool:
    rows = world.run_query(
        "MATCH (x:Person {name: $a})-[:KNOWS]->(y:Person {name: $b}) RETURN y.name AS n LIMIT 1",
        a=a,
        b=b,
    )
    return bool(rows)


def _plan_gain_information(
    world: "Neo4jWorld",
    character: str,
    target: Optional[str],
) -> PlanResult:
    """规则计划：寻路到目标角色 + talk（避免 PDDL 将 talk 绑定为同一人）。"""
    from pddl import build_pathfinding_pddl
    from solver import solve_pddl

    persons = [p for p in world.list_entities("Person") if p != character]

    if not target:
        for p in persons:
            if not _already_knows(world, character, p):
                target = p
                break
        if not target and persons:
            target = persons[0]

    if not target:
        return PlanResult(success=False, error="Gain information requires a conversation target", used_rule_based=True)

    if target == character:
        return PlanResult(success=False, error="Cannot talk to yourself for information", used_rule_based=True)

    locations = world.list_entities("Location") or []
    if target in locations:
        cur = _get_person_location(world, character)
        if cur == target:
            return PlanResult(success=True, plan=[], engine="rule:gain-info", used_rule_based=True)
        domain, problem = build_pathfinding_pddl(world, character, target)
        result = solve_pddl(domain, problem, engines=["bfs"])
        if not result["success"]:
            return PlanResult(success=False, error=result.get("error", "Pathfinding failed"), used_rule_based=True)
        return PlanResult(success=True, plan=result["plan"], engine="rule:gain-info", used_rule_based=True)

    if target not in persons:
        return PlanResult(success=False, error=f"Unknown conversation target: {target}", used_rule_based=True)

    # 相识(KNOWS)≠已问到线索；仍可再次 talk
    target_loc = _get_entity_location(world, "Person", target)
    if target_loc is None:
        return PlanResult(success=False, error=f"Cannot find location of {target}", used_rule_based=True)

    cur = _get_person_location(world, character)
    plan: List[PlanStep] = []

    domain_str = ""
    problem_str = ""
    if cur != target_loc:
        domain_str, problem_str = build_pathfinding_pddl(world, character, target_loc)
        result = solve_pddl(domain_str, problem_str, engines=["bfs"])
        if not result["success"]:
            return PlanResult(
                success=False,
                error=result.get("error", "Pathfinding to conversation target failed"),
                used_rule_based=True,
            )
        plan.extend(result["plan"])

    plan.append((act("converse"), [character, target, target_loc]))
    return PlanResult(
        success=True,
        plan=plan,
        engine="rule:gain-info",
        used_rule_based=True,
        domain_str=domain_str,
        problem_str=problem_str,
    )


def _visited_locations(world: "Neo4jWorld", character: str) -> set:
    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:VISITED]->(l:Location) RETURN l.name AS loc",
        n=character,
    )
    return {r["loc"] for r in rows if r.get("loc")}


def _suggest_location(
    world: "Neo4jWorld",
    character: str,
    *,
    explore: bool = False,
) -> Optional[str]:
    """从图中选取默认地点（无硬编码别名）。"""
    locations = world.list_entities("Location") or []
    if not locations:
        return None
    cur = _get_person_location(world, character)
    visited = _visited_locations(world, character)

    if explore:
        unvisited = [loc for loc in locations if loc not in visited and loc != cur]
        if unvisited:
            return unvisited[0]
        rows = world.run_query(
            """
            MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location)-[:ADJACENT_TO]->(adj:Location)
            WHERE NOT (p)-[:VISITED]->(adj)
            RETURN adj.name AS loc LIMIT 1
            """,
            n=character,
        )
        if rows and rows[0].get("loc"):
            return rows[0]["loc"]
    elif cur:
        rows = world.run_query(
            """
            MATCH (l:Location {name: $loc})-[:ADJACENT_TO]->(adj:Location)
            RETURN adj.name AS loc ORDER BY loc LIMIT 1
            """,
            loc=cur,
        )
        if rows and rows[0].get("loc"):
            return rows[0]["loc"]

    return locations[0]


def _plan_go_to_location(
    world: "Neo4jWorld",
    character: str,
    target_loc: Optional[str],
    *,
    explore: bool = False,
) -> PlanResult:
    """规则寻路：Go to location / Explore unknown location。"""
    from pddl import build_pathfinding_pddl
    from solver import solve_pddl

    locations = world.list_entities("Location") or []
    if target_loc and target_loc not in locations:
        return PlanResult(
            success=False,
            error=f"Unknown location '{target_loc}'. Valid locations: {', '.join(locations)}",
            used_rule_based=True,
        )

    resolved = target_loc or _suggest_location(world, character, explore=explore)
    if not resolved:
        return PlanResult(success=False, error="A valid location is required", used_rule_based=True)

    if explore:
        rows = world.run_query(
            """
            MATCH (p:Person {name: $n})-[:VISITED]->(l:Location {name: $loc})
            RETURN l.name AS loc LIMIT 1
            """,
            n=character,
            loc=resolved,
        )
        if rows:
            return PlanResult(success=True, plan=[], engine="rule:explore", used_rule_based=True)

    cur = _get_person_location(world, character)
    if cur == resolved:
        return PlanResult(success=True, plan=[], engine="rule:goto", used_rule_based=True)

    domain, problem = build_pathfinding_pddl(world, character, resolved)
    result = solve_pddl(domain, problem, engines=["bfs"])
    if not result["success"]:
        return PlanResult(
            success=False,
            error=result.get("error", "Pathfinding failed"),
            used_rule_based=True,
        )
    engine = "rule:explore" if explore else "rule:goto"
    return PlanResult(
        success=True,
        plan=result["plan"],
        engine=engine,
        used_rule_based=True,
        domain_str=domain,
        problem_str=problem,
    )


def _held_items(world: "Neo4jWorld", character: str) -> List[str]:
    rows = world.run_query(
        "MATCH (p:Person {name: $c})-[:HAS_ITEM]->(i:Item) RETURN i.name AS n",
        c=character,
    )
    return [r["n"] for r in rows if r.get("n")]


def _person_hp(world: "Neo4jWorld", character: str) -> int:
    rows = world.run_query(
        "MATCH (p:Person {name: $n}) RETURN coalesce(p.hp, 0) AS hp LIMIT 1",
        n=character,
    )
    return int(rows[0]["hp"]) if rows else 0


def _find_potion(world: "Neo4jWorld", character: str) -> Optional[str]:
    for item in _held_items(world, character):
        if item.lower().startswith("potion") or item == "Potion":
            return item
    return None


def _maybe_prepend_heal(
    world: "Neo4jWorld",
    character: str,
    plan: List[PlanStep],
    *,
    hp_threshold: int = 40,
) -> List[PlanStep]:
    """低 HP 且持有药水时，在计划前插入 use_item。"""
    if _person_hp(world, character) >= hp_threshold:
        return plan
    potion = _find_potion(world, character)
    if not potion:
        return plan
    consume = act("consume")
    if any(step[0] == consume and potion in step[1] for step in plan):
        return plan
    return [(consume, [character, potion])] + list(plan)


def _maybe_append_give(
    world: "Neo4jWorld",
    character: str,
    target: Optional[str],
    plan: List[PlanStep],
) -> List[PlanStep]:
    """Gain information 后若背包有非药水小礼物，可附加 give。"""
    if not target or not plan:
        return plan
    if not any(step[0] == act("converse") for step in plan):
        return plan
    held = _held_items(world, character)
    try:
        from world_interactions import list_interactions

        working_tools = {it.tool for it in list_interactions()}
    except Exception:
        working_tools = set()
    # 有用途的物品（世界交互里的工具）不当随手礼物
    gifts = [
        i for i in held
        if not (i.lower().startswith("potion") or i == "Potion") and i not in working_tools
    ]
    if not gifts:
        return plan
    gift = gifts[0]
    loc = _get_entity_location(world, "Person", target) or _get_person_location(world, character)
    if not loc:
        return plan
    give = act("give")
    if any(step[0] == give for step in plan):
        return plan
    return list(plan) + [(give, [character, gift, target, loc])]


def _plan_perform_action(
    world: "Neo4jWorld",
    character: str,
    goal_data: Dict[str, Any],
) -> PlanResult:
    """先走到 at_location（可选），再执行一个只需本人参与的内容动作。"""
    from registry import get_registry

    name = str(goal_data.get("target_entity") or "").strip()
    action = get_registry().get(name)
    if action is None:
        return PlanResult(success=False, error=f"Unknown action: {name}")
    if len(action.parameters) != 1:
        return PlanResult(success=False, error=f"{name} is not a self action")
    plan: List[PlanStep] = []
    where = str(goal_data.get("at_location") or "").strip()
    if where and _get_person_location(world, character) != where:
        route = _plan_go_to_location(world, character, where, explore=False)
        if not route.success:
            return route
        plan.extend(route.plan)
    plan.append((name, [character]))
    return PlanResult(success=True, plan=plan, engine="rule:perform", used_rule_based=True)


def wait_plan(character: str) -> PlanResult:
    """无可行动作时的显式等待。"""
    return PlanResult(
        success=True,
        plan=[(act("idle"), [character])],
        engine="rule:wait",
        used_rule_based=True,
    )


def _format_plan_steps(plan: List[PlanStep]) -> str:
    if not plan:
        return "(empty plan)"
    lines = []
    for i, (action, args) in enumerate(plan, 1):
        lines.append(f"{i}. {action}({', '.join(args)})")
    return "\n".join(lines)


def _record_plan_trace(
    *,
    character: str,
    goal_data: Dict[str, Any],
    result: PlanResult,
    elapsed_ms: float,
    domain_str: str = "",
    problem_str: str = "",
) -> None:
    goal_term = goal_data.get("goal", "")
    target = goal_data.get("target_entity")
    plan_text = _format_plan_steps(result.plan) if result.success else ""
    err = result.error or ""
    domain = domain_str or result.domain_str or ""
    problem = problem_str or result.problem_str or ""
    mode = "rule" if result.used_rule_based else "pddl-solver"
    prompt_parts = [
        "# Action plan request (not an LLM prompt)",
        f"mode: {mode}",
        f"engine: {result.engine or '(none)'}",
        f"character: {character}",
        f"goal: {goal_term}",
        f"target: {target}",
        f"goal_facts: {result.goal_facts or []}",
        "",
        "# Planned steps",
        plan_text if result.success else f"(failed) {err or 'Planning failed'}",
    ]
    if domain:
        prompt_parts.append("")
        prompt_parts.append("--- domain.pddl ---")
        prompt_parts.append(domain[:6000])
    if problem:
        prompt_parts.append("")
        prompt_parts.append("--- problem.pddl ---")
        prompt_parts.append(problem[:8000])
    elif result.used_rule_based and result.success:
        prompt_parts.append("")
        prompt_parts.append(
            "(rule path: no full PDDL domain/problem; steps come from BFS pathfinding + action append)"
        )
    record_trace(
        kind="pddl_plan",
        prompt="\n".join(prompt_parts),
        raw=plan_text if result.success else (err or "Planning failed"),
        ok=bool(result.success),
        error="" if result.success else (err or "Planning failed"),
        meta={
            "character": character,
            "goal": goal_term,
            "target_entity": target,
            "engine": result.engine or "",
            "used_rule_based": bool(result.used_rule_based),
            "mode": mode,
            "provider": "pddl",
            "plan_len": len(result.plan or []),
        },
        parsed={
            "plan": [{"action": a, "args": args} for a, args in (result.plan or [])],
            "goal_facts": list(result.goal_facts or []),
            "engine": result.engine,
            "error": result.error,
        },
        elapsed_ms=elapsed_ms,
    )


def plan_for_goal(
    world: "Neo4jWorld",
    character: str,
    goal_data: Dict[str, Any],
    *,
    engines: Optional[List[str]] = None,
) -> PlanResult:
    """
    根据目标数据生成可执行计划。

    Returns:
        PlanResult(success, plan, engine, error, ...)
    """
    # LLM 原始字段 final_goal → 内部 goal
    data = dict(goal_data or {})
    if not data.get("goal") and data.get("final_goal"):
        data["goal"] = data.get("final_goal")

    t0 = time.perf_counter()
    pddl_buf: Dict[str, str] = {}
    result = PlanResult(success=False, error="Planning incomplete")
    try:
        result = _plan_for_goal_impl(
            world, character, data, engines=engines, pddl_buf=pddl_buf
        )
        if result.domain_str and not pddl_buf.get("domain"):
            pddl_buf["domain"] = result.domain_str
        if result.problem_str and not pddl_buf.get("problem"):
            pddl_buf["problem"] = result.problem_str
    except Exception as e:
        result = PlanResult(success=False, error=f"Planning error: {e}")
    finally:
        _record_plan_trace(
            character=character,
            goal_data=data,
            result=result,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            domain_str=pddl_buf.get("domain", ""),
            problem_str=pddl_buf.get("problem", ""),
        )
    return result


def _plan_compiled(
    world: "Neo4jWorld",
    character: str,
    conditions: List[Dict[str, Any]],
    buf: Dict[str, str],
) -> PlanResult:
    """从内容数据自动编译的 PDDL（planner/compiler.py）。"""
    from compiler import load_snapshot, plan_compiled
    from knowledge import filter_snapshot, load_knowledge

    snap = filter_snapshot(load_snapshot(world), character, load_knowledge(world, character))
    result = plan_compiled(world, character, conditions, snapshot=snap)
    buf["domain"] = result.get("domain") or ""
    buf["problem"] = result.get("problem") or ""
    if not result["success"]:
        return PlanResult(success=False, error=f"Compiled PDDL: {result.get('error')}")
    return PlanResult(
        success=True,
        plan=list(result["plan"]),
        engine=f"compiled:{result.get('engine', '')}",
        domain_str=result.get("domain") or "",
        problem_str=result.get("problem") or "",
    )


def _as_far_as_known(err: str) -> str:
    from knowledge import knowledge_mode

    if knowledge_mode() == "omniscient" or not err:
        return err
    return f"{err} (as far as you know; explore or ask others)"


def _compiled_fallback(
    world: "Neo4jWorld",
    character: str,
    goal_term: str,
    conditions: List[Dict[str, Any]],
    failed: PlanResult,
    buf: Dict[str, str],
) -> PlanResult:
    """planning.json 的 fallback 里列出的目标：规则规划失败后再用编译 PDDL 试一次。"""
    from compiler import load_config

    if goal_term not in load_config().fallback:
        return failed
    result = _plan_compiled(world, character, conditions, buf)
    if result.success:
        return result
    failed.error = f"{failed.error}; {result.error}"
    return failed


def _plan_for_goal_impl(
    world: "Neo4jWorld",
    character: str,
    goal_data: Dict[str, Any],
    *,
    engines: Optional[List[str]] = None,
    pddl_buf: Optional[Dict[str, str]] = None,
) -> PlanResult:
    from pddl import build_domain, build_problem
    from solver import solve_pddl

    goal_term = goal_data.get("goal", "")
    target = goal_data.get("target_entity")
    converter = GoalToPDDLConverter(world)
    buf = pddl_buf if pddl_buf is not None else {}

    def _finalize(result: PlanResult) -> PlanResult:
        if not result.success:
            return result
        plan = _maybe_prepend_heal(world, character, list(result.plan))
        if goal_term == "Gain information":
            plan = _maybe_append_give(world, character, target, plan)
        result.plan = plan
        return result

    if goal_term == "Defeat enemy":
        if not target:
            persons = world.list_entities("Person")
            others = [p for p in persons if p != character]
            target = others[0] if others else None
        if not target:
            return PlanResult(success=False, error="Defeat enemy requires target_entity")
        return _finalize(_plan_defeat_enemy(world, character, target))

    if goal_term == "Fulfil request":
        from job_board import open_requests

        reqs = [r for r in open_requests(world, exclude=character) if not target or r["poster"] == target]
        if not reqs:
            return PlanResult(success=False, error=f"{target or 'Nobody'} has no open request")
        req = reqs[0]
        # 弄一件他要的那类东西，交到他手上（采集 / 制造 / 购买都行，交给规划器）
        want = [{"op": "has_item", "person": req["poster"], "item": f"kind:{req['kind']}"}]
        return _finalize(_plan_compiled(world, character, want, buf))

    if goal_term == "Form alliance":
        if not target:
            return PlanResult(success=False, error="Form alliance requires a person as target_entity")
        # 走过去、提出结盟（propose_alliance）；对方答不答应要到当面才知道
        want = [{"op": "has_rel", "a": "@self", "b": str(target), "rel": "ALLY",
                 "a_label": "Person", "b_label": "Person", "directed": True}]
        return _finalize(_plan_compiled(world, character, want, buf))

    if goal_term == "Achieve":
        conditions = goal_data.get("conditions") or []
        if not conditions:
            return PlanResult(success=False, error="Achieve requires conditions")
        return _finalize(_plan_compiled(world, character, list(conditions), buf))

    if goal_term in ("Collect item", "Accumulate items") and target and str(target).startswith("kind:"):
        from item_chain import load_world_view, plan_obtain

        steps, err = plan_obtain(load_world_view(world, viewer=character), character, str(target))
        if steps is None:
            failed = PlanResult(success=False, error=_as_far_as_known(err), used_rule_based=True)
            want = [{"op": "has_item", "person": "@self", "item": str(target)}]
            return _finalize(_compiled_fallback(world, character, goal_term, want, failed, buf))
        return _finalize(PlanResult(
            success=True, plan=steps, engine="rule:chain", used_rule_based=True
        ))

    if goal_term == "Use item":
        from item_chain import kind_ref, load_world_view, plan_use_self

        ref = target or (kind_ref(goal_data["item_kind"]) if goal_data.get("item_kind") else "")
        if not ref:
            return PlanResult(success=False, error="Use item requires an item or item_kind")
        steps, err = plan_use_self(
            load_world_view(world, viewer=character), character, str(ref), craft=bool(goal_data.get("craft"))
        )
        if steps is None:
            return PlanResult(success=False, error=_as_far_as_known(err), used_rule_based=True)
        return _finalize(PlanResult(
            success=True, plan=steps, engine="rule:chain", used_rule_based=True
        ))

    if goal_term in ("Collect item", "Accumulate items", "Strengthen abilities", "Enhance capabilities"):
        if not target:
            items = world.list_entities("Item")
            target = items[0] if items else None
        if not target:
            return PlanResult(success=False, error=f"{goal_term} requires target_entity")
        return _finalize(_plan_collect_item(world, character, target))

    if goal_term == "Use item on":
        if not target:
            try:
                from world_interactions import list_interactions

                held = set(_held_items(world, character))
                for inter in list_interactions() or []:
                    if inter.tool in held:
                        target = inter.target
                        break
            except Exception:
                target = None
            if not target:
                rows = world.run_query(
                    """
                    MATCH (i:Item)
                    WHERE coalesce(i.item_kind, '') = 'door' AND coalesce(i.locked, false) = true
                    RETURN i.name AS n ORDER BY n LIMIT 1
                    """
                )
                target = rows[0]["n"] if rows else None
        if not target:
            return PlanResult(success=False, error="Use item on requires an interaction target")
        # 规则路径优先（BFS 寻路 + use_on）；人物目标只走规则（PDDL 无 person use_on）
        goal_facts = converter.convert_goal_to_pddl(
            {"goal": goal_term, "target_entity": target}, character
        )
        door = _door_info(world, target)
        if not (door and str(door.get("kind") or "").lower() == "door"):
            from item_chain import load_world_view, plan_use_on

            prefer_kind = str(
                goal_data.get("item_kind")
                or (goal_data.get("metadata") or {}).get("item_kind")
                or ""
            )
            tool_hint = goal_data.get("tool_hint")
            view = load_world_view(world, viewer=character)
            if tool_hint and not prefer_kind:
                prefer_kind = view.kind_of(str(tool_hint))
            steps, chain_err = plan_use_on(view, character, target, prefer_kind=prefer_kind)
            if steps is not None:
                return _finalize(PlanResult(
                    success=True,
                    plan=steps,
                    engine="rule:chain",
                    used_rule_based=True,
                    goal_facts=goal_facts,
                ))
            if target in (world.list_entities("Person") or []):
                return PlanResult(success=False, error=chain_err, used_rule_based=True)
        rule = _plan_use_item_on(world, character, target)
        rule.goal_facts = goal_facts
        if rule.domain_str:
            buf["domain"] = rule.domain_str
        if rule.problem_str:
            buf["problem"] = rule.problem_str
        if rule.success:
            return _finalize(rule)

        if target in (world.list_entities("Person") or []):
            return _finalize(rule)

        action_names, include_predicates = converter.get_actions_for_goal(goal_term)
        domain_name = "aitown3-use-item-on"
        problem_name = f"{character}-use-on".lower()
        domain_str = build_domain(action_names, domain_name=domain_name)
        problem_str = build_problem(
            world,
            problem_name=problem_name,
            domain_name=domain_name,
            goal_facts=goal_facts,
            include_predicates=include_predicates,
        )
        buf["domain"] = domain_str
        buf["problem"] = problem_str
        result = solve_pddl(domain_str, problem_str, engines=engines)
        if result["success"]:
            return _finalize(PlanResult(
                success=True,
                plan=result["plan"],
                engine=result.get("engine", ""),
                goal_facts=goal_facts,
                domain_str=domain_str,
                problem_str=problem_str,
            ))
        if result.get("error"):
            rule.error = f"{rule.error or 'Rule fallback failed'}; PDDL: {result.get('error')}"
        return _finalize(rule)

    if goal_term == "Interact with":
        if not target:
            try:
                from item_interact import is_interactive_item

                for name in world.list_entities("Item") or []:
                    if is_interactive_item(world, name):
                        target = name
                        break
            except Exception:
                target = None
        if not target:
            return PlanResult(success=False, error="Interact with requires a scripted item/mechanism")
        option = (
            goal_data.get("interact_action")
            or (goal_data.get("metadata") or {}).get("interact_action")
            or goal_data.get("option")
            or ""
        )
        return _finalize(
            _plan_interact_with(
                world, character, target, interact_action=str(option or "")
            )
        )

    if goal_term == "Gain information":
        return _finalize(_plan_gain_information(world, character, target))

    if goal_term == "Perform action":
        return _finalize(_plan_perform_action(world, character, goal_data))

    if goal_term == "Go to location":
        return _finalize(_plan_go_to_location(world, character, target, explore=False))

    if goal_term == "Explore unknown location":
        return _finalize(_plan_go_to_location(world, character, target, explore=True))

    goal_facts = converter.convert_goal_to_pddl(goal_data, character)
    action_names, include_predicates = converter.get_actions_for_goal(goal_term)

    # Explore: 目标为 visited，domain 仍用 move
    domain_name = f"aitown3-{goal_term.replace(' ', '-').lower()}"
    problem_name = f"{character}-goal".lower()

    domain_str = build_domain(action_names, domain_name=domain_name)
    problem_str = build_problem(
        world,
        problem_name=problem_name,
        domain_name=domain_name,
        goal_facts=goal_facts,
        include_predicates=include_predicates,
    )
    buf["domain"] = domain_str
    buf["problem"] = problem_str

    result = solve_pddl(domain_str, problem_str, engines=engines)
    if result["success"]:
        return _finalize(PlanResult(
            success=True,
            plan=result["plan"],
            engine=result.get("engine", ""),
            goal_facts=goal_facts,
        ))

    # 回退：Go to location / Explore 用纯寻路
    if goal_term in ("Go to location", "Explore unknown location") and target:
        from pddl import build_pathfinding_pddl

        domain, problem = build_pathfinding_pddl(world, character, target)
        buf["domain"] = domain
        buf["problem"] = problem
        pf = solve_pddl(domain, problem, engines=engines)
        if pf["success"]:
            return _finalize(PlanResult(
                success=True,
                plan=pf["plan"],
                engine=pf.get("engine", ""),
                goal_facts=goal_facts,
            ))
        return PlanResult(
            success=False,
            error=pf.get("error"),
            goal_facts=goal_facts,
        )

    return PlanResult(
        success=False,
        error=result.get("error", "Planning failed"),
        goal_facts=goal_facts,
    )
