"""
目标到 PDDL 转换 — 映射到 AITown3 已有谓词。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from goal_terms import get_goal_definition

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld


# 目标术语 -> (所需动作, 所需谓词集合)
GOAL_ACTION_MAP: Dict[str, Tuple[List[str], List[str]]] = {
    "Go to location": (["move"], ["at", "adjacent", "visited"]),
    "Explore unknown location": (["move"], ["at", "adjacent", "visited"]),
    "Gain information": (["move", "talk"], ["at", "adjacent", "visited", "alive", "knows"]),
    "Collect item": (["move", "pick_up"], ["at", "adjacent", "visited", "has-item", "item-at"]),
    "Accumulate items": (["move", "pick_up"], ["at", "adjacent", "visited", "has-item", "item-at"]),
    "Strengthen abilities": (["move", "pick_up"], ["at", "adjacent", "visited", "has-item", "item-at"]),
    "Enhance capabilities": (["move", "pick_up"], ["at", "adjacent", "visited", "has-item", "item-at"]),
    "Use item on": (
        ["move", "use_on"],
        [
            "at", "adjacent", "visited", "has-item", "item-at", "alive",
            "locked", "unlocked", "key-for", "connects",
        ],
    ),
}


class GoalToPDDLConverter:
    def __init__(self, world: Optional["Neo4jWorld"] = None):
        self.world = world

    def convert_goal_to_pddl(
        self,
        goal_data: Dict[str, Any],
        character_name: str,
    ) -> List[str]:
        goal_term = goal_data.get("goal", "")
        target_entity = goal_data.get("target_entity")

        if goal_term == "Go to location":
            if target_entity:
                return [f"(at {character_name} {target_entity})"]
            return [f"(alive {character_name})"]

        if goal_term == "Explore unknown location":
            if target_entity:
                return [f"(visited {character_name} {target_entity})"]
            return [f"(visited {character_name} Forest)"]

        if goal_term in ("Collect item", "Accumulate items", "Strengthen abilities", "Enhance capabilities"):
            if target_entity:
                return [f"(has-item {character_name} {target_entity})"]
            return [f"(has-item {character_name} Potion)"]

        if goal_term == "Use item on":
            if target_entity:
                return [f"(unlocked {target_entity})"]
            return [f"(unlocked VaultDoor)"]

        if goal_term == "Gain information":
            if target_entity and not self._is_location(target_entity):
                return [f"(knows {character_name} {target_entity})"]
            if target_entity and self._is_location(target_entity):
                return [f"(visited {character_name} {target_entity})"]
            return [f"(knows {character_name} Bob)"]

        if goal_term == "Defeat enemy":
            return []  # 规则计划，不走 PDDL

        definition = get_goal_definition(goal_term)
        if definition and definition.pddl_predicates:
            pred = definition.pddl_predicates[0]
            predicate = pred.get("predicate", "at")
            if predicate == "has-item" and target_entity:
                return [f"(has-item {character_name} {target_entity})"]
            if predicate == "at" and target_entity:
                return [f"(at {character_name} {target_entity})"]

        if target_entity:
            return [f"(at {character_name} {target_entity})"]
        return [f"(alive {character_name})"]

    def get_actions_for_goal(self, goal_term: str) -> Tuple[List[str], List[str]]:
        return GOAL_ACTION_MAP.get(goal_term, (["move"], ["at", "adjacent", "visited"]))

    def _is_location(self, entity_name: str) -> bool:
        if not self.world:
            return False
        try:
            return entity_name in self.world.list_entities("Location")
        except Exception:
            return False
