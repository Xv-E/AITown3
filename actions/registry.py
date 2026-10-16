"""
全局动作注册表 = 引擎原生动作 + 内容包数据动作。

引擎原生（交互机制本身，不是具体玩法）：
  use_on    对目标使用持有物 —— 效果由世界里的 effects_on × accepts_from 决定
  interact  执行物品上的临时 action（LLM 写的 Cypher）

其余动作（移动、拾取、对话、睡觉…）全部来自 content/<pack>/actions/*.json，
见 data_action.DataAction；规划器通过 capability 找到它们。
"""

from typing import Dict, List, Optional, Tuple

from action import (
    Action,
    Param,
    get_item_holder,
    get_person_location,
    get_property,
)


# ============== use_on ==============

class UseOnAction(Action):
    """
    对目标使用持有物：匹配当前世界临时交互表，执行配置的图操作。
    具体谜题（如开 Vault）只写在世界 JSON 的 effects_on.ops 里。
    """

    def __init__(self):
        super().__init__(
            name="use_on",
            description="Use a held item on a target (effects from world graph ops)",
            parameters=[
                Param("c", "Person"),
                Param("i", "Item"),
                Param("target", "Item"),
                Param("from", "Location"),
                Param("to", "Location"),
            ],
            category="item",
            preconditions_pddl=[
                "(has-item ?c ?i)",
                "(alive ?c)",
                "(at ?c ?from)",
                "(item-at ?target ?from)",
                "(locked ?target)",
                "(key-for ?i ?target)",
                "(connects ?target ?from ?to)",
            ],
            effects_pddl=[
                "(not (locked ?target))",
                "(unlocked ?target)",
                "(adjacent ?from ?to)",
                "(adjacent ?to ?from)",
                "(not (has-item ?c ?i))",
            ],
        )
        self.duration = 1.5

    def check(self, world, c: str, i: str, target: str, **kwargs) -> Tuple[bool, str]:
        from world_interactions import can_use_on, target_location

        from_loc = kwargs.get("from")
        if get_item_holder(world, i) != c:
            return False, f"{c} is not holding {i}"
        hp = get_property(world, c, "Person", "hp")
        if hp is None or hp <= 0:
            return False, f"{c} is dead"
        cur = get_person_location(world, c)
        if from_loc and cur != from_loc:
            return False, f"{c} is not at {from_loc} (currently at {cur})"
        t_loc = target_location(world, target)
        if t_loc is None:
            return False, f"Target not found: {target}"
        if from_loc and t_loc != from_loc:
            return False, f"{target} is not at {from_loc} (at {t_loc})"
        ok, reason, _ = can_use_on(world, c, i, target)
        if not ok:
            return False, reason
        return True, "ok"

    def apply(self, world, c: str, i: str, target: str, **kwargs) -> None:
        from world_interactions import execute_use_on

        ok, reason = execute_use_on(world, c, i, target)
        if not ok:
            raise RuntimeError(reason)


# ============== interact ==============

class InteractAction(Action):
    """
    执行物品上的临时 Action（LLM 发明的声明式 requires / effects，挂在 Item.ephemeral_actions；
    旧存档里的 Cypher 版本也能执行）。
    option=auto：必要时先合成，再选一个临时 action id 执行。
    """

    def __init__(self):
        super().__init__(
            name="interact",
            description="Run a temporary interact Action on an item (Cypher, not hardcoded)",
            parameters=[
                Param("c", "Person"),
                Param("target", "Item"),
                Param("option", "Item"),  # 临时 action id；auto=自动
            ],
            category="item",
            preconditions_pddl=[
                "(alive ?c)",
                "(at ?c ?loc)",
                "(item-at ?target ?loc)",
            ],
            effects_pddl=[],
        )
        self.duration = 3.0

    def check(self, world, c: str, target: str, option: str = "auto") -> Tuple[bool, str]:
        from item_interact import is_interactive_item

        hp = get_property(world, c, "Person", "hp")
        if hp is None or hp <= 0:
            return False, f"{c} is dead"
        if not world.get_entity_by_name(target, label="Item"):
            return False, f"Unknown item: {target}"
        if not is_interactive_item(world, target):
            return False, f"{target} has no interactive script"
        cur = get_person_location(world, c)
        rows = world.run_query(
            "MATCH (i:Item {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1",
            n=target,
        )
        t_loc = rows[0]["loc"] if rows else None
        if not t_loc:
            return False, f"{target} is not on the ground"
        if cur != t_loc:
            return False, f"{c} and {target} are not at the same location"
        if option and option not in ("auto", "_", "-"):
            # 计划里点名的声明式临时动作：连同它自己的 requires 一起预检
            from ephemeral_action_dsl import check_ephemeral, find_action, load_actions_from_item

            chosen = find_action(load_actions_from_item(world, target), option)
            if chosen is not None:
                return check_ephemeral(world, chosen, actor=c, target=target)
        return True, "ok"

    def apply(self, world, c: str, target: str, option: str = "auto") -> None:
        from item_interact import resolve_interaction

        result = resolve_interaction(
            world,
            c,
            target,
            option or "auto",
            force_mock=bool(getattr(__import__("item_interact"), "FORCE_MOCK", False)),
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Interaction failed")


ENGINE_ACTIONS = (UseOnAction, InteractAction)


# ============== Registry ==============

class ActionRegistry:
    """引擎原生动作 + 当前内容包里的数据动作（同名时内容包优先）。"""

    def __init__(self) -> None:
        from content import get_content
        from data_action import DataAction

        self._actions: Dict[str, Action] = {}
        for cls in ENGINE_ACTIONS:
            action = cls()
            self._actions[action.name] = action
        content = get_content()
        for spec in content.actions.values():
            self._actions[spec["name"]] = DataAction(spec)

    def get(self, name: str) -> Optional[Action]:
        return self._actions.get(name)

    def for_capability(self, capability: str) -> Optional[Action]:
        from content import get_content

        return self.get(get_content().action_for(capability))

    def all(self) -> List[Action]:
        return list(self._actions.values())

    def names(self) -> List[str]:
        return list(self._actions.keys())

    def by_category(self, category: str) -> List[Action]:
        return [a for a in self._actions.values() if a.category == category]

    def execute(self, world, name: str, **kwargs) -> Tuple[bool, str]:
        action = self.get(name)
        if action is None:
            return False, f"Unknown action: {name}"
        return action.execute(world, **kwargs)

    def to_pddl_block(self) -> str:
        return "\n\n".join(a.to_pddl() for a in self._actions.values())


_registry: Optional[ActionRegistry] = None


def get_registry() -> ActionRegistry:
    global _registry
    if _registry is None:
        _registry = ActionRegistry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None


def act(capability: str) -> str:
    """规划器用：能力 → 当前内容包里的动作名。"""
    from content import get_content

    return get_content().action_for(capability)
