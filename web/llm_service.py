"""
Web LLM decision service — wraps MultiStepDecider + NPC memory (Chroma or Neo4j, see MEMORY_BACKEND).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

_AITOWN3 = Path(__file__).resolve().parent.parent

_memory = None
_llm_checked = False
_llm_available = False
_model_hint = ""


def get_web_memory():
    global _memory
    if _memory is None:
        try:
            import sys

            mem_path = str(_AITOWN3 / "memory")
            if mem_path not in sys.path:
                sys.path.insert(0, mem_path)
            from memory_backend import get_memory

            persist = os.getenv("CHROMA_PATH", str(_AITOWN3 / "data" / "chroma"))
            _memory = get_memory(persist_path=persist)
        except Exception:
            _memory = False  # type: ignore[assignment]
    return _memory if _memory is not False else None


def get_llm_status(*, refresh: bool = False) -> Dict[str, Any]:
    """Check whether the primary model (Qwen) and local GBNF fallback are ready."""
    global _llm_checked, _llm_available, _model_hint
    if refresh or not _llm_checked:
        _llm_checked = True
        _llm_available = False
        _model_hint = ""
        try:
            import sys

            util = str(_AITOWN3 / "utility")
            if util not in sys.path:
                sys.path.insert(0, util)
            from model_instances import (
                is_primary_configured,
                primary_model_name,
                is_local_gbnf_ready,
                local_model_path,
            )

            if is_primary_configured():
                _llm_available = True
                _model_hint = primary_model_name()
            else:
                _model_hint = "OPENAI_API_KEY not configured"
        except Exception as e:
            _model_hint = str(e)

    gbnf_ready = False
    local_path = ""
    try:
        import sys

        util = str(_AITOWN3 / "utility")
        if util not in sys.path:
            sys.path.insert(0, util)
        from model_instances import is_local_gbnf_ready, local_model_path

        gbnf_ready = is_local_gbnf_ready()
        local_path = local_model_path()
    except Exception:
        pass

    return {
        "available": _llm_available,
        "mode": "llm" if _llm_available else "mock",
        "model": _model_hint if _llm_available else None,
        "provider": "qwen" if _llm_available else None,
        "gbnf_fallback_ready": gbnf_ready,
        "gbnf_model": local_path if gbnf_ready else None,
        "message": (
            (
                f"Primary model ready ({_model_hint})"
                + ("; local GBNF fallback available" if gbnf_ready else "; local GBNF not ready")
            )
            if _llm_available
            else f"Knowledge-reply mode ({_model_hint})"
        ),
        "memory": get_web_memory() is not None,
        "loaded": _llm_available,
    }


def _sanitize_goal_target(
    character: str,
    goal: str,
    target_entity: Optional[str],
) -> Optional[str]:
    if not target_entity:
        return None
    t = str(target_entity).strip()
    if goal in ("Gain information", "Defeat enemy") and t == character:
        return None
    return t or None


def _select_best_candidate(
    candidates: List[Dict[str, Any]],
    character: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    cleaned: List[Dict[str, Any]] = []
    for c in candidates:
        item = dict(c)
        if character:
            item["target_entity"] = _sanitize_goal_target(
                character,
                item.get("goal", ""),
                item.get("target_entity"),
            )
        cleaned.append(item)
    return max(
        cleaned,
        key=lambda c: float(c.get("priority", 0.5)) * 0.6 + float(c.get("confidence", 0.5)) * 0.4,
    )


def run_llm_decision(
    world,
    character: str,
    *,
    force_mock: bool = False,
    parent_goal: Optional[str] = None,
    last_goal: Optional[Dict[str, Any]] = None,
    last_action: Optional[Dict[str, Any]] = None,
    num_candidates: int = 1,
) -> Dict[str, Any]:
    from agent_profile import AgentProfile
    from multi_step_decider import MultiStepDecider

    memory = get_web_memory()
    use_mock = force_mock or not get_llm_status().get("available")
    decider = MultiStepDecider(world, memory=memory, force_mock=use_mock)

    agent = AgentProfile.from_world(world, character)
    if memory:
        agent.set_memory(memory)

    result = decider.decide_goal_multi_step(
        agent=agent,
        parent_goal=agent.parent_goal,
        current_goal=(last_goal or {}).get("goal"),
        last_action_result=(last_action or {}).get("message"),
        last_action_success=(last_action or {}).get("success", True)
        if last_action
        else True,
        generate_multiple=False,
        num_candidates=1,
        verbose=False,
    )

    mode = "mock" if decider.llm is None else "llm"
    candidates = list(result.get("candidate_goals") or [])
    if not candidates and result.get("goal_data"):
        g = result["goal_data"]
        candidates = [{
            "goal": g.get("goal"),
            "target_entity": g.get("target_entity"),
            "reason": g.get("reason", ""),
            "priority": 0.7,
            "confidence": 0.6,
        }]

    selected = _select_best_candidate(candidates, character)
    return {
        "character": character,
        "mode": mode,
        "decision_process": result.get("decision_process"),
        "candidate_goals": candidates,
        "selected_goal": selected,
        "steps": format_decision_steps(result.get("decision_process") or {}),
    }


def format_decision_steps(process: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert decision_process into a frontend-renderable step list."""
    # Single-shot decision: one combined result only
    if process.get("planner") == "single_shot" or (
        process.get("synthesis_result") and not process.get("values")
    ):
        syn = process.get("synthesis_result") or {}
        items = []
        for f, label in (
            ("final_goal", "Final goal"),
            ("target_entity", "Target entity"),
            ("reasoning", "Reasoning"),
            ("confidence", "Confidence"),
            ("contributing_factors", "Contributing factors"),
        ):
            val = syn.get(f)
            if val is None or val == "" or val == []:
                continue
            items.append({"key": f, "label": label, "value": val})
        return [{"index": 1, "id": "decide", "title": "Single decision", "items": items}]

    defs = [
        ("values", "Values assessment", ["relevant_values", "value_conflicts", "value_alignment", "reasoning", "recommended_actions"]),
        ("emotion", "Emotion analysis", ["current_emotion", "emotion_intensity", "emotion_cause", "emotion_impact", "recommended_actions"]),
        ("background", "Background consideration", ["relevant_background", "past_experiences", "learned_lessons", "background_influence", "recommended_actions"]),
        ("environment", "Environment analysis", ["opportunities", "threats", "available_resources", "constraints", "recommended_actions"]),
        ("synthesis", "Final synthesis", ["final_goal", "target_entity", "reasoning", "confidence", "contributing_factors"]),
    ]
    labels = {
        "relevant_values": "Relevant values",
        "value_conflicts": "Value conflicts",
        "value_alignment": "Value alignment",
        "reasoning": "Reasoning",
        "recommended_actions": "Recommended actions",
        "current_emotion": "Current emotion",
        "emotion_intensity": "Emotion intensity",
        "emotion_cause": "Emotion cause",
        "emotion_impact": "Impact on decision",
        "relevant_background": "Relevant background",
        "past_experiences": "Past experiences",
        "learned_lessons": "Lessons learned",
        "background_influence": "Background influence",
        "opportunities": "Opportunities",
        "threats": "Threats",
        "available_resources": "Available resources",
        "constraints": "Constraints",
        "final_goal": "Final goal",
        "target_entity": "Target entity",
        "confidence": "Confidence",
        "contributing_factors": "Contributing factors",
    }
    steps = []
    for i, (key, title, fields) in enumerate(defs, start=1):
        block = process.get(key) or {}
        items = []
        for f in fields:
            val = block.get(f)
            if val is None or val == "" or val == []:
                continue
            items.append({"key": f, "label": labels.get(f, f), "value": val})
        steps.append({"index": i, "id": key, "title": title, "items": items})
    return steps


def _format_memory_item(item: Dict[str, Any]) -> Dict[str, Any]:
    meta = item.get("metadata") or {}
    success = meta.get("success")
    if isinstance(success, str):
        success = success.lower() in ("true", "1", "yes")
    return {
        "id": item.get("id"),
        "text": item.get("text", ""),
        "type": meta.get("type", ""),
        "timestamp": meta.get("timestamp", ""),
        "location": meta.get("location", ""),
        "success": success,
        "result": meta.get("result", ""),
        "distance": item.get("distance"),
    }


def get_character_memories(
    character: str,
    *,
    limit: int = 50,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """List or semantically search a character's memories."""
    memory = get_web_memory()
    if memory is None:
        return {
            "available": False,
            "character": character,
            "mode": "unavailable",
            "query": query or "",
            "count": 0,
            "total": 0,
            "memories": [],
            "message": "NPC memory not enabled or failed to load",
        }

    total = memory.count(character)
    q = (query or "").strip()
    if q:
        raw = memory.search(character, q, top_k=max(1, min(limit, 100)))
        mode = "search"
    else:
        raw = memory.list(character)
        raw.sort(
            key=lambda x: (x.get("metadata") or {}).get("timestamp", ""),
            reverse=True,
        )
        raw = raw[: max(1, min(limit, 200))]
        mode = "list"

    return {
        "available": True,
        "character": character,
        "mode": mode,
        "query": q,
        "count": len(raw),
        "total": total,
        "memories": [_format_memory_item(x) for x in raw],
    }


def clear_character_memories(character: str) -> Dict[str, Any]:
    """Clear all memories for a character."""
    memory = get_web_memory()
    if memory is None:
        return {
            "ok": False,
            "available": False,
            "character": character,
            "message": "NPC memory not enabled or failed to load",
        }
    before = memory.count(character)
    memory.clear(character)
    return {
        "ok": True,
        "available": True,
        "character": character,
        "cleared": before,
        "message": f"Cleared {before} memories for {character}",
    }


def clear_all_memories() -> Dict[str, Any]:
    """Clear memories for all characters."""
    memory = get_web_memory()
    if memory is None:
        return {
            "ok": False,
            "available": False,
            "message": "NPC memory not enabled or failed to load",
        }
    memory.clear_all()
    return {
        "ok": True,
        "available": True,
        "message": "Cleared all NPC memories",
    }
