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
    # 进行中的监听器也是玩家能「做」的事：触发它的事件 + 条件 -> 它的效果（例如给某个阶段要的标记）
    heard = [a for a, ls in listener_actions(patch) if not ls.then.get("start")]

    def attempt(k: int):
        goal = [{"op": "prop_eq", "entity": player, "label": "Person", "prop": _flag(k), "value": True}]
        compiled = compile_pddl(world, player, goal, config=cfg, extra_actions=extra[:k] + heard)
        unsupported = [
            f"stage {name.rsplit('-', 1)[-1]} {part}: {note}".replace(" :", ":")
            for name, part, note in compiled.report.rows
            if name.startswith("questline-stage") and ("ignored" in note or "never applicable" in note)
        ]
        result = solve(compiled.domain, compiled.problem)
        return compiled, result, unsupported

    problem = _start_problem(world, patch, player, cfg, solve)
    if problem:
        return Verification(False, problem)

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


def _start_problem(world, patch: QuestlinePatch, player: str, cfg, solve) -> str:
    """接下任务的途径：不能一开局就成立；至少一条玩家做得到（只有「听说」时靠对话，证明不了）。

    find / visit / when 交给规划器；pattern 里能证明的步骤（走到、拿到）每一步都要做得到，
    说话 / 做动作 / 放下这类步骤只查名字（validate 里查过）。
    """
    st = patch.start
    if st.empty() and not any(ls.then.get("start") for ls in patch.listeners):
        return ""

    def plan_for(goal):
        return solve_goal(world, player, list(substitute(goal, "player", player)), cfg, solve)

    routes = []  # 每条途径能不能做到
    for cond in st.planner_conditions():
        plan = plan_for([cond])
        if plan is not None and not plan:
            return (f"start {_cond_text(cond)} is already true, so the questline would start at once; "
                    "pick something the player still has to find or reach")
        routes.append(plan is not None)
    if st.when:
        try:
            plan = plan_for(st.when)
        except Exception:  # noqa: BLE001  @here 之类规划器不认：当作证明不了，但不拦
            plan = None
        if plan is not None and not plan:
            return (f"start.when ({'; '.join(_cond_text(c) for c in st.when)}) is already true, "
                    "so the questline would start at once")
        routes.append(plan is not None)
    if st.pattern:
        steps = {"enter": lambda w: {"op": "person_at", "person": "@player", "location": w},
                 "pick_up": lambda w: {"op": "has_item", "person": "@player", "item": w}}
        ok = True
        for step in st.pattern:
            make = steps.get(step.get("event"))
            if make is None:
                continue
            plan = plan_for([make(step.get("what"))])
            if plan is None:
                ok = False
                break
        routes.append(ok)
    # 会接下任务线的监听器：能编译的看规划器做不做得到；编译不了的（等 NPC / 时辰 / 消息）证明不了，不拦
    starters = [ls for ls in patch.listeners if ls.then.get("start")]
    compiled = {ls.id: action for action, ls in listener_actions(patch)}
    for ls in starters:
        action = compiled.get(ls.id)
        if action is None:
            routes.append(None)
            continue
        goal = [substitute(c, "c", player) for c in action["requires"]]
        plan = solve_goal(world, player, goal, cfg, solve) if goal else []
        if plan is not None and not plan and not ls.on and not ls.sequence:
            return (f"listener {ls.id} watches conditions that are already true, so the questline would "
                    "start at once")
        routes.append(plan is not None)
    if None in routes:
        routes = [r for r in routes if r is not None] + [True]
    if not any(routes) and not st.hear:
        return "the player can't reach any way into the questline (check start.find / visit / when / pattern)"
    return ""


# 事件 -> 规划器里「这件事能发生」的前提（玩家做的事；时辰总会到，没有前提；其余编译不了）
_EVENT_PRECONDITION = {
    "enter": lambda w: {"op": "person_at", "person": "@c", "location": w},
    "pick_up": lambda w: {"op": "has_item", "person": "@c", "item": w},
}
_ALWAYS_HAPPENS = ("time", "hour", "new_day")


def _trigger(spec: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """一条事件描述 -> 前提列表；None = 规划器没法表达（NPC 做的事、消息、说话……）。"""
    event = spec.get("event")
    if event in _ALWAYS_HAPPENS:
        return []
    who = spec.get("who") or "anyone"
    if who not in ("@player", "anyone") or event not in _EVENT_PRECONDITION or not spec.get("what"):
        return None
    return [_EVENT_PRECONDITION[event](spec["what"])]


def listener_actions(patch: QuestlinePatch) -> List[Tuple[Dict[str, Any], Any]]:
    """能编译成规划器动作的监听器：[(内容包格式的动作, 监听器)]。

    前提 = 触发它的事件能发生（顺序事件取最后一步）+ when；效果 = then.effects。
    @player / @actor 换成执行者 @c，@here 换成触发它的地点；换不了的就不编译。
    stage:<id> 的监听器要求前一阶段已完成。
    """
    stage_k = {st.id: k for k, st in enumerate(patch.stages, 1)}
    out = []
    for ls in patch.listeners:
        if ls.sequence:
            steps = [_trigger(x) for x in ls.sequence]
            requires = None if any(x is None for x in steps) else steps[-1]
            where = ls.sequence[-1].get("what") if ls.sequence[-1].get("event") == "enter" else ""
        elif ls.on:
            requires = _trigger(ls.on)
            where = ls.on.get("what") if ls.on.get("event") == "enter" else ""
        else:
            requires, where = [], ""
        if requires is None:
            continue
        conds = substitute(substitute(ls.when, "player", "@c"), "actor", "@c")
        effects = substitute(substitute(ls.then.get("effects") or [], "player", "@c"), "actor", "@c")
        if where:
            conds, effects = substitute(conds, "here", where), substitute(effects, "here", where)
        if "@here" in str(conds) + str(effects):
            continue
        requires = list(requires) + list(conds)
        phase = ls.phase()
        if phase.startswith("stage:") and stage_k.get(phase[6:], 1) > 1:
            requires.insert(0, {"op": "prop_eq", "entity": "@c", "label": "Person",
                                "prop": _flag(stage_k[phase[6:]] - 1), "value": True})
        if not effects and not ls.then.get("start"):
            continue  # 只说话 / 提醒人的监听器不改世界，规划器用不上
        out.append(({
            "name": f"questline-listener-{ls.id.lower().replace('_', '-')}",
            "params": [{"name": "c", "type": "Person", "source": "self"}],
            "requires": requires,
            "effects": list(effects),
        }, ls))
    return out


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
