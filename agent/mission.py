"""任务进度同步（软状态）— 不硬停自动规划。

角色下一步由 LLM 根据情境（位置、线索、记忆、背包）决定。
parent_goal 只是角色动机文案，不是完成条件清单。
本模块仅把 Quest 节点 stage/state 更新给 UI 参考。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Set, Tuple

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld


def _person_at(world: "Neo4jWorld", character: str, loc: str) -> bool:
    rows = world.run_query(
        """
        MATCH (p:Person {name: $c})-[:LOCATED_AT]->(l:Location {name: $loc})
        RETURN l.name AS n LIMIT 1
        """,
        c=character,
        loc=loc,
    )
    return bool(rows)


def _person_visited(world: "Neo4jWorld", character: str, loc: str) -> bool:
    rows = world.run_query(
        """
        MATCH (p:Person {name: $c})-[:VISITED]->(l:Location {name: $loc})
        RETURN l.name AS n LIMIT 1
        """,
        c=character,
        loc=loc,
    )
    return bool(rows)


def _quests_with_doors(world: "Neo4jWorld") -> Dict[str, List[dict]]:
    try:
        rows = world.run_query(
            """
            MATCH (q:Quest)-[:INVOLVES]->(i:Item)
            WHERE toLower(coalesce(i.item_kind, '')) = 'door'
            OPTIONAL MATCH (i)-[:LOCATED_AT]->(l:Location)
            RETURN q.name AS quest,
                   i.name AS door,
                   coalesce(i.locked, false) AS locked,
                   l.name AS door_loc,
                   toLower(coalesce(q.state, 'active')) AS state
            """
        ) or []
    except Exception:
        return {}
    out: Dict[str, List[dict]] = {}
    for r in rows:
        q = r.get("quest") or ""
        if q:
            out.setdefault(q, []).append(r)
    return out


def _quest_goal_locations(world: "Neo4jWorld", quest: str, door_locs: Set[str]) -> List[str]:
    try:
        rows = world.run_query(
            """
            MATCH (q:Quest {name: $q})-[:INVOLVES]->(l:Location)
            RETURN l.name AS n ORDER BY n
            """,
            q=quest,
        ) or []
    except Exception:
        rows = []
    locs = [r["n"] for r in rows if r.get("n")]
    targets = [loc for loc in locs if loc not in door_locs]
    return targets or locs


def sync_quest_progress(world: "Neo4jWorld", character: str = "") -> str:
    """根据世界状态轻量更新 Quest.stage/state，供 UI 显示。返回备注（可空）。

    不会宣布「角色停止行动」——是否继续规划由决策器按情境决定。
    """
    notes: List[str] = []
    by_quest = _quests_with_doors(world)
    for qname, doors in by_quest.items():
        if not doors:
            continue
        all_open = all(not bool(r.get("locked")) for r in doors)
        if not all_open:
            continue
        door_locs = {r["door_loc"] for r in doors if r.get("door_loc")}
        targets = _quest_goal_locations(world, qname, door_locs)
        reached = None
        if character:
            for loc in targets:
                if _person_at(world, character, loc) or _person_visited(world, character, loc):
                    reached = loc
                    break
        try:
            if reached:
                world.run_query(
                    """
                    MATCH (q:Quest {name: $n})
                    SET q.state = 'completed', q.stage = 'done'
                    """,
                    n=qname,
                )
                notes.append(f"{qname}: entered {reached}")
            else:
                world.run_query(
                    """
                    MATCH (q:Quest {name: $n})
                    WHERE toLower(coalesce(q.state, 'active')) = 'active'
                    SET q.stage = 'doors_open'
                    """,
                    n=qname,
                )
                notes.append(f"{qname}: doors open")
        except Exception:
            pass
    return "; ".join(notes)


def is_parent_mission_complete(
    world: "Neo4jWorld",
    character: str,
    parent_goal: str = "",
) -> Tuple[bool, str]:
    """兼容旧调用：始终返回未完成，仅同步 Quest UI。

    长期目标不再用作硬停条件；parent_goal 只应出现在决策 prompt 里作动机。
    """
    note = sync_quest_progress(world, character)
    return False, note or ""
