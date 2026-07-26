"""
具体动作 + 全局注册表

9 个动作：move / pick_up / drop / give / talk / attack / use_item / use_on / wait

约定：
  - hp 属性存在 Person 节点上；power_level 同样
  - 物品价值（如药水回血量）存在 value 属性
  - use_on：对门等目标使用持有物（如钥匙开门）
  - 同一动作多次执行幂等性由底层 MERGE / DELETE 语义决定
"""

from typing import Dict, List, Optional, Tuple

from action import (
    Action,
    Param,
    get_item_holder,
    get_item_location,
    get_person_location,
    get_property,
    remove_relationship,
    set_property,
)


# ============== move ==============

class MoveAction(Action):
    def __init__(self):
        super().__init__(
            name="move",
            description="Move character along adjacent locations from → to",
            parameters=[
                Param("c", "Person"),
                Param("from", "Location"),
                Param("to", "Location"),
            ],
            category="pathfinding",
            preconditions_pddl=[
                "(at ?c ?from)",
                "(adjacent ?from ?to)",
            ],
            effects_pddl=[
                "(not (at ?c ?from))",
                "(at ?c ?to)",
                "(visited ?c ?to)",
            ],
        )

    def check(self, world, c: str, **kwargs) -> Tuple[bool, str]:
        from_loc = kwargs["from"]
        to_loc = kwargs["to"]
        cur = get_person_location(world, c)
        if cur != from_loc:
            return False, f"{c} is not at {from_loc} (currently at {cur})"
        if not world.has_directed_relationship(
            from_loc, "Location", to_loc, "Location", "ADJACENT_TO"
        ):
            return False, f"{from_loc} cannot reach {to_loc} (no directed adjacency)"
        return True, "ok"

    def apply(self, world, c: str, **kwargs) -> None:
        from_loc = kwargs["from"]
        to_loc = kwargs["to"]
        remove_relationship(world, c, "Person", from_loc, "Location", "LOCATED_AT")
        world.link_entities(c, "Person", to_loc, "Location", "LOCATED_AT")
        world.link_entities(c, "Person", to_loc, "Location", "VISITED")


# ============== pick_up ==============

class PickUpAction(Action):
    def __init__(self):
        super().__init__(
            name="pick_up",
            description="Pick up an item at the current location",
            parameters=[
                Param("c", "Person"),
                Param("i", "Item"),
                Param("l", "Location"),
            ],
            category="item",
            preconditions_pddl=[
                "(at ?c ?l)",
                "(item-at ?i ?l)",
                "(not (has-item ?c ?i))",
            ],
            effects_pddl=[
                "(has-item ?c ?i)",
                "(not (item-at ?i ?l))",
            ],
        )

    def check(self, world, c: str, i: str, l: str) -> Tuple[bool, str]:
        loc = get_person_location(world, c)
        if loc is None:
            return False, f"{c} has no current location"
        if l != loc:
            return False, f"Location param {l} does not match {c}'s current location {loc}"
        item_loc = get_item_location(world, i)
        if item_loc != loc:
            return False, f"Item {i} is not at {c}'s location {loc} (at {item_loc})"
        if get_item_holder(world, i) is not None:
            return False, f"Item {i} is already held by someone else"
        kind = (get_property(world, i, "Item", "item_kind") or "") or ""
        portable = get_property(world, i, "Item", "portable")
        if str(kind).lower() == "door" or portable is False:
            return False, f"{i} is not pickable"
        return True, "ok"

    def apply(self, world, c: str, i: str, l: str) -> None:
        loc = get_person_location(world, c)
        if loc:
            remove_relationship(world, i, "Item", loc, "Location", "LOCATED_AT")
        world.link_entities(c, "Person", i, "Item", "HAS_ITEM")


# ============== drop ==============

class DropAction(Action):
    def __init__(self):
        super().__init__(
            name="drop",
            description="Drop a held item at the current location",
            parameters=[
                Param("c", "Person"),
                Param("i", "Item"),
                Param("l", "Location"),
            ],
            category="item",
            preconditions_pddl=[
                "(has-item ?c ?i)",
                "(at ?c ?l)",
            ],
            effects_pddl=[
                "(not (has-item ?c ?i))",
                "(item-at ?i ?l)",
            ],
        )

    def check(self, world, c: str, i: str, l: str) -> Tuple[bool, str]:
        if get_item_holder(world, i) != c:
            return False, f"{c} is not holding {i}"
        loc = get_person_location(world, c)
        if loc is None:
            return False, f"{c} has no current location"
        if l != loc:
            return False, f"Location param {l} does not match {c}'s current location {loc}"
        return True, "ok"

    def apply(self, world, c: str, i: str, l: str) -> None:
        loc = get_person_location(world, c)
        remove_relationship(world, c, "Person", i, "Item", "HAS_ITEM")
        if loc:
            world.link_entities(i, "Item", loc, "Location", "LOCATED_AT")


# ============== give ==============

class GiveAction(Action):
    def __init__(self):
        super().__init__(
            name="give",
            description="Give a held item to another person at the same location",
            parameters=[
                Param("giver", "Person"),
                Param("i", "Item"),
                Param("receiver", "Person"),
                Param("l", "Location"),
            ],
            category="item",
            preconditions_pddl=[
                "(has-item ?giver ?i)",
                "(at ?giver ?l)",
                "(at ?receiver ?l)",
            ],
            effects_pddl=[
                "(not (has-item ?giver ?i))",
                "(has-item ?receiver ?i)",
            ],
        )

    def check(self, world, giver: str, i: str, receiver: str, l: str) -> Tuple[bool, str]:
        if get_item_holder(world, i) != giver:
            return False, f"{giver} is not holding {i}"
        loc_g = get_person_location(world, giver)
        loc_r = get_person_location(world, receiver)
        if loc_g is None or loc_r is None or loc_g != loc_r:
            return False, f"{giver}({loc_g}) and {receiver}({loc_r}) are not at the same location"
        if l != loc_g:
            return False, f"Location param {l} does not match both parties' location {loc_g}"
        return True, "ok"

    def apply(self, world, giver: str, i: str, receiver: str, l: str) -> None:
        remove_relationship(world, giver, "Person", i, "Item", "HAS_ITEM")
        world.link_entities(receiver, "Person", i, "Item", "HAS_ITEM")


# ============== talk ==============

class TalkAction(Action):
    def __init__(self):
        super().__init__(
            name="talk",
            description="Two people at the same location talk; create KNOWS both ways",
            parameters=[
                Param("a", "Person"),
                Param("b", "Person"),
                Param("l", "Location"),
            ],
            category="social",
            preconditions_pddl=[
                "(at ?a ?l)",
                "(at ?b ?l)",
                "(alive ?a)",
                "(alive ?b)",
            ],
            effects_pddl=[
                "(knows ?a ?b)",
                "(knows ?b ?a)",
            ],
        )

    def check(self, world, a: str, b: str, l: str) -> Tuple[bool, str]:
        if a == b:
            return False, "Cannot talk to yourself"
        loc_a = get_person_location(world, a)
        loc_b = get_person_location(world, b)
        if loc_a is None or loc_b is None or loc_a != loc_b:
            return False, f"{a}({loc_a}) and {b}({loc_b}) are not at the same location"
        if l != loc_a:
            return False, f"Location param {l} does not match both parties' location {loc_a}"
        for n in (a, b):
            hp = get_property(world, n, "Person", "hp")
            if hp is None or hp <= 0:
                return False, f"{n} is dead or has no hp set"
        return True, "ok"

    def apply(self, world, a: str, b: str, l: str) -> None:
        world.link_entities(a, "Person", b, "Person", "KNOWS")
        world.link_entities(b, "Person", a, "Person", "KNOWS")


# ============== attack ==============

class AttackAction(Action):
    def __init__(self):
        super().__init__(
            name="attack",
            description="Attack a target at the same location; reduce HP by attacker.power_level",
            parameters=[
                Param("attacker", "Person"),
                Param("target", "Person"),
                Param("l", "Location"),
            ],
            category="combat",
            preconditions_pddl=[
                "(alive ?attacker)",
                "(alive ?target)",
                "(at ?attacker ?l)",
                "(at ?target ?l)",
            ],
            effects_pddl=[
                "(decrease (hp ?target) (power_level ?attacker))",
            ],
        )

    def check(self, world, attacker: str, target: str, l: str) -> Tuple[bool, str]:
        if attacker == target:
            return False, "Cannot attack yourself"
        for n in (attacker, target):
            hp = get_property(world, n, "Person", "hp")
            if hp is None or hp <= 0:
                return False, f"{n} is dead or has no hp set"
        loc_a = get_person_location(world, attacker)
        loc_b = get_person_location(world, target)
        if loc_a is None or loc_b is None or loc_a != loc_b:
            return False, f"{attacker}({loc_a}) and {target}({loc_b}) are not at the same location"
        if l != loc_a:
            return False, f"Location param {l} does not match both parties' location {loc_a}"
        return True, "ok"

    def apply(self, world, attacker: str, target: str, l: str) -> None:
        power = get_property(world, attacker, "Person", "power_level") or 5
        cur_hp = get_property(world, target, "Person", "hp") or 0
        new_hp = max(cur_hp - int(power), 0)
        set_property(world, target, "Person", "hp", new_hp)
        # 如被击败，标记 dead 属性方便其它动作筛查
        if new_hp == 0:
            set_property(world, target, "Person", "dead", True)


# ============== use_item ==============

class UseItemAction(Action):
    """自用持有物品（目前：Potion 加血）。开门请用 use_on。"""

    def __init__(self):
        super().__init__(
            name="use_item",
            description="Use a held item (Potion restores HP; consumed on use)",
            parameters=[
                Param("c", "Person"),
                Param("i", "Item"),
            ],
            category="item",
            preconditions_pddl=[
                "(has-item ?c ?i)",
                "(alive ?c)",
            ],
            effects_pddl=[
                "(not (has-item ?c ?i))",
            ],
        )

    def check(self, world, c: str, i: str) -> Tuple[bool, str]:
        if get_item_holder(world, i) != c:
            return False, f"{c} is not holding {i}"
        hp = get_property(world, c, "Person", "hp")
        if hp is None or hp <= 0:
            return False, f"{c} is dead"
        if i.lower().startswith("potion") or i == "Potion":
            return True, "ok"
        kind = str(get_property(world, i, "Item", "item_kind") or "").lower()
        if kind == "key":
            return False, f"{i} should be used on a door (use_on), not use_item"
        if kind in ("food", "meal", "cooked"):
            return True, "ok"
        return True, "ok"

    def apply(self, world, c: str, i: str) -> None:
        kind = str(get_property(world, i, "Item", "item_kind") or "").lower()
        if i.lower().startswith("potion") or i == "Potion" or kind in ("food", "meal", "cooked"):
            value = get_property(world, i, "Item", "value") or 0
            cur_hp = get_property(world, c, "Person", "hp") or 0
            set_property(world, c, "Person", "hp", min(120, int(cur_hp) + int(value)))
            # 吃饭后略提振心情（若有 mood 字段）
            if kind in ("food", "meal", "cooked"):
                set_property(world, c, "Person", "mood", "content")
        world.delete_entity(i, label="Item")


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
    执行物品上的临时 Action（LLM 写 Cypher，挂在 Item.ephemeral_actions）。
    option=auto：必要时先合成脚本，再选一个临时 action id 执行。
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


# ============== wait ==============

class WaitAction(Action):
    """原地等待；用于无可行动作或重规划退避。"""

    def __init__(self):
        super().__init__(
            name="wait",
            description="Wait in place for a moment",
            parameters=[Param("c", "Person")],
            category="general",
            preconditions_pddl=["(alive ?c)"],
            effects_pddl=[],
        )

    def check(self, world, c: str) -> Tuple[bool, str]:
        hp = get_property(world, c, "Person", "hp")
        if hp is None or hp <= 0:
            return False, f"{c} is dead"
        return True, "ok"

    def apply(self, world, c: str) -> None:
        return None


# ============== Registry ==============

class ActionRegistry:
    """全局动作注册表（单例语义，但允许多次构造，结果一致）。"""

    def __init__(self) -> None:
        self._actions: Dict[str, Action] = {}
        for action in [
            MoveAction(),
            PickUpAction(),
            DropAction(),
            GiveAction(),
            TalkAction(),
            AttackAction(),
            UseItemAction(),
            UseOnAction(),
            InteractAction(),
            WaitAction(),
        ]:
            self._actions[action.name] = action

    def get(self, name: str) -> Optional[Action]:
        return self._actions.get(name)

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
