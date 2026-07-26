"""
Web 端 LLM 决策服务 — 封装 MultiStepDecider + Chroma 记忆。
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
            from chroma_memory import ChromaMemory

            persist = os.getenv("CHROMA_PATH", str(_AITOWN3 / "data" / "chroma"))
            _memory = ChromaMemory.get_instance(persist_path=persist)
        except Exception:
            _memory = False  # type: ignore[assignment]
    return _memory if _memory is not False else None


def get_llm_status(*, refresh: bool = False) -> Dict[str, Any]:
    """探测主模型（Qwen）配置与本地 GBNF 兜底是否就绪。"""
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
                _model_hint = "OPENAI_API_KEY 未配置"
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
                f"主模型就绪（{_model_hint}）"
                + ("；本地 GBNF 兜底可用" if gbnf_ready else "；本地 GBNF 未就绪")
            )
            if _llm_available
            else f"知识回复模式（{_model_hint}）"
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
    """将 decision_process 转为前端可渲染的步骤列表。"""
    # 单次决策：只有一条综合结果
    if process.get("planner") == "single_shot" or (
        process.get("synthesis_result") and not process.get("values")
    ):
        syn = process.get("synthesis_result") or {}
        items = []
        for f, label in (
            ("final_goal", "最终目标"),
            ("target_entity", "目标实体"),
            ("reasoning", "推理"),
            ("confidence", "信心"),
            ("contributing_factors", "因素权重"),
        ):
            val = syn.get(f)
            if val is None or val == "" or val == []:
                continue
            items.append({"key": f, "label": label, "value": val})
        return [{"index": 1, "id": "decide", "title": "单次决策", "items": items}]

    defs = [
        ("values", "价值观评估", ["relevant_values", "value_conflicts", "value_alignment", "reasoning", "recommended_actions"]),
        ("emotion", "情绪分析", ["current_emotion", "emotion_intensity", "emotion_cause", "emotion_impact", "recommended_actions"]),
        ("background", "背景考虑", ["relevant_background", "past_experiences", "learned_lessons", "background_influence", "recommended_actions"]),
        ("environment", "环境分析", ["opportunities", "threats", "available_resources", "constraints", "recommended_actions"]),
        ("synthesis", "综合决定", ["final_goal", "target_entity", "reasoning", "confidence", "contributing_factors"]),
    ]
    labels = {
        "relevant_values": "相关价值观",
        "value_conflicts": "价值冲突",
        "value_alignment": "价值对齐度",
        "reasoning": "推理",
        "recommended_actions": "推荐行动",
        "current_emotion": "当前情绪",
        "emotion_intensity": "情绪强度",
        "emotion_cause": "情绪原因",
        "emotion_impact": "对决策影响",
        "relevant_background": "相关背景",
        "past_experiences": "过往经历",
        "learned_lessons": "经验教训",
        "background_influence": "背景影响",
        "opportunities": "机会",
        "threats": "威胁",
        "available_resources": "可用资源",
        "constraints": "约束",
        "final_goal": "最终目标",
        "target_entity": "目标实体",
        "confidence": "信心",
        "contributing_factors": "因素权重",
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
    """列出或语义检索某角色的 Chroma 记忆。"""
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
            "message": "Chroma 记忆未启用或加载失败",
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
    """清空某角色的全部 Chroma 记忆。"""
    memory = get_web_memory()
    if memory is None:
        return {
            "ok": False,
            "available": False,
            "character": character,
            "message": "Chroma 记忆未启用或加载失败",
        }
    before = memory.count(character)
    memory.clear(character)
    return {
        "ok": True,
        "available": True,
        "character": character,
        "cleared": before,
        "message": f"已清空 {character} 的 {before} 条记忆",
    }


def clear_all_memories() -> Dict[str, Any]:
    """清空所有角色的 Chroma 记忆。"""
    memory = get_web_memory()
    if memory is None:
        return {
            "ok": False,
            "available": False,
            "message": "Chroma 记忆未启用或加载失败",
        }
    memory.clear_all()
    return {
        "ok": True,
        "available": True,
        "message": "已清空全部 Chroma 记忆",
    }
