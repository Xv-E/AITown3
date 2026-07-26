"""
角色设定（Neo4j Person 属性）供 Web API 与前端展示。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

PROFILE_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("goal", "当前目标"),
    ("personality", "性格"),
    ("background", "背景"),
    ("values", "价值观"),
    ("mood", "情绪"),
)


def _format_values(values: Any) -> str:
    if values is None:
        return ""
    if isinstance(values, list):
        return ", ".join(str(v).strip() for v in values if str(v).strip())
    return str(values).strip()


def _format_goal_dict(goal: Any) -> str:
    if not isinstance(goal, dict):
        return str(goal or "").strip()
    term = (goal.get("goal") or goal.get("final_goal") or "").strip()
    target = goal.get("target_entity")
    if term and target:
        return f"{term} → {target}"
    return term or (goal.get("reason") or "").strip()


def get_character_profile(world, name: str, *, live_goal: Optional[str] = None) -> Dict[str, Any]:
    """从 Neo4j 加载角色设定；live_goal 可覆盖展示用的当前目标。"""
    rows = world.run_query(
        "MATCH (p:Person {name: $name}) RETURN p.name AS name LIMIT 1",
        name=name,
    )
    if not rows:
        return {"found": False, "name": name}

    from agent_profile import AgentProfile

    profile = AgentProfile.from_world(world, name)
    memory_count: Optional[int] = None
    try:
        from llm_service import get_web_memory

        mem = get_web_memory()
        if mem is not None:
            memory_count = mem.count(name)
    except Exception:
        pass

    goal_text = (live_goal or profile.goal or "").strip()
    fields: List[Dict[str, str]] = []
    raw: Dict[str, Any] = {
        "goal": goal_text,
        "personality": profile.personality,
        "background": profile.background,
        "values": profile.values,
        "mood": profile.mood,
    }
    for key, label in PROFILE_FIELDS:
        val = raw.get(key)
        if key == "values":
            text = _format_values(val)
        else:
            text = str(val).strip() if val else ""
        if text:
            fields.append({"key": key, "label": label, "value": text})
        elif key == "goal":
            fields.append({"key": key, "label": label, "value": "（暂无）"})

    return {
        "found": True,
        "name": name,
        "fields": fields,
        "goal": goal_text,
        "personality": profile.personality,
        "background": profile.background,
        "values": _format_values(profile.values),
        "mood": profile.mood,
        "memory_count": memory_count,
    }


def get_character_dossier(world, name: str, *, memory_limit: int = 40) -> Dict[str, Any]:
    """角色一览：状态 + 设定 + 计划 + 记忆日志。"""
    from character_actions import _person_state
    from world_simulator import get_world_simulator

    sim = get_world_simulator()
    plan = sim.get_plan_dict(world, name)

    live_goal = ""
    try:
        loop = getattr(sim, "_game_loop", None)
        if loop is not None:
            st = loop.get_agent_state(name)
            if st is not None and getattr(st.agent, "goal", None):
                live_goal = str(st.agent.goal).strip()
    except Exception:
        pass
    if not live_goal:
        live_goal = _format_goal_dict(plan.get("goal"))
    if not live_goal:
        # Neo4j 上的初值 / 已写回值
        try:
            rows = world.run_query(
                """
                MATCH (p:Person {name: $n})
                RETURN coalesce(p.goal, '') AS g
                LIMIT 1
                """,
                n=name,
            ) or []
            live_goal = str((rows[0].get("g") if rows else "") or "").strip()
        except Exception:
            pass

    profile = get_character_profile(world, name, live_goal=live_goal or None)
    if not profile.get("found"):
        return {"found": False, "name": name}

    state = _person_state(world, name) or {}

    memories: Dict[str, Any] = {
        "available": False,
        "memories": [],
        "total": 0,
        "message": "",
    }
    try:
        from llm_service import get_character_memories

        memories = get_character_memories(name, limit=memory_limit)
    except Exception as e:
        memories["message"] = str(e)

    activity = None
    for row in sim.npc_activity_summary(world):
        if row.get("name") == name:
            activity = row
            break

    return {
        "found": True,
        "name": name,
        "state": {
            "location": state.get("location"),
            "hp": state.get("hp"),
            "power_level": state.get("power_level"),
            "alive": state.get("alive", True),
            "held_items": state.get("held_items") or [],
            "is_player": state.get("is_player", False),
            "role": state.get("role") or "npc",
            "mood": profile.get("mood"),
            "personality": profile.get("personality"),
            "goal": profile.get("goal") or live_goal,
        },
        "profile": profile,
        "plan": plan,
        "activity": activity,
        "memories": memories,
        "sim_time": sim.sim_time,
        "tick": sim.tick_count,
    }
