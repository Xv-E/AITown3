"""
行为日志 → 人能读懂的句子（给玩家看的历史面板）。

GameLoop 记的是结构化数据（动作名 + 参数、目标 dict）；这里只负责「怎么说」，
从 viewer（历史的主人）的角度：同一段对话，发起的人看到「和 Sage 聊了聊」，
被搭话的人看到「Ash 过来找他说话」。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

NO_WORDS = "no words recorded: talk is only written in LLM mode, Shift+P"


def pretty(name: Any) -> str:
    """BerryGrove → Berry Grove；kind:wood → some wood。"""
    text = str(name or "")
    if text.startswith("kind:"):
        return f"some {text[5:].replace('_', ' ')}"
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)


def goal_phrase(goal: Optional[Dict[str, Any]], world: Any = None) -> str:
    goal = goal or {}
    term = str(goal.get("goal") or goal.get("final_goal") or "")
    target = pretty(goal.get("target_entity"))
    action = goal.get("interact_action") or (goal.get("metadata") or {}).get("interact_action")
    phrases = {
        "Go to location": f"go to {target}",
        "Explore unknown location": f"explore {target}" if target else "explore somewhere new",
        "Collect item": f"get {target}",
        "Accumulate items": f"gather {target}",
        "Strengthen abilities": f"get {target} to grow stronger",
        "Enhance capabilities": f"get {target}",
        "Use item on": f"use something on {target}",
        "Use item": f"use {target}",
        "Gain information": f"talk to {target}",
        "Defeat enemy": f"fight {target}",
        "Perform action": target.lower(),
        "Achieve": "make something happen",
    }
    if term == "Interact with":
        label = _action_label(world, goal.get("target_entity"), action) if action else ""
        return f"{label.lower()} ({target})" if label else f"do something with {target}"
    return phrases.get(term, f"{term.lower()} {target}".strip())


def entry_line(entry: Dict[str, Any], viewer: str, world: Any = None) -> str:
    kind = entry.get("kind")
    if kind == "decision":
        return _decision_line(entry, world)
    if kind in ("start", "done", "failed") and entry.get("action"):
        return _step_line(kind, entry, viewer, world)
    if kind == "replan":
        why = entry.get("reason")
        return f"The plan fell through{f' ({why})' if why else ''}; thinking again."
    if kind == "news":
        return f"News: {entry.get('text')}"
    if kind == "story":
        return f"Had a thought / heard a rumour: {entry.get('text')}"
    if kind == "conflict":
        text = str(entry.get("text") or "")
        m = re.match(r"Lost (\S+) to (\S+)", text)
        if m:
            return f"{pretty(m.group(2))} got to {pretty(m.group(1))} first. Annoying."
        m = re.match(r"Took (\S+) that (\S+) wanted", text)
        if m:
            return f"Took {pretty(m.group(1))}, but {pretty(m.group(2))} wanted it too."
        return text
    return str(entry.get("text") or "")


def _decision_line(entry: Dict[str, Any], world: Any) -> str:
    goal = entry.get("goal")
    phrase = goal_phrase(goal, world) if goal else ""
    outcome = entry.get("outcome")
    if outcome == "kept":
        return f"Thought it over and kept going: {phrase}." if phrase else "Thought it over and kept going."
    if outcome == "wait":
        return "Couldn't find anything useful to do, so waits."
    if outcome == "satisfied":
        return f"Wanted to {phrase}, but that's already done."
    if outcome == "none" or not phrase:
        return "Couldn't decide what to do."
    return f"Decided to {phrase}."


def _step_line(kind: str, entry: Dict[str, Any], viewer: str, world: Any) -> str:
    from content import get_content

    action = str(entry.get("action"))
    args: List[str] = [str(a) for a in entry.get("args") or []]
    arg = lambda i: pretty(args[i]) if i < len(args) else ""  # noqa: E731
    cap = get_content().capability_of(action) or action
    message = str(entry.get("message") or "")

    if cap == "converse":
        other = arg(1) if args and args[0] == viewer else arg(0)
        if kind == "plan":
            return f"Talk with {other}"
        if kind == "start":
            return f"Goes over to talk with {other}."
        if kind == "failed":
            return f"Wanted to talk with {other}, but couldn't: {message}"
        return f"Talked with {other}." if args and args[0] == viewer else f"{other} came over to talk."

    if action == "interact":
        label = _action_label(world, args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else "")
        doing = (label or "use").lower()
        if kind == "plan":
            return f"{doing.capitalize()} ({arg(1)})"
        if kind == "start":
            return f"Goes to {doing} ({arg(1)})."
        if kind == "failed":
            return f"Tried to {doing} ({arg(1)}), but couldn't: {message}"
        return message if message and message != "ok" else f"Did it: {doing} ({arg(1)})."

    start = {
        "travel": f"Sets off for {arg(2)}.",
        "take": f"Goes to pick up {arg(1)}.",
        "give": f"Goes to give {arg(1)} to {arg(2)}.",
        "attack": f"Goes for {arg(1)}!",
        "consume": f"Is about to use {arg(1)}.",
        "drop": f"Puts down {arg(1)}.",
        "idle": "Waits a moment.",
    }
    done = {
        "travel": f"Arrived at {arg(2)}.",
        "take": f"Picked up {arg(1)}.",
        "give": f"Gave {arg(1)} to {arg(2)}.",
        "attack": f"Attacked {arg(1)}.",
        "consume": f"Used {arg(1)}.",
        "drop": f"Dropped {arg(1)}.",
        "idle": "Waited.",
    }
    tried = {
        "travel": f"go to {arg(2)}",
        "take": f"pick up {arg(1)}",
        "give": f"give {arg(1)} to {arg(2)}",
        "attack": f"attack {arg(1)}",
        "consume": f"use {arg(1)}",
        "drop": f"drop {arg(1)}",
        "idle": "wait",
    }
    if action == "use_on":
        start["use_on"] = f"Takes {arg(1)} over to {arg(2)}."
        done["use_on"] = f"Used {arg(1)} on {arg(2)}."
        tried["use_on"] = f"use {arg(1)} on {arg(2)}"
        cap = "use_on"
    if kind == "plan":
        todo = tried.get(cap, action.replace("_", " "))
        return todo[:1].upper() + todo[1:]
    if kind == "start":
        return start.get(cap, f"Starts to {action.replace('_', ' ')}.")
    if kind == "failed":
        return f"Tried to {tried.get(cap, action.replace('_', ' '))}, but couldn't: {message}"
    return done.get(cap, f"Did: {action.replace('_', ' ')}.")


def _action_label(world: Any, item: Any, action_id: Any) -> str:
    """物品动作的显示名（作者写的 label），找不到就用 id。"""
    if not action_id:
        return ""
    if world is not None and item:
        try:
            from ephemeral_action_dsl import find_action, load_actions_from_item

            found = find_action(load_actions_from_item(world, str(item)), str(action_id))
            if found is not None and found.label:
                return found.label
        except Exception:
            pass
    return str(action_id).replace("_", " ")


def plan_step_line(action: Any, args: List[Any], viewer: str, world: Any = None) -> str:
    """计划里的一步，写成待办："Go to Forest" / "Chop (Oak Tree)" / "Talk with Sage"。"""
    try:
        return _step_line("plan", {"action": action, "args": list(args or [])}, viewer, world)
    except Exception:  # noqa: BLE001
        return f"{action}({', '.join(str(a) for a in args or [])})"


def plan_view(state: Any, sim_time: float, world: Any = None) -> Dict[str, Any]:
    """当前计划（每步：已完成 / 正在做 / 待做）+ 此刻在干什么、做到几成。"""
    name = state.agent.name
    plan = list(state.current_plan or [])
    pending = state.pending_action
    working = pending is not None and not pending.hold
    steps = []
    for i, step in enumerate(plan):
        try:
            action, args = step
        except (TypeError, ValueError):
            action, args = str(step), []
        if i < state.plan_index:
            mark = "done"
        elif i == state.plan_index and working:
            mark = "current"
        else:
            mark = "todo"
        steps.append({
            "line": plan_step_line(action, args, name, world),
            "text": f"{action}({', '.join(str(a) for a in args)})",
            "state": mark,
        })

    progress, phase, remaining = None, "", 0.0
    if pending is not None and pending.duration > 0:
        elapsed = max(0.0, sim_time - pending.started_at)
        progress = min(1.0, elapsed / pending.duration)
        remaining = max(0.0, pending.completes_at - sim_time)
        phase = "walking" if elapsed < (pending.walk_seconds or 0.0) else "doing"

    current = steps[state.plan_index]["line"] if working and state.plan_index < len(steps) else ""
    status = state.status or "idle"
    engaged_by = getattr(state, "engaged_by", None)
    if getattr(state, "held_state", None):
        label = f"{pretty(state.held_state).capitalize()}"
    elif pending is not None and pending.hold and status == "talking":
        label = "Finishing a conversation"
    elif pending is not None and pending.hold:
        who = getattr(state, "waiting_for", None)
        label = f"Waiting for {pretty(who)} to be free to talk" if who else "Waiting"
    elif engaged_by and status == "waiting":
        label = f"Waiting to talk with {pretty(engaged_by)}"
    elif working and phase == "walking":
        label = f"On the way: {current}"
    elif working:
        label = f"Doing: {current}"
    elif status in ("deciding", "planning"):
        label = "Thinking about what to do next"
    elif status == "failed":
        why = state.plan_error or state.replan_reason or ""
        label = f"The plan failed{f': {why}' if why else ''}"
    elif state.plan_index < len(steps):
        label = "About to start the next step"
    else:
        label = "Nothing to do right now"
    return {
        "steps": steps,
        "step": min(state.plan_index + 1, len(steps)) if steps else 0,
        "now": {
            "status": status,
            "label": label,
            "phase": phase,
            "progress": progress,
            "remaining": round(remaining, 2),
        },
    }


def entry_reason(entry: Dict[str, Any], world: Any = None) -> Dict[str, str]:
    """每条历史的「为了什么 / 为什么」：步骤 → 所属目标和当时决策的理由；决策 → 理由（或触发原因）。"""
    kind = entry.get("kind")
    out: Dict[str, str] = {}
    if kind == "decision":
        goal = entry.get("goal")
        if isinstance(goal, dict) and goal:
            out["for"] = goal_phrase(goal, world)
        why = entry.get("reason") or "; ".join(str(t) for t in entry.get("triggers") or [] if t)
        if why:
            out["why"] = str(why)
        return out
    if kind == "replan":
        goal = entry.get("goal")
        if isinstance(goal, dict) and goal:
            out["for"] = goal_phrase(goal, world)
        if entry.get("reason"):
            out["why"] = str(entry["reason"])
        return out
    goal = entry.get("for_goal")
    if isinstance(goal, dict) and goal:
        out["for"] = goal_phrase(goal, world)
    if entry.get("why"):
        out["why"] = str(entry["why"])
    elif entry.get("goal_source") in ("routine",):
        out["why"] = "it's on the daily routine"
    elif entry.get("reason"):
        out["why"] = str(entry["reason"])
    return out
