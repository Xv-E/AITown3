"""
证明任务线玩家真能做完：把补丁叠到世界快照的副本上，每个阶段变成一个 PDDL 动作
「完成第 k 阶段」（前提 = 上一阶段已完成 + 本阶段条件，效果 = 奖励 + 本阶段完成标记），
让规划器以玩家身份求「最后一个阶段完成」。

纯内存：只读快照，不写数据库。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .spec import QuestlinePatch, StageSpec, item_props, substitute

# 验证时放开交谈（村庄内容包为了 NPC 规划把它排除了）：「去找谁谈谈」是常见的任务步骤
ALLOWED_FOR_QUESTS = ("talk",)


@dataclass
class Verification:
    ok: bool
    reason: str = ""
    failed_stage: Optional[int] = None  # 1 起
    # 每个阶段玩家要做的步骤（游戏动作），给调试和提示用
    walkthrough: List[List[Tuple[str, List[str]]]] = field(default_factory=list)


def apply_to_snapshot(snap, patch: QuestlinePatch):
    """返回叠加了补丁的新快照（原快照不变）。"""
    out = copy.deepcopy(snap)
    adj = out.edges.setdefault("ADJACENT_TO", set())
    for r in patch.regions:
        out.objects[r.name] = ("Location", {"description": r.description, "tags": r.tags})
        adj.update({(r.name, r.next_to), (r.next_to, r.name)})
    for p in patch.persons:
        out.objects[p.name] = ("Person", {
            "hp": 100, "role": p.role, "mood": p.mood, "personality": p.personality, "goal": p.goal,
        })
        out.location[p.name] = p.located_at
    for it in patch.items:
        out.objects[it.name] = ("Item", item_props(it))
        if it.held_by:
            out.holder[it.name] = it.held_by
        else:
            out.location[it.name] = it.located_at
    return out


def _flag(k: int) -> str:
    return f"questline_stage_{k}_done"


def stage_actions(patch: QuestlinePatch) -> List[Dict[str, Any]]:
    """每个阶段一个内容包格式的动作；@player 换成动作的执行者 @c。"""
    out = []
    for k, st in enumerate(patch.stages, 1):
        requires = list(substitute(st.complete_when, "player", "@c"))
        if k > 1:
            requires.insert(0, {"op": "prop_eq", "entity": "@c", "label": "Person",
                                "prop": _flag(k - 1), "value": True})
        effects = list(substitute(st.reward, "player", "@c")) + [
            {"op": "set_prop", "entity": "@c", "label": "Person", "prop": _flag(k), "value": True}
        ]
        out.append({
            "name": f"questline-stage-{k}",
            "params": [{"name": "c", "type": "Person", "source": "self"}],
            "requires": requires,
            "effects": effects,
        })
    return out


def verify(snap, patch: QuestlinePatch, player: str, *, solve=None) -> Verification:
    """solve：(domain, problem) -> 求解结果（测试可替换）；默认 unified-planning。"""
    from compiler import compile_pddl, load_config

    if solve is None:
        from solver import solve_pddl

        def solve(domain: str, problem: str) -> Dict[str, Any]:
            return solve_pddl(domain, problem, engines=["unified_planning"])

    world = apply_to_snapshot(snap, patch)
    base = load_config()
    cfg = load_config({"exclude_actions": [a for a in base.exclude_actions if a not in ALLOWED_FOR_QUESTS]})
    extra = stage_actions(patch)

    def attempt(k: int):
        goal = [{"op": "prop_eq", "entity": player, "label": "Person", "prop": _flag(k), "value": True}]
        compiled = compile_pddl(world, player, goal, config=cfg, extra_actions=extra[:k])
        unsupported = [
            f"stage {name.rsplit('-', 1)[-1]} {part}: {note}".replace(" :", ":")
            for name, part, note in compiled.report.rows
            if name.startswith("questline-stage") and ("ignored" in note or "never applicable" in note)
        ]
        result = solve(compiled.domain, compiled.problem)
        return compiled, result, unsupported

    # 一开局就成立的阶段：玩家什么都不用做，不算任务
    for k, st in enumerate(patch.stages, 1):
        goal = list(substitute(st.complete_when, "player", player))
        trivial = solve_goal(world, player, goal, cfg, solve)
        if trivial is not None and not trivial:
            return Verification(
                False,
                f"stage {k} '{st.title}' is already true when the questline starts "
                f"({_conditions_text(st)}); it needs something for the player to do",
                failed_stage=k,
            )

    # 上一阶段一完成就自动成立的阶段（条件被上一阶段的条件 + 奖励全盖住）：同样没事可做
    for k in range(2, len(patch.stages) + 1):
        prev, st = patch.stages[k - 2], patch.stages[k - 1]
        implied = _implied_by(prev)
        if st.complete_when and all(_key(c) in implied for c in st.complete_when):
            return Verification(
                False,
                f"stage {k} '{st.title}' completes the moment stage {k - 1} does (its conditions "
                f"{_conditions_text(st)} are already met by stage {k - 1} and its reward); "
                "it needs something for the player to do",
                failed_stage=k,
            )

    n = len(patch.stages)
    compiled, result, unsupported = attempt(n)
    if unsupported:
        return Verification(False, "the planner can't check these conditions: " + "; ".join(unsupported[:3]))
    if not result.get("success"):
        for k in range(1, n + 1):
            _, partial, _ = attempt(k)
            if not partial.get("success"):
                return Verification(False, _cannot(k, patch.stages[k - 1]), failed_stage=k)
        return Verification(False, f"the whole line can't be finished ({result.get('error')})")

    # 每个阶段完成之前的步骤（规划器可能穿插着做，仅供参考）
    walkthrough: List[List[Tuple[str, List[str]]]] = [[]]
    for name, args in compiled.decode(result.get("plan") or []):
        if name.startswith("questline-stage-"):
            walkthrough.append([])
        else:
            walkthrough[-1].append((name, args))
    return Verification(True, walkthrough=walkthrough[:n])


def solve_goal(world, player: str, goal, cfg, solve) -> Optional[List[Tuple[str, List[str]]]]:
    """玩家达成 goal 的计划（[] = 已经成立）；做不到 / 规划器不支持返回 None。"""
    from compiler import compile_pddl

    try:
        compiled = compile_pddl(world, player, goal, config=cfg)
    except ValueError:
        return None
    result = solve(compiled.domain, compiled.problem)
    if not result.get("success"):
        return None
    return compiled.decode(result.get("plan") or [])


def _key(cond: Dict[str, Any]) -> str:
    import json

    return json.dumps({k: v for k, v in cond.items() if k != "message"}, sort_keys=True)


def _implied_by(stage: StageSpec) -> set:
    """一个阶段完成后一定成立的条件：它自己的条件，加上奖励直接造成的事实。"""
    out = {_key(c) for c in stage.complete_when}
    for op in stage.reward or []:
        kind = op.get("op")
        if kind == "spawn_item" and op.get("held_by") and op.get("name"):
            out.add(_key({"op": "has_item", "person": op["held_by"], "item": op["name"]}))
        elif kind == "link" and str(op.get("rel")).upper() == "HAS_ITEM":
            out.add(_key({"op": "has_item", "person": op.get("a"), "item": op.get("b")}))
        elif kind == "relocate":
            out.add(_key({"op": "item_at", "entity": op.get("entity"), "location": op.get("to")}))
        elif kind == "set_prop":
            base = {"op": "prop_eq", "entity": op.get("entity"), "prop": op.get("prop"), "value": op.get("value")}
            out.add(_key(base))
            if op.get("label"):
                out.add(_key({**base, "label": op["label"]}))
    return out


def _conditions_text(st: StageSpec) -> str:
    return "; ".join(_cond_text(c) for c in st.complete_when)


def _cond_text(c: Dict[str, Any]) -> str:
    op = c.get("op")
    if op == "any":
        return " or ".join(_cond_text(s) for s in c.get("of") or [])
    keys = [k for k in ("person", "item", "entity", "location", "prop", "value", "a", "rel", "b") if k in c]
    return f"{op}(" + ", ".join(f"{c[k]}" for k in keys) + ")"


def _cannot(k: int, st: StageSpec) -> str:
    return (
        f"stage {k} '{st.title}' can't be completed: from the world as it is after the earlier stages "
        f"(and their rewards), the player has no way to make all of these true: {_conditions_text(st)}. "
        "Give the player a way: put needed items where they can be reached, hand them over as a reward "
        "of an earlier stage, or add an object action that produces them."
    )
