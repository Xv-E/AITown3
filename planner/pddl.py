"""
PDDL domain / problem 字符串生成

设计：
  - 谓词集中在 KNOWN_PREDICATES 里，便于扩展
  - ACTION_PREDICATES 声明每个动作要用到哪些谓词，从而按需导出最小 domain
  - 每个动作的 PDDL 块来自 ActionRegistry（即 action.to_pddl()）
  - Problem 从 Neo4j 现状读取（at / adjacent / visited / has-item / item-at / alive / knows）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld


# ---------- 已知类型与谓词 ----------

ALL_TYPES: List[str] = ["person", "location", "item"]

# 谓词名 -> [(变量名, 类型)]
KNOWN_PREDICATES: Dict[str, List[Tuple[str, str]]] = {
    "at":        [("?c", "person"),   ("?l", "location")],
    "adjacent":  [("?l1", "location"), ("?l2", "location")],
    "visited":   [("?c", "person"),   ("?l", "location")],
    "has-item":  [("?c", "person"),   ("?i", "item")],
    "item-at":   [("?i", "item"),     ("?l", "location")],
    "alive":     [("?c", "person")],
    "knows":     [("?a", "person"),   ("?b", "person")],
    "locked":    [("?i", "item")],
    "unlocked":  [("?i", "item")],
    "key-for":   [("?k", "item"), ("?d", "item")],
    "connects":  [("?d", "item"), ("?from", "location"), ("?to", "location")],
}

# 每个动作引用的谓词（用于精简 domain，只导出必要的谓词）
ACTION_PREDICATES: Dict[str, List[str]] = {
    "move":     ["at", "adjacent", "visited"],
    "pick_up":  ["at", "item-at", "has-item"],
    "drop":     ["at", "has-item", "item-at"],
    "give":     ["at", "has-item"],
    "talk":     ["at", "alive", "knows"],
    "attack":   ["at", "alive"],
    "use_item": ["has-item", "alive"],
    "use_on": [
        "has-item", "alive", "at", "item-at", "locked", "unlocked",
        "key-for", "connects", "adjacent",
    ],
}


# ---------- domain 生成 ----------

def _format_predicate(name: str) -> str:
    params = KNOWN_PREDICATES[name]
    parts = [f"{var} - {typ}" for var, typ in params]
    return f"({name} " + " ".join(parts) + ")"


def build_domain(
    action_names: Iterable[str],
    domain_name: str = "aitown3",
) -> str:
    """根据选定的动作名生成 PDDL domain。"""
    # 延迟引入，避免顶层依赖 sys.path 注入顺序
    from registry import get_registry

    reg = get_registry()
    selected_actions = []
    needed_predicates: Set[str] = set()

    for name in action_names:
        action = reg.get(name)
        if action is None:
            raise ValueError(f"未知动作: {name}")
        selected_actions.append(action)
        for pred in ACTION_PREDICATES.get(name, []):
            needed_predicates.add(pred)

    types_block = " ".join(ALL_TYPES)
    pred_block = "\n    ".join(_format_predicate(p) for p in sorted(needed_predicates))
    actions_block = "\n\n  ".join(a.to_pddl() for a in selected_actions)

    return (
        f"(define (domain {domain_name})\n"
        f"  (:requirements :strips :typing :negative-preconditions)\n"
        f"  (:types {types_block})\n"
        f"  (:predicates\n    {pred_block})\n\n"
        f"  {actions_block})\n"
    )


# ---------- problem 生成 ----------

def _norm(name: str) -> str:
    """PDDL 对象名建议无空格；这里保持原样（数据里都是 CamelCase 单词）。"""
    return name


def collect_world_state(
    world: "Neo4jWorld",
    include_predicates: Iterable[str],
) -> Tuple[Dict[str, List[str]], List[str]]:
    """
    从 Neo4j 读出当前世界状态。

    Returns:
        (objects_by_type, init_facts)
          objects_by_type: {"person": [...], "location": [...], "item": [...]}
          init_facts:      ["(at Alice TownSquare)", "(adjacent TownSquare Tavern)", ...]
    """
    persons = world.list_entities("Person")
    locations = world.list_entities("Location")
    items = world.list_entities("Item")
    objects_by_type = {
        "person":   [_norm(n) for n in persons],
        "location": [_norm(n) for n in locations],
        "item":     [_norm(n) for n in items],
    }

    facts: List[str] = []
    include = set(include_predicates)

    if "at" in include:
        rows = world.run_query(
            "MATCH (p:Person)-[:LOCATED_AT]->(l:Location) RETURN p.name AS p, l.name AS l"
        )
        for r in rows:
            facts.append(f"(at {_norm(r['p'])} {_norm(r['l'])})")

    if "adjacent" in include:
        rows = world.run_query(
            "MATCH (a:Location)-[:ADJACENT_TO]->(b:Location) RETURN a.name AS a, b.name AS b"
        )
        for r in rows:
            facts.append(f"(adjacent {_norm(r['a'])} {_norm(r['b'])})")

    if "visited" in include:
        rows = world.run_query(
            "MATCH (p:Person)-[:VISITED]->(l:Location) RETURN p.name AS p, l.name AS l"
        )
        for r in rows:
            facts.append(f"(visited {_norm(r['p'])} {_norm(r['l'])})")

    if "has-item" in include:
        rows = world.run_query(
            "MATCH (p:Person)-[:HAS_ITEM]->(i:Item) RETURN p.name AS p, i.name AS i"
        )
        for r in rows:
            facts.append(f"(has-item {_norm(r['p'])} {_norm(r['i'])})")

    if "item-at" in include:
        rows = world.run_query(
            "MATCH (i:Item)-[:LOCATED_AT]->(l:Location) RETURN i.name AS i, l.name AS l"
        )
        for r in rows:
            facts.append(f"(item-at {_norm(r['i'])} {_norm(r['l'])})")

    if "alive" in include:
        rows = world.run_query(
            "MATCH (p:Person) WHERE coalesce(p.hp, 0) > 0 AND coalesce(p.dead, false) = false "
            "RETURN p.name AS p"
        )
        for r in rows:
            facts.append(f"(alive {_norm(r['p'])})")

    if "knows" in include:
        rows = world.run_query(
            "MATCH (a:Person)-[:KNOWS]->(b:Person) RETURN a.name AS a, b.name AS b"
        )
        for r in rows:
            facts.append(f"(knows {_norm(r['a'])} {_norm(r['b'])})")

    if "locked" in include or "unlocked" in include or "key-for" in include or "connects" in include:
        rows = world.run_query(
            """
            MATCH (i:Item)
            WHERE coalesce(i.item_kind, '') = 'door'
               OR coalesce(i.portable, true) = false
            OPTIONAL MATCH (i)-[:LOCATED_AT]->(l:Location)
            RETURN i.name AS name,
                   coalesce(i.locked, false) AS locked,
                   coalesce(i.key_item, '') AS key_item,
                   coalesce(i.connects_to, '') AS connects_to,
                   l.name AS loc
            """
        )
        # 会话交互表：key-for / connects
        inter_by_target: Dict[str, Any] = {}
        try:
            from world_interactions import (
                destination_from_interaction,
                list_interactions,
            )

            for inter in list_interactions():
                inter_by_target.setdefault(inter.target, []).append(inter)
        except Exception:
            pass

        seen_doors = set()
        for r in rows:
            name = r.get("name")
            if not name:
                continue
            seen_doors.add(name)
            n = _norm(name)
            locked = bool(r.get("locked"))
            if "locked" in include and locked:
                facts.append(f"(locked {n})")
            if "unlocked" in include and not locked:
                facts.append(f"(unlocked {n})")
            key = r.get("key_item") or ""
            loc = r.get("loc") or ""
            to = r.get("connects_to") or ""
            inters = inter_by_target.get(name) or []
            if inters:
                key = inters[0].tool
                if loc:
                    dest = destination_from_interaction(inters[0], from_loc=loc)
                    if dest:
                        to = dest
            if "key-for" in include and key:
                facts.append(f"(key-for {_norm(key)} {n})")
            if "connects" in include and loc and to:
                facts.append(f"(connects {n} {_norm(loc)} {_norm(to)})")

        # 交互目标可能不是 door 查询命中的节点
        for target, inters in inter_by_target.items():
            if target in seen_doors:
                continue
            n = _norm(target)
            inter = inters[0]
            if "key-for" in include and inter.tool:
                facts.append(f"(key-for {_norm(inter.tool)} {n})")
            loc_rows = world.run_query(
                """
                MATCH (i:Item {name: $n})-[:LOCATED_AT]->(l:Location)
                RETURN l.name AS loc, coalesce(i.locked, false) AS locked
                LIMIT 1
                """,
                n=target,
            )
            if loc_rows:
                loc = loc_rows[0].get("loc") or ""
                locked = bool(loc_rows[0].get("locked"))
                if "locked" in include and locked:
                    facts.append(f"(locked {n})")
                if "unlocked" in include and not locked:
                    facts.append(f"(unlocked {n})")
                if "connects" in include and loc:
                    dest = destination_from_interaction(inter, from_loc=loc)
                    if dest:
                        facts.append(f"(connects {n} {_norm(loc)} {_norm(dest)})")

    return objects_by_type, facts


def build_problem(
    world: "Neo4jWorld",
    problem_name: str,
    domain_name: str,
    goal_facts: List[str],
    include_predicates: Iterable[str],
) -> str:
    objects_by_type, init_facts = collect_world_state(world, include_predicates)

    # :objects 段
    object_lines = []
    for typ in ALL_TYPES:
        names = objects_by_type.get(typ, [])
        if names:
            object_lines.append(f"    {' '.join(names)} - {typ}")
    objects_block = "\n".join(object_lines) if object_lines else ""

    init_block = "\n    ".join(init_facts) if init_facts else ""
    goal_block = "\n    ".join(goal_facts) if goal_facts else ""

    if len(goal_facts) == 1:
        goal_section = f"  (:goal {goal_facts[0]}))\n"
    else:
        goal_section = f"  (:goal (and\n    {goal_block})))\n"

    return (
        f"(define (problem {problem_name})\n"
        f"  (:domain {domain_name})\n"
        f"  (:objects\n{objects_block})\n"
        f"  (:init\n    {init_block})\n"
        f"{goal_section}"
    )


# ---------- 寻路便捷 API ----------

def build_pathfinding_pddl(
    world: "Neo4jWorld",
    character: str,
    goal_location: str,
    domain_name: str = "aitown3-pathfinding",
    problem_name: Optional[str] = None,
) -> Tuple[str, str]:
    """
    生成寻路 (move-only) 的 domain + problem。

    Returns:
        (domain_str, problem_str)
    """
    if problem_name is None:
        problem_name = f"{character}-to-{goal_location}".lower()

    domain_str = build_domain(["move"], domain_name=domain_name)
    problem_str = build_problem(
        world,
        problem_name=problem_name,
        domain_name=domain_name,
        goal_facts=[f"(at {_norm(character)} {_norm(goal_location)})"],
        include_predicates=ACTION_PREDICATES["move"],
    )
    return domain_str, problem_str
