"""
目标术语定义 — 可转换为 PDDL 目标的预定义词汇。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class GoalTerm(Enum):
    STRENGTHEN_ABILITIES = "Strengthen abilities"
    GAIN_INFORMATION = "Gain information"
    EXPLORE_UNKNOWN_LOCATION = "Explore unknown location"
    ACCUMULATE_ITEMS = "Accumulate items"
    GO_TO_LOCATION = "Go to location"
    DEFEAT_ENEMY = "Defeat enemy"
    COLLECT_ITEM = "Collect item"
    ENHANCE_CAPABILITIES = "Enhance capabilities"
    USE_ITEM_ON = "Use item on"
    USE_ITEM = "Use item"
    INTERACT_WITH = "Interact with"
    PERFORM_ACTION = "Perform action"
    FORM_ALLIANCE = "Form alliance"
    FULFIL_REQUEST = "Fulfil request"


@dataclass
class GoalDefinition:
    term: GoalTerm
    description: str
    pddl_predicates: List[Dict[str, object]]
    example: str


GOAL_DEFINITIONS: Dict[GoalTerm, GoalDefinition] = {
    GoalTerm.STRENGTHEN_ABILITIES: GoalDefinition(
        term=GoalTerm.STRENGTHEN_ABILITIES,
        description="增强角色的能力（如提升等级、装备物品）",
        pddl_predicates=[
            {"predicate": "has-item", "args": ["?character", "?item"]},
        ],
        example="增强Hero的能力，提升等级或装备更好的物品",
    ),
    GoalTerm.GAIN_INFORMATION: GoalDefinition(
        term=GoalTerm.GAIN_INFORMATION,
        description="获取信息（如与NPC对话、探索知识）",
        pddl_predicates=[
            {"predicate": "knows", "args": ["?character", "?other"]},
            {"predicate": "visited", "args": ["?character", "?location"]},
        ],
        example="与NPC对话获取任务线索",
    ),
    GoalTerm.EXPLORE_UNKNOWN_LOCATION: GoalDefinition(
        term=GoalTerm.EXPLORE_UNKNOWN_LOCATION,
        description="探索未知位置",
        pddl_predicates=[
            {"predicate": "visited", "args": ["?character", "?location"]},
            {"predicate": "at", "args": ["?character", "?location"]},
        ],
        example="探索Forest位置",
    ),
    GoalTerm.ACCUMULATE_ITEMS: GoalDefinition(
        term=GoalTerm.ACCUMULATE_ITEMS,
        description="积累物品",
        pddl_predicates=[
            {"predicate": "has-item", "args": ["?character", "?item"]},
        ],
        example="收集Sword和Shield",
    ),
    GoalTerm.GO_TO_LOCATION: GoalDefinition(
        term=GoalTerm.GO_TO_LOCATION,
        description="前往指定位置",
        pddl_predicates=[
            {"predicate": "at", "args": ["?character", "?location"]},
        ],
        example="前往Castle位置",
    ),
    GoalTerm.DEFEAT_ENEMY: GoalDefinition(
        term=GoalTerm.DEFEAT_ENEMY,
        description="击败敌人",
        pddl_predicates=[],
        example="击败Guard",
    ),
    GoalTerm.COLLECT_ITEM: GoalDefinition(
        term=GoalTerm.COLLECT_ITEM,
        description="收集特定物品",
        pddl_predicates=[
            {"predicate": "has-item", "args": ["?character", "?item"]},
        ],
        example="收集Sword",
    ),
    GoalTerm.ENHANCE_CAPABILITIES: GoalDefinition(
        term=GoalTerm.ENHANCE_CAPABILITIES,
        description="增强能力（同Strengthen abilities）",
        pddl_predicates=[
            {"predicate": "has-item", "args": ["?character", "?item"]},
        ],
        example="通过装备物品增强能力",
    ),
    GoalTerm.USE_ITEM_ON: GoalDefinition(
        term=GoalTerm.USE_ITEM_ON,
        description="对可交互目标使用持有物品（效果由世界临时交互 / effects_on 决定）",
        pddl_predicates=[
            {"predicate": "unlocked", "args": ["?target"]},
        ],
        example="Use DoorKey on VaultDoor (ops from world JSON); missing tools are fetched or crafted first",
    ),
    GoalTerm.USE_ITEM: GoalDefinition(
        term=GoalTerm.USE_ITEM,
        description="自己使用 / 食用手上或附近的物品（target_entity 可为物品名或 kind:<类别>）",
        pddl_predicates=[],
        example="Use item kind:meal",
    ),
    GoalTerm.INTERACT_WITH: GoalDefinition(
        term=GoalTerm.INTERACT_WITH,
        description="与可交互物品执行某个临时 action（interact_action=chop 等）；每个临时 action 是独立规划候选",
        pddl_predicates=[
            {"predicate": "at", "args": ["?character", "?location"]},
        ],
        example="Interact with OakTree (interact_action=chop)",
    ),
    GoalTerm.PERFORM_ACTION: GoalDefinition(
        term=GoalTerm.PERFORM_ACTION,
        description="执行一个只需自己参与的内容动作（target_entity=动作名，如 sleep；可选 at_location 先走过去）",
        pddl_predicates=[],
        example="Perform action sleep at Cottages",
    ),
    GoalTerm.FORM_ALLIANCE: GoalDefinition(
        term=GoalTerm.FORM_ALLIANCE,
        description="与某人结盟（target_entity=人；对方对你的好感够高才会答应），结盟的人会聚成一个联盟",
        pddl_predicates=[],
        example="Form alliance with Sage",
    ),
    GoalTerm.FULFIL_REQUEST: GoalDefinition(
        term=GoalTerm.FULFIL_REQUEST,
        description="帮别人完成委托：把他要的那类东西弄来交给他（target_entity=发委托的人），他会付酬劳",
        pddl_predicates=[],
        example="Fulfil request of Reed",
    ),
}


def get_all_goal_terms() -> List[str]:
    return [term.value for term in GoalTerm]


def get_goal_definition(term: str) -> Optional[GoalDefinition]:
    for goal_term, definition in GOAL_DEFINITIONS.items():
        if goal_term.value == term:
            return definition
    return None


def goal_term_exists(term: str) -> bool:
    return get_goal_definition(term) is not None
