"""
多步骤决策器

将决策过程分解为多个LLM步骤：
1. 价值观评估
2. 情绪分析
3. 背景考虑
4. 环境分析
5. 综合决定

每个步骤产生结构化的中间结果，最终综合得出决策。
"""

import json
import re
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict
from enum import Enum
from agent_profile import AgentProfile
from prompt_builder import PromptBuilder
from goal_terms import get_all_goal_terms, goal_term_exists, get_goal_definition
from model_instances import is_primary_configured
from gbnf import (
    collect_entities_by_type,
    generate_json_grammar,
    generate_json_array_grammar,
    generate_synthesis_grammar,
    generate_synthesis_array_grammar,
)
from llm_router import invoke_primary, invoke_local_gbnf, build_repair_prompt
from trace_util import record_trace


class DecisionStep(Enum):
    """决策步骤枚举"""
    VALUES = "values"           # 价值观评估
    EMOTION = "emotion"          # 情绪分析
    BACKGROUND = "background"   # 背景考虑
    ENVIRONMENT = "environment"  # 环境分析
    SYNTHESIS = "synthesis"      # 综合决定


@dataclass
class ValuesAssessment:
    """价值观评估结果"""
    relevant_values: List[str]           # 相关的价值观
    value_conflicts: List[str]           # 价值观冲突
    value_alignment: float              # 价值观对齐度 (0.0-1.0)
    reasoning: str                      # 推理过程
    recommended_actions: List[str]      # 基于价值观的推荐行动


@dataclass
class EmotionAnalysis:
    """情绪分析结果"""
    current_emotion: str                 # 当前情绪 (happy, sad, angry, fearful, neutral, etc.)
    emotion_intensity: float            # 情绪强度 (0.0-1.0)
    emotion_cause: str                  # 情绪原因
    emotion_impact: str                 # 情绪对决策的影响
    recommended_actions: List[str]      # 基于情绪的推荐行动


@dataclass
class BackgroundConsideration:
    """背景考虑结果"""
    relevant_background: List[str]       # 相关的背景信息
    past_experiences: List[str]          # 相关过往经历
    learned_lessons: List[str]           # 学到的经验教训
    background_influence: str            # 背景对决策的影响
    recommended_actions: List[str]       # 基于背景的推荐行动


@dataclass
class EnvironmentAnalysis:
    """环境分析结果"""
    opportunities: List[str]             # 当前环境中的机会
    threats: List[str]                   # 当前环境中的威胁
    available_resources: List[str]       # 可用资源
    constraints: List[str]              # 约束条件
    recommended_actions: List[str]       # 基于环境的推荐行动


@dataclass
class SynthesisResult:
    """综合决策结果"""
    final_goal: str                      # 最终目标
    target_entity: Optional[str]         # 目标实体
    reasoning: str                       # 综合推理
    confidence: float                    # 决策信心 (0.0-1.0)
    contributing_factors: Dict[str, float]  # 各因素贡献度


@dataclass
class DecisionProcess:
    """完整的决策过程"""
    values_assessment: ValuesAssessment
    emotion_analysis: EmotionAnalysis
    background_consideration: BackgroundConsideration
    environment_analysis: EnvironmentAnalysis
    synthesis_result: SynthesisResult
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "values": asdict(self.values_assessment),
            "emotion": asdict(self.emotion_analysis),
            "background": asdict(self.background_consideration),
            "environment": asdict(self.environment_analysis),
            "synthesis": asdict(self.synthesis_result)
        }


class MultiStepDecider:
    """
    多步骤决策器
    
    将决策过程分解为多个步骤，每个步骤使用LLM进行分析，
    最终综合所有步骤的结果做出决策。
    """
    
    def __init__(self, world=None, llm=None, memory=None, force_mock: bool = False):
        self.world = world
        self.prompt_builder = PromptBuilder(world, memory=memory)
        # llm=True 表示主模型（Qwen）可用；不再把本地 Llama 当主句柄
        if force_mock:
            self.llm = None
        elif llm is None:
            try:
                self.llm = True if is_primary_configured() else None
                if self.llm is None:
                    print("⚠️ 警告: OPENAI_API_KEY 未配置，将使用模拟模式")
            except Exception as e:
                print(f"⚠️ 警告: 无法初始化主 LLM: {e}")
                print("   将使用模拟模式")
                self.llm = None
        else:
            self.llm = llm
    
    def decide_goal_multi_step(
        self,
        agent: AgentProfile,
        parent_goal: Optional[str] = None,
        current_goal: Optional[str] = None,
        last_action_result: Optional[str] = None,
        last_action_success: bool = True,
        task_instructions: Optional[str] = None,
        verbose: bool = False,
        generate_multiple: bool = False,
        num_candidates: int = 1,
        multi_layer: bool = False,
    ) -> Dict[str, Any]:
        """决策目标。

        默认：单次 LLM 调用，只返回 1 个目标（candidate_goals 长度为 1）。
        multi_layer=True 时恢复旧的 5 步流水线（调试用）。
        """
        _ = last_action_success, task_instructions  # 保留签名兼容
        if self.llm is None:
            if generate_multiple and num_candidates > 1:
                return self._default_multiple_decision(agent, num_candidates)
            return self._default_decision()

        try:
            self._trace_character = getattr(agent, "name", "") or ""
            if not multi_layer:
                if verbose:
                    print("🎯 单次决策...")
                return self._decide_single_shot(
                    agent,
                    parent_goal=parent_goal,
                    current_goal=current_goal,
                    last_action_result=last_action_result,
                    verbose=verbose,
                )
            return self._decide_multi_layer(
                agent,
                parent_goal=parent_goal,
                current_goal=current_goal,
                last_action_result=last_action_result,
                last_action_success=last_action_success,
                verbose=verbose,
                generate_multiple=generate_multiple,
                num_candidates=num_candidates,
            )
        except Exception as e:
            print(f"⚠️ 决策失败: {e}")
            import traceback
            traceback.print_exc()
            return self._default_decision()

    def _decide_single_shot(
        self,
        agent: AgentProfile,
        *,
        parent_goal: Optional[str] = None,
        current_goal: Optional[str] = None,
        last_action_result: Optional[str] = None,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """一次 LLM 调用选出唯一目标。"""
        self._trace_step = "decide"
        personality = ", ".join(self._safe_get_list(agent, "personality", [])) or "none"
        values = ", ".join(self._safe_get_list(agent, "values", [])) or "none"
        situation = ""
        try:
            situation = self.prompt_builder.build_situation_block(agent)
        except Exception as e:
            situation = f"(situation unavailable: {e})"
        prompt = f"""You choose the single best next goal for a character in a text adventure.

=== Character (fixed persona — not a checklist goal) ===
Name: {agent.name}
Status: {agent.get_status()}
Personality: {personality}
Values: {values}
Background: {getattr(agent, "background", "") or "none"}
Current goal (may change next turn): {current_goal or getattr(agent, "goal", None) or "None"}
Last action result: {last_action_result or "None"}

{situation}

=== Available Goal Terms ===
{self._format_goal_terms()}

=== Valid Entity Names (exact spelling required) ===
{self._format_valid_entities()}

=== Rules ===
1. Return exactly ONE goal — the best immediate next step from YOUR current situation.
2. final_goal MUST be one of the Available Goal Terms.
3. target_entity MUST be an EXACT name from Valid Entity Names, or null.
4. Decide from Nearby Graph + Known Facts + Recent Memories; persona only colors how
   you choose, it is not a fixed quest script.
5. Do not invent entities, facts, or goal terms.
6. Do NOT repeatedly Gain information from the same NPC if Known Facts / memories
   already contain their advice — act on it (Collect item / Go to location / Use item on).
7. You may keep, refine, or replace the current goal when the situation changes.
8. If Temporary Actions are listed (e.g. OakTree.chop), prefer final_goal="Interact with"
   with that target_entity and interact_action id (chop), not a vague interact.

=== Response Format ===
Respond with a single JSON object:
{{
    "final_goal": "one of the goal terms above",
    "target_entity": "exact entity name or null",
    "interact_action": "temporary action id when Interact with (e.g. chop), else null",
    "reasoning": "brief why this is the best next step",
    "confidence": 0.0-1.0,
    "contributing_factors": {{
        "values": 0.0-1.0,
        "emotion": 0.0-1.0,
        "background": 0.0-1.0,
        "environment": 0.0-1.0,
        "priority": 0.0-1.0
    }}
}}
"""
        response = self._invoke_llm_synthesis_json(prompt, include_priority=True, agent=agent)
        synthesis = self._parse_synthesis_result(response)
        final_goal = synthesis.final_goal
        if not goal_term_exists(final_goal):
            if verbose:
                print(f"⚠️ 目标术语 '{final_goal}' 无效，回退 Gain information")
            final_goal = "Gain information"
            synthesis.final_goal = final_goal

        priority = 0.8
        if isinstance(synthesis.contributing_factors, dict):
            priority = float(synthesis.contributing_factors.get("priority", 0.8) or 0.8)

        goal_data = {
            "goal": final_goal,
            "target_entity": synthesis.target_entity,
            "reason": synthesis.reasoning,
            "confidence": float(synthesis.confidence or 0.7),
            "priority": priority,
            "metadata": {"contributing_factors": synthesis.contributing_factors},
        }
        if isinstance(response, dict):
            ia = str(response.get("interact_action") or "").strip()
            if ia and ia.lower() not in ("null", "none", "-"):
                goal_data["interact_action"] = ia
                goal_data["metadata"]["interact_action"] = ia
        decision_process = {
            "planner": "single_shot",
            "note": "单次 LLM 决策，仅一个目标",
            "settings": agent.get_settings_summary(),
            "synthesis_result": {
                "final_goal": final_goal,
                "target_entity": synthesis.target_entity,
                "interact_action": goal_data.get("interact_action"),
                "reasoning": synthesis.reasoning,
                "confidence": synthesis.confidence,
                "contributing_factors": synthesis.contributing_factors,
            },
        }
        return {
            "goal_data": goal_data,
            "candidate_goals": [goal_data],
            "decision_process": decision_process,
        }

    def _decide_multi_layer(
        self,
        agent: AgentProfile,
        *,
        parent_goal: Optional[str] = None,
        current_goal: Optional[str] = None,
        last_action_result: Optional[str] = None,
        last_action_success: bool = True,
        verbose: bool = False,
        generate_multiple: bool = False,
        num_candidates: int = 1,
    ) -> Dict[str, Any]:
        """旧 5 步流水线（仅 multi_layer=True 时使用）。"""
        if verbose:
            print("🔍 步骤1: 价值观评估...")
        self._trace_step = "values"
        values_assessment = self._assess_values(agent, parent_goal, current_goal, last_action_result)

        self._trace_step = "emotion"
        emotion_analysis = self._analyze_emotion(agent, last_action_result, last_action_success)

        if verbose:
            print("📖 步骤3: 背景考虑...")
        self._trace_step = "background"
        background_consideration = self._consider_background(agent, parent_goal, current_goal)

        if verbose:
            print("🌍 步骤4: 环境分析...")
        self._trace_step = "environment"
        environment_analysis = self._analyze_environment(agent)

        if verbose:
            print("🎯 步骤5: 综合决定...")
        self._trace_step = "synthesis"

        if generate_multiple and num_candidates > 1:
            candidate_results = self._synthesize_multiple_decisions(
                agent=agent,
                values_assessment=values_assessment,
                emotion_analysis=emotion_analysis,
                background_consideration=background_consideration,
                environment_analysis=environment_analysis,
                parent_goal=parent_goal,
                current_goal=current_goal,
                last_action_result=last_action_result,
                num_candidates=num_candidates,
                verbose=verbose,
            )
            decision_process = DecisionProcess(
                values_assessment=values_assessment,
                emotion_analysis=emotion_analysis,
                background_consideration=background_consideration,
                environment_analysis=environment_analysis,
                synthesis_result=candidate_results[0] if candidate_results else self._default_synthesis(),
            )
            candidate_goals = []
            for i, result in enumerate(candidate_results):
                final_goal = result.final_goal
                if not goal_term_exists(final_goal):
                    if verbose:
                        print(f"⚠️ 警告: 候选目标 {i+1} 的术语 '{final_goal}' 无效")
                    final_goal = "Gain information"
                    result.final_goal = final_goal
                candidate_goals.append({
                    "goal": final_goal,
                    "target_entity": result.target_entity,
                    "reason": result.reasoning,
                    "confidence": result.confidence,
                    "priority": result.contributing_factors.get("priority", 0.5)
                    if isinstance(result.contributing_factors, dict)
                    else 0.5,
                    "metadata": {"contributing_factors": result.contributing_factors},
                })
            return {
                "candidate_goals": candidate_goals,
                "decision_process": decision_process.to_dict(),
            }

        synthesis_result = self._synthesize_decision(
            agent=agent,
            values_assessment=values_assessment,
            emotion_analysis=emotion_analysis,
            background_consideration=background_consideration,
            environment_analysis=environment_analysis,
            parent_goal=parent_goal,
            current_goal=current_goal,
            last_action_result=last_action_result,
        )
        decision_process = DecisionProcess(
            values_assessment=values_assessment,
            emotion_analysis=emotion_analysis,
            background_consideration=background_consideration,
            environment_analysis=environment_analysis,
            synthesis_result=synthesis_result,
        )
        final_goal = synthesis_result.final_goal
        if not goal_term_exists(final_goal):
            if verbose:
                print(f"⚠️ 警告: 目标术语 '{final_goal}' 无效，使用默认目标")
            final_goal = "Gain information"
            synthesis_result.final_goal = final_goal
        goal_data = {
            "goal": final_goal,
            "target_entity": synthesis_result.target_entity,
            "reason": synthesis_result.reasoning,
            "confidence": float(synthesis_result.confidence or 0.7),
            "priority": 0.8,
        }
        return {
            "goal_data": goal_data,
            "candidate_goals": [goal_data],
            "decision_process": decision_process.to_dict(),
        }
    
    def _safe_get_list(self, obj, attr_name: str, default: List[str]) -> List[str]:
        """安全获取列表属性，确保返回的是列表"""
        value = getattr(obj, attr_name, default)
        if value is None:
            return default
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [value] if value else default
        # 如果是其他可迭代对象，转换为列表
        try:
            return list(value)
        except (TypeError, ValueError):
            return default
    
    def _assess_values(
        self,
        agent: AgentProfile,
        parent_goal: Optional[str],
        current_goal: Optional[str],
        last_action_result: Optional[str]
    ) -> ValuesAssessment:
        """步骤1: 价值观评估"""
        # 获取NPC的价值观（从属性或默认值），确保是列表
        agent_values = self._safe_get_list(agent, 'values', ['honor', 'friendship', 'courage'])
        agent_traits = self._safe_get_list(agent, 'personality', ['brave', 'curious'])
        
        prompt = f"""You are analyzing a character's values and how they align with potential actions.

=== Character Profile ===
Name: {agent.name}
Values: {', '.join(agent_values) if agent_values else 'None'}
Personality Traits: {', '.join(agent_traits) if agent_traits else 'None'}

=== Current Situation ===
Background: {getattr(agent, "background", "") or "None"}
Current Goal: {current_goal or "None"}
Last Action Result: {last_action_result or "None"}

=== Task ===
Analyze which values are relevant to the current situation, identify any value conflicts,
and assess how well potential actions align with the character's values.

=== Response Format ===
Respond with a JSON object:
{{
    "relevant_values": ["value1", "value2", ...],
    "value_conflicts": ["conflict1", "conflict2", ...],
    "value_alignment": 0.0-1.0,
    "reasoning": "explanation of value assessment",
    "recommended_actions": ["action1", "action2", ...]
}}
"""
        
        response = self._invoke_llm_with_json(prompt)
        return self._parse_values_assessment(response)
    
    def _analyze_emotion(
        self,
        agent: AgentProfile,
        last_action_result: Optional[str],
        last_action_success: bool
    ) -> EmotionAnalysis:
        """步骤2: 情绪分析"""
        # 获取NPC的当前情绪（从属性或默认值）
        current_mood = getattr(agent, 'mood', 'neutral')
        memories = agent.get_recent_memories(5)
        
        prompt = f"""You are analyzing a character's emotional state and how it affects decision-making.

=== Character Profile ===
Name: {agent.name}
Current Mood: {current_mood}
HP: {getattr(agent, 'hp', 100)}
Power Level: {getattr(agent, 'power_level', 10)}

=== Recent Memories ===
{self._format_memories(memories)}

=== Last Action ===
Result: {last_action_result or "None"}
Success: {last_action_success}

=== Task ===
Analyze the character's current emotional state, identify the cause, assess the intensity,
and determine how emotions might influence decision-making.

=== Response Format ===
Respond with a JSON object:
{{
    "current_emotion": "emotion_name",
    "emotion_intensity": 0.0-1.0,
    "emotion_cause": "explanation of what caused this emotion",
    "emotion_impact": "how this emotion affects decision-making",
    "recommended_actions": ["action1", "action2", ...]
}}

Valid emotions: happy, sad, angry, fearful, anxious, excited, calm, neutral, determined, frustrated
"""
        
        response = self._invoke_llm_with_json(prompt)
        return self._parse_emotion_analysis(response)
    
    def _consider_background(
        self,
        agent: AgentProfile,
        parent_goal: Optional[str],
        current_goal: Optional[str]
    ) -> BackgroundConsideration:
        """步骤3: 背景考虑"""
        background = getattr(agent, 'background', f"{agent.name} is a hero on a quest.")
        memories = agent.get_memories(limit=10)
        
        prompt = f"""You are analyzing how a character's background and past experiences influence their decisions.

=== Character Profile ===
Name: {agent.name}
Background: {background}
Background: {getattr(agent, "background", "") or "none"}

=== Past Experiences ===
{self._format_memories(memories)}

=== Current Context ===
Background: {getattr(agent, "background", "") or "None"}
Current Goal: {current_goal or "None"}

=== Task ===
Identify relevant background information, past experiences, and lessons learned that should
influence the character's current decision.

=== Response Format ===
Respond with a JSON object:
{{
    "relevant_background": ["background_point1", "background_point2", ...],
    "past_experiences": ["experience1", "experience2", ...],
    "learned_lessons": ["lesson1", "lesson2", ...],
    "background_influence": "how background influences the decision",
    "recommended_actions": ["action1", "action2", ...]
}}
"""
        
        response = self._invoke_llm_with_json(prompt)
        return self._parse_background_consideration(response)
    
    def _analyze_environment(
        self,
        agent: AgentProfile
    ) -> EnvironmentAnalysis:
        """步骤4: 环境分析"""
        location_name = agent.location
        status = agent.get_status()
        available_entities = self._get_available_entities_text()
        
        prompt = f"""You are analyzing the current environment to identify opportunities, threats, and constraints.

=== Character Status ===
{status}

=== Current Location ===
{location_name or "Unknown"}

=== Available Entities ===
{available_entities}

=== Task ===
Analyze the environment to identify:
1. Opportunities (things that could help achieve goals)
2. Threats (dangers or obstacles)
3. Available resources
4. Constraints (limitations or restrictions)

=== Response Format ===
Respond with a JSON object:
{{
    "opportunities": ["opportunity1", "opportunity2", ...],
    "threats": ["threat1", "threat2", ...],
    "available_resources": ["resource1", "resource2", ...],
    "constraints": ["constraint1", "constraint2", ...],
    "recommended_actions": ["action1", "action2", ...]
}}
"""
        
        response = self._invoke_llm_with_json(prompt)
        return self._parse_environment_analysis(response)
    
    def _synthesize_decision(
        self,
        agent: AgentProfile,
        values_assessment: ValuesAssessment,
        emotion_analysis: EmotionAnalysis,
        background_consideration: BackgroundConsideration,
        environment_analysis: EnvironmentAnalysis,
        parent_goal: Optional[str],
        current_goal: Optional[str],
        last_action_result: Optional[str]
    ) -> SynthesisResult:
        """步骤5: 综合决定"""
        # 获取所有可用目标术语
        goal_terms = get_all_goal_terms()
        situation = ""
        try:
            situation = self.prompt_builder.build_situation_block(agent)
        except Exception as e:
            situation = f"(situation unavailable: {e})"

        prompt = f"""You are synthesizing all factors to make a final decision.

=== Character Profile ===
Name: {agent.name}
Status: {agent.get_status()}

{situation}

=== Values Assessment ===
Relevant Values: {', '.join(values_assessment.relevant_values) if values_assessment.relevant_values else 'None'}
Value Alignment: {values_assessment.value_alignment}
Reasoning: {values_assessment.reasoning}
Recommended Actions: {', '.join(values_assessment.recommended_actions) if values_assessment.recommended_actions else 'None'}

=== Emotion Analysis ===
Current Emotion: {emotion_analysis.current_emotion}
Intensity: {emotion_analysis.emotion_intensity}
Impact: {emotion_analysis.emotion_impact}
Recommended Actions: {', '.join(emotion_analysis.recommended_actions) if emotion_analysis.recommended_actions else 'None'}

=== Background Consideration ===
Relevant Background: {', '.join(background_consideration.relevant_background) if background_consideration.relevant_background else 'None'}
Lessons Learned: {', '.join(background_consideration.learned_lessons) if background_consideration.learned_lessons else 'None'}
Influence: {background_consideration.background_influence}
Recommended Actions: {', '.join(background_consideration.recommended_actions) if background_consideration.recommended_actions else 'None'}

=== Environment Analysis ===
Opportunities: {', '.join(environment_analysis.opportunities) if environment_analysis.opportunities else 'None'}
Threats: {', '.join(environment_analysis.threats) if environment_analysis.threats else 'None'}
Resources: {', '.join(environment_analysis.available_resources) if environment_analysis.available_resources else 'None'}
Recommended Actions: {', '.join(environment_analysis.recommended_actions) if environment_analysis.recommended_actions else 'None'}

=== Context ===
Background: {getattr(agent, "background", "") or "None"}
Current Goal: {current_goal or "None"}
Last Action: {last_action_result or "None"}

=== Available Goal Terms ===
{self._format_goal_terms()}

=== Valid Entity Names (exact spelling required) ===
{self._format_valid_entities()}

=== Task ===
Synthesize all the above factors to make a final decision. Consider:
1. Which values are most important in this situation?
2. How does the current emotion affect the decision?
3. What lessons from the past / memories apply here?
4. What is nearby in the graph, and what facts do you already know?
5. How do all these factors combine to suggest the best action?

=== Response Format ===
Respond with a JSON object:
{{
    "final_goal": "one of the goal terms above",
    "target_entity": "exact entity name from Valid Entity Names, or null",
    "reasoning": "comprehensive reasoning that synthesizes all factors",
    "confidence": 0.0-1.0,
    "contributing_factors": {{
        "values": 0.0-1.0,
        "emotion": 0.0-1.0,
        "background": 0.0-1.0,
        "environment": 0.0-1.0
    }}
}}
"""
        
        response = self._invoke_llm_synthesis_json(prompt, include_priority=False, agent=agent)
        return self._parse_synthesis_result(response)
    
    def _entities_for_grammar(self, agent: AgentProfile):
        if not self.world:
            from gbnf import EntitiesByType
            return EntitiesByType([], [], [])
        try:
            return collect_entities_by_type(self.world)
        except Exception:
            from gbnf import EntitiesByType
            return EntitiesByType([], [], [])

    def _primary_then_gbnf(
        self,
        prompt: str,
        *,
        grammar_str: str,
        parse_fn,
        schema_hint: str = "",
        expect_list: bool = False,
    ):
        """Qwen 主调用；解析失败则本地 GBNF 修复。每次调用写入 /inspect 日志。"""
        empty = [] if expect_list else {}
        primary_raw = ""
        character = getattr(self, "_trace_character", "") or ""
        step = getattr(self, "_trace_step", "goal_decision") or "goal_decision"
        base_meta = {
            "character": character,
            "step": step,
            "kind_label": "goal_decision",
        }
        t0 = time.perf_counter()

        def _ok_parsed(parsed) -> bool:
            if expect_list:
                return isinstance(parsed, list) and bool(parsed)
            return isinstance(parsed, dict) and bool(parsed)

        try:
            primary_raw = invoke_primary(prompt)
            parsed = parse_fn(primary_raw)
            if _ok_parsed(parsed):
                record_trace(
                    kind="goal_decision",
                    prompt=prompt,
                    raw=primary_raw or "",
                    ok=True,
                    meta={**base_meta, "provider": "qwen", "fallback_used": False},
                    parsed=parsed if isinstance(parsed, (dict, list)) else None,
                    gbnf={"used": False, "provider": "qwen", "fallback_used": False},
                    elapsed_ms=(time.perf_counter() - t0) * 1000,
                )
                return parsed
            record_trace(
                kind="goal_decision",
                prompt=prompt,
                raw=primary_raw or "",
                ok=False,
                error="unparseable primary output",
                meta={**base_meta, "provider": "qwen", "fallback_used": False},
                gbnf={
                    "used": False,
                    "error": "unparseable",
                    "provider": "qwen",
                    "fallback_used": False,
                },
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            print(f"⚠️ 主模型调用/解析失败: {e}")
            record_trace(
                kind="goal_decision",
                prompt=prompt,
                raw=primary_raw or "",
                ok=False,
                error=f"invoke: {e}",
                meta={**base_meta, "provider": "qwen", "fallback_used": False},
                gbnf={
                    "used": False,
                    "error": str(e),
                    "provider": "qwen",
                    "fallback_used": False,
                },
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        t1 = time.perf_counter()
        try:
            repair_prompt = build_repair_prompt(
                draft=primary_raw or "(empty)",
                schema_hint=schema_hint or "Output valid JSON matching the request.",
            )
            repair_raw = invoke_local_gbnf(repair_prompt, grammar_str)
            parsed2 = parse_fn(repair_raw)
            ginfo = {
                "used": True,
                "provider": "local_gbnf",
                "fallback_used": True,
                "grammar_preview": (grammar_str or "")[:400],
                "error": "",
            }
            if _ok_parsed(parsed2):
                record_trace(
                    kind="goal_decision",
                    prompt=repair_prompt,
                    raw=repair_raw or "",
                    ok=True,
                    meta={**base_meta, "provider": "local_gbnf", "fallback_used": True},
                    parsed=parsed2 if isinstance(parsed2, (dict, list)) else None,
                    gbnf=ginfo,
                    elapsed_ms=(time.perf_counter() - t1) * 1000,
                )
                return parsed2
            ginfo["error"] = "unparseable after repair"
            record_trace(
                kind="goal_decision",
                prompt=repair_prompt,
                raw=repair_raw or "",
                ok=False,
                error="本地 GBNF 修复后仍无法解析",
                meta={**base_meta, "provider": "local_gbnf", "fallback_used": True},
                gbnf=ginfo,
                elapsed_ms=(time.perf_counter() - t1) * 1000,
            )
            print("⚠️ 本地 GBNF 修复后仍无法解析")
            return empty
        except Exception as e:
            print(f"⚠️ 本地 GBNF 修复失败: {e}")
            record_trace(
                kind="goal_decision",
                prompt=prompt,
                raw="",
                ok=False,
                error=f"gbnf: {e}",
                meta={**base_meta, "provider": "local_gbnf", "fallback_used": True},
                gbnf={
                    "used": True,
                    "error": str(e),
                    "provider": "local_gbnf",
                    "fallback_used": True,
                    "grammar_preview": (grammar_str or "")[:400],
                },
                elapsed_ms=(time.perf_counter() - t1) * 1000,
            )
            return empty

    def _invoke_llm_synthesis_json(
        self,
        prompt: str,
        *,
        include_priority: bool = False,
        agent: Optional[AgentProfile] = None,
    ) -> Dict[str, Any]:
        """第5步：Qwen 主路径；失败时本地 GBNF 约束 final_goal / target_entity。"""
        goal_terms = get_all_goal_terms()
        entities = self._entities_for_grammar(agent) if agent else None
        grammar_str = generate_synthesis_grammar(
            goal_terms,
            entities,
            include_priority=include_priority,
        )
        result = self._primary_then_gbnf(
            prompt,
            grammar_str=grammar_str,
            parse_fn=self._extract_json,
            schema_hint=(
                '{"final_goal":"...","target_entity":"...|null","reasoning":"...",'
                '"confidence":0.0-1.0,"contributing_factors":{...}}'
            ),
        )
        if result:
            return result
        print("⚠️ synthesis 失败，回退通用 JSON 路径")
        return self._invoke_llm_with_json(prompt)

    def _invoke_llm_synthesis_json_array(
        self,
        prompt: str,
        *,
        agent: Optional[AgentProfile] = None,
    ) -> List[Dict[str, Any]]:
        """第5步多候选：Qwen 主路径；失败时本地 GBNF 约束 goal / entity。"""
        goal_terms = get_all_goal_terms()
        entities = self._entities_for_grammar(agent) if agent else None
        grammar_str = generate_synthesis_array_grammar(
            goal_terms,
            entities,
            include_priority=True,
        )
        result = self._primary_then_gbnf(
            prompt,
            grammar_str=grammar_str,
            parse_fn=self._extract_json_array,
            schema_hint='[{"final_goal":"...","target_entity":"...|null",...}, ...]',
            expect_list=True,
        )
        if result:
            return result
        print("⚠️ synthesis array 失败，回退通用 JSON 数组路径")
        return self._invoke_llm_with_json_array(prompt)

    def _invoke_llm_with_json(self, prompt: str) -> Dict[str, Any]:
        """调用主 LLM 并解析 JSON；失败则本地通用 JSON GBNF。"""
        return self._primary_then_gbnf(
            prompt,
            grammar_str=generate_json_grammar(),
            parse_fn=self._extract_json,
            schema_hint="A single JSON object.",
        )
    
    def _extract_json(self, text: str) -> Dict[str, Any]:
        """从文本中提取JSON"""
        # 尝试查找JSON对象
        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
        else:
            # 尝试查找代码块中的JSON
            code_block_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
            if code_block_match:
                json_str = code_block_match.group(1)
            else:
                json_str = text
        
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            print(f"⚠️ JSON解析失败，原始文本: {text[:200]}")
            return {}
    
    def _parse_values_assessment(self, data: Dict[str, Any]) -> ValuesAssessment:
        """解析价值观评估结果"""
        return ValuesAssessment(
            relevant_values=data.get("relevant_values", []),
            value_conflicts=data.get("value_conflicts", []),
            value_alignment=float(data.get("value_alignment", 0.5)),
            reasoning=data.get("reasoning", ""),
            recommended_actions=data.get("recommended_actions", [])
        )
    
    def _parse_emotion_analysis(self, data: Dict[str, Any]) -> EmotionAnalysis:
        """解析情绪分析结果"""
        return EmotionAnalysis(
            current_emotion=data.get("current_emotion", "neutral"),
            emotion_intensity=float(data.get("emotion_intensity", 0.5)),
            emotion_cause=data.get("emotion_cause", ""),
            emotion_impact=data.get("emotion_impact", ""),
            recommended_actions=data.get("recommended_actions", [])
        )
    
    def _parse_background_consideration(self, data: Dict[str, Any]) -> BackgroundConsideration:
        """解析背景考虑结果"""
        return BackgroundConsideration(
            relevant_background=data.get("relevant_background", []),
            past_experiences=data.get("past_experiences", []),
            learned_lessons=data.get("learned_lessons", []),
            background_influence=data.get("background_influence", ""),
            recommended_actions=data.get("recommended_actions", [])
        )
    
    def _parse_environment_analysis(self, data: Dict[str, Any]) -> EnvironmentAnalysis:
        """解析环境分析结果"""
        return EnvironmentAnalysis(
            opportunities=data.get("opportunities", []),
            threats=data.get("threats", []),
            available_resources=data.get("available_resources", []),
            constraints=data.get("constraints", []),
            recommended_actions=data.get("recommended_actions", [])
        )
    
    def _parse_synthesis_result(self, data: Dict[str, Any]) -> SynthesisResult:
        """解析综合决策结果"""
        return SynthesisResult(
            final_goal=data.get("final_goal", "Gain information"),
            target_entity=data.get("target_entity"),
            reasoning=data.get("reasoning", ""),
            confidence=float(data.get("confidence", 0.5)),
            contributing_factors=data.get("contributing_factors", {
                "values": 0.25,
                "emotion": 0.25,
                "background": 0.25,
                "environment": 0.25
            })
        )
    
    def _format_memories(self, memories: List[Dict]) -> str:
        """格式化记忆"""
        if not memories:
            return "No memories yet."
        
        lines = []
        for mem in memories:
            desc = mem.get('description', 'N/A')
            action = mem.get('action', 'N/A')
            lines.append(f"  - {desc} (Action: {action})")
        
        return "\n".join(lines)
    
    def _get_available_entities_text(self) -> str:
        """获取可用实体文本"""
        if not self.world:
            return "No world information available."
        
        try:
            lines = []
            locations = self.world.get_all_names("Location")
            if locations:
                lines.append(f"Locations: {', '.join(locations)}")
            
            npcs = self.world.get_all_names("Person")
            if npcs:
                lines.append(f"NPCs: {', '.join(npcs)}")
            
            items = self.world.get_all_names("Item")
            if items:
                lines.append(f"Items: {', '.join(items)}")
            
            return "\n".join(lines) if lines else "No entities available."
        except Exception:
            return "Error retrieving entities."
    
    def _format_goal_terms(self) -> str:
        """格式化目标术语列表"""
        from goal_terms import get_all_goal_terms, get_goal_definition
        
        lines = []
        for term in get_all_goal_terms():
            definition = get_goal_definition(term)
            if definition:
                lines.append(f"  - \"{term}\": {definition.description}")
            else:
                lines.append(f"  - \"{term}\"")
        
        return "\n".join(lines)
    
    def _format_valid_entities(self) -> str:
        """列出图中合法实体名，供 LLM 在 target_entity 中精确引用。"""
        if not self.world:
            return "No world data."
        try:
            locs = self.world.list_entities("Location") or []
            persons = self.world.list_entities("Person") or []
            items = self.world.list_entities("Item") or []
            doors = []
            pickable = []
            use_on_targets: List[str] = []
            try:
                door_rows = self.world.run_query(
                    """
                    MATCH (i:Item)
                    WHERE coalesce(i.item_kind, '') = 'door'
                    RETURN i.name AS n, coalesce(i.locked, false) AS locked
                    ORDER BY n
                    """
                )
                doors = [r["n"] for r in door_rows if r.get("n") and r.get("locked")]
                pickable = [
                    n for n in items
                    if n not in {r["n"] for r in door_rows if r.get("n")}
                ]
            except Exception:
                pickable = list(items)
            try:
                from world_interactions import use_on_catalog

                use_on_targets = sorted(use_on_catalog().keys())
            except Exception:
                use_on_targets = list(doors)
            lines = []
            if locs:
                lines.append(f"Locations (Go to location / Explore unknown location): {', '.join(locs)}")
            if persons:
                lines.append(f"Persons (Gain information / Defeat enemy): {', '.join(persons)}")
            if pickable:
                lines.append(f"Items (Collect item): {', '.join(pickable)}")
            if use_on_targets:
                lines.append(
                    "Use-on targets (Use item on — person or item from world effects_on/"
                    f"accepts_from): {', '.join(use_on_targets)}"
                )
            lines.append(
                "CRITICAL: target_entity must be an EXACT name from the lists above "
                "(e.g. BlueHerb, Lila, DoorKey). Use null only if no specific entity applies. "
                "Prefer Collect item for tools on the ground; Prefer Use item on for "
                "interactable targets when holding a matching tool from world config."
            )
            return "\n".join(lines) if lines else "No entities in world graph."
        except Exception:
            return "Error retrieving entities."
    
    def _synthesize_multiple_decisions(
        self,
        agent: AgentProfile,
        values_assessment: ValuesAssessment,
        emotion_analysis: EmotionAnalysis,
        background_consideration: BackgroundConsideration,
        environment_analysis: EnvironmentAnalysis,
        parent_goal: Optional[str],
        current_goal: Optional[str],
        last_action_result: Optional[str],
        num_candidates: int = 3,
        verbose: bool = False
    ) -> List[SynthesisResult]:
        """生成多个候选决策"""
        if self.llm is None:
            # Mock：首个候选按角色设定，其余多样化
            if self.world and agent:
                try:
                    from profile_planner import profile_aware_pick_goal
                    primary = profile_aware_pick_goal(self.world, agent)
                    results = []
                    for i in range(num_candidates):
                        if i == 0:
                            results.append(self._synthesis_from_goal_dict(primary))
                        else:
                            extras = self._generate_diverse_default_candidates(agent, 1)
                            results.extend(extras)
                    return results[:num_candidates]
                except Exception:
                    pass
            return self._generate_diverse_default_candidates(agent, num_candidates)
        
        # 获取所有可用目标术语
        goal_terms = get_all_goal_terms()
        situation = ""
        try:
            situation = self.prompt_builder.build_situation_block(agent)
        except Exception as e:
            situation = f"(situation unavailable: {e})"

        prompt = f"""You are synthesizing all factors to make multiple candidate decisions.

=== Character Profile ===
Name: {agent.name}
Status: {agent.get_status()}

{situation}

=== Values Assessment ===
Relevant Values: {', '.join(values_assessment.relevant_values) if values_assessment.relevant_values else 'None'}
Value Alignment: {values_assessment.value_alignment}
Reasoning: {values_assessment.reasoning}
Recommended Actions: {', '.join(values_assessment.recommended_actions) if values_assessment.recommended_actions else 'None'}

=== Emotion Analysis ===
Current Emotion: {emotion_analysis.current_emotion}
Intensity: {emotion_analysis.emotion_intensity}
Impact: {emotion_analysis.emotion_impact}
Recommended Actions: {', '.join(emotion_analysis.recommended_actions) if emotion_analysis.recommended_actions else 'None'}

=== Background Consideration ===
Relevant Background: {', '.join(background_consideration.relevant_background) if background_consideration.relevant_background else 'None'}
Lessons Learned: {', '.join(background_consideration.learned_lessons) if background_consideration.learned_lessons else 'None'}
Influence: {background_consideration.background_influence}
Recommended Actions: {', '.join(background_consideration.recommended_actions) if background_consideration.recommended_actions else 'None'}

=== Environment Analysis ===
Opportunities: {', '.join(environment_analysis.opportunities) if environment_analysis.opportunities else 'None'}
Threats: {', '.join(environment_analysis.threats) if environment_analysis.threats else 'None'}
Resources: {', '.join(environment_analysis.available_resources) if environment_analysis.available_resources else 'None'}
Recommended Actions: {', '.join(environment_analysis.recommended_actions) if environment_analysis.recommended_actions else 'None'}

=== Context ===
Background: {getattr(agent, "background", "") or "None"}
Current Goal: {current_goal or "None"}
Last Action: {last_action_result or "None"}

=== Available Goal Terms ===
{self._format_goal_terms()}

=== Valid Entity Names (exact spelling required) ===
{self._format_valid_entities()}

=== Task ===
Generate {num_candidates} different candidate goals based on the above factors. Each candidate should:
1. Be a different approach or priority
2. Consider different aspects (values, emotion, background, environment)
3. Have varying confidence levels
4. Use valid goal terms from the list above
5. Use only exact entity names from Valid Entity Names for target_entity

=== Response Format ===
Respond with a JSON array of {num_candidates} objects:
[
    {{
        "final_goal": "one of the goal terms above",
        "target_entity": "exact entity name from Valid Entity Names, or null",
        "reasoning": "comprehensive reasoning for this candidate",
        "confidence": 0.0-1.0,
        "contributing_factors": {{
            "values": 0.0-1.0,
            "emotion": 0.0-1.0,
            "background": 0.0-1.0,
            "environment": 0.0-1.0,
            "priority": 0.0-1.0
        }}
    }},
    ...
]
"""
        
        try:
            response = self._invoke_llm_synthesis_json_array(prompt, agent=agent)
            if not response:
                if verbose:
                    print("⚠️ 第5步 JSON 数组为空，使用多样化默认候选")
                return self._generate_diverse_default_candidates(agent, num_candidates)
            results = []
            for data in response:
                results.append(self._parse_synthesis_result(data))
            while len(results) < num_candidates:
                extras = self._generate_diverse_default_candidates(agent, num_candidates - len(results))
                results.extend(extras)
            return results[:num_candidates]
        except Exception as e:
            if verbose:
                print(f"⚠️ 生成多个候选目标失败: {e}，使用多样化默认候选")
            return self._generate_diverse_default_candidates(agent, num_candidates)
    
    def _invoke_llm_with_json_array(self, prompt: str) -> List[Dict[str, Any]]:
        """调用主 LLM 并解析 JSON 数组；失败则本地通用 JSON array GBNF。"""
        return self._primary_then_gbnf(
            prompt,
            grammar_str=generate_json_array_grammar(),
            parse_fn=self._extract_json_array,
            schema_hint="A JSON array of objects.",
            expect_list=True,
        )
    
    def _extract_json_array(self, text: str) -> List[Dict[str, Any]]:
        """从文本中提取JSON数组"""
        import json
        import re
        
        # 方法1: 尝试查找代码块中的JSON数组
        code_block_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
        if code_block_match:
            json_str = code_block_match.group(1)
            try:
                data = json.loads(json_str)
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass
        
        # 方法2: 查找第一个 [ 到最后一个 ] 之间的内容
        first_bracket = text.find('[')
        if first_bracket != -1:
            # 从第一个 [ 开始，尝试找到匹配的 ]
            bracket_count = 0
            end_pos = first_bracket
            for i in range(first_bracket, len(text)):
                if text[i] == '[':
                    bracket_count += 1
                elif text[i] == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        end_pos = i + 1
                        break
            
            if bracket_count == 0:
                json_str = text[first_bracket:end_pos]
                try:
                    data = json.loads(json_str)
                    if isinstance(data, list):
                        return data
                except json.JSONDecodeError:
                    pass
        
        # 方法3: 尝试提取所有JSON对象，然后组合成数组
        objects = []
        # 查找所有 { ... } 结构
        brace_count = 0
        start_pos = -1
        
        for i, char in enumerate(text):
            if char == '{':
                if brace_count == 0:
                    start_pos = i
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0 and start_pos != -1:
                    obj_str = text[start_pos:i+1]
                    try:
                        obj = json.loads(obj_str)
                        if isinstance(obj, dict):
                            objects.append(obj)
                    except json.JSONDecodeError:
                        pass
                    start_pos = -1
        
        if objects:
            return objects
        
        # 方法4: 如果都失败了，尝试直接解析整个文本
        try:
            data = json.loads(text.strip())
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                # 如果返回的是单个对象，包装成数组
                return [data]
        except json.JSONDecodeError:
            pass
        
        # 方法5: 尝试修复不完整的JSON（如果数组被截断）
        if first_bracket != -1:
            # 尝试找到最后一个完整的对象
            incomplete_json = text[first_bracket:]
            # 尝试添加缺失的括号
            open_brackets = incomplete_json.count('[')
            close_brackets = incomplete_json.count(']')
            open_braces = incomplete_json.count('{')
            close_braces = incomplete_json.count('}')
            
            # 如果缺少闭合括号，尝试添加
            if open_brackets > close_brackets:
                incomplete_json += ']' * (open_brackets - close_brackets)
            if open_braces > close_braces:
                incomplete_json += '}' * (open_braces - close_braces)
            
            try:
                data = json.loads(incomplete_json)
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass
        
        # 如果所有方法都失败，打印错误信息
        print(f"⚠️ JSON数组解析失败，原始文本: {text[:300]}")
        if len(text) > 300:
            print(f"   ... (文本被截断，总长度: {len(text)})")
        return []
    
    def _synthesis_from_goal_dict(self, goal_dict: Dict[str, Any]) -> SynthesisResult:
        return SynthesisResult(
            final_goal=goal_dict.get("goal", "Go to location"),
            target_entity=goal_dict.get("target_entity"),
            reasoning=goal_dict.get("reason", "根据角色设定"),
            confidence=float(goal_dict.get("confidence", 0.7)),
            contributing_factors={
                "values": 0.3,
                "emotion": 0.1,
                "background": 0.3,
                "environment": 0.3,
                "priority": float(goal_dict.get("priority", 0.7)),
            },
        )

    def _default_synthesis(self) -> SynthesisResult:
        """返回默认的综合决策结果"""
        return SynthesisResult(
            final_goal="Gain information",
            target_entity=None,
            reasoning="默认决策（LLM未初始化）",
            confidence=0.5,
            contributing_factors={
                "values": 0.25,
                "emotion": 0.25,
                "background": 0.25,
                "environment": 0.25,
                "priority": 0.5
            }
        )
    
    def _generate_diverse_default_candidates(self, agent: AgentProfile, num_candidates: int) -> List[SynthesisResult]:
        from goal_terms import get_all_goal_terms
        
        goal_terms = get_all_goal_terms()
        if not goal_terms:
            # 如果没有目标术语，返回相同的默认值
            return [self._default_synthesis() for _ in range(num_candidates)]
        
        # 获取可用实体
        available_locations = []
        available_npcs = []
        available_items = []
        
        if self.world:
            try:
                available_locations = self.world.list_entities("Location") or []
                available_npcs = [
                    n for n in (self.world.list_entities("Person") or [])
                    if n != agent.name
                ]
                available_items = self.world.list_entities("Item") or []
            except Exception:
                pass

        # Mock 模式优先使用可执行的目标组合；有锁门任务时插入 Use item on / Collect
        preferred_goals = ["Go to location", "Gain information", "Collect item"]
        if self.world:
            try:
                from profile_planner import profile_aware_pick_goal
                primary = profile_aware_pick_goal(self.world, agent)
                g0 = primary.get("goal")
                if g0 and g0 in get_all_goal_terms():
                    preferred_goals = [g0] + [g for g in preferred_goals if g != g0]
            except Exception:
                pass
        selected_goals = [
            preferred_goals[i % len(preferred_goals)] for i in range(num_candidates)
        ]

        visited: set = set()
        if self.world:
            rows = self.world.run_query(
                "MATCH (p:Person {name: $n})-[:VISITED]->(l:Location) RETURN l.name AS loc",
                n=agent.name,
            )
            visited = {r["loc"] for r in rows}

        candidates = []

        for i, goal_term in enumerate(selected_goals):
            target_entity = None
            if goal_term in ("Go to location", "Explore unknown location"):
                cur = agent.location
                unvisited = [loc for loc in available_locations if loc not in visited and loc != cur]
                pool = unvisited or [loc for loc in available_locations if loc != cur]
                if pool:
                    target_entity = pool[i % len(pool)]
            elif goal_term == "Gain information" and available_npcs:
                target_entity = available_npcs[i % len(available_npcs)]
            elif goal_term == "Use item on" and self.world:
                try:
                    from profile_planner import _pick_unlock
                    u = _pick_unlock(self.world, agent.name)
                    if u and u.get("goal") == "Use item on":
                        target_entity = u.get("target_entity")
                except Exception:
                    target_entity = None
            elif goal_term in ("Collect item", "Accumulate items") and available_items:
                pickable = [
                    n for n in available_items
                    if not str(n).endswith("Door")  # 粗滤；精确过滤靠 feasibility
                ]
                # 排除门
                if self.world:
                    try:
                        door_names = {
                            r["n"]
                            for r in self.world.run_query(
                                "MATCH (i:Item) WHERE coalesce(i.item_kind,'')='door' RETURN i.name AS n"
                            )
                            if r.get("n")
                        }
                        pickable = [n for n in available_items if n not in door_names]
                    except Exception:
                        pickable = list(available_items)
                if pickable:
                    target_entity = pickable[i % len(pickable)]

            base_priority = 0.85 - (i * 0.1)
            base_confidence = 0.7
            if goal_term == "Go to location":
                base_priority = 0.9

            priority = max(0.3, min(0.95, base_priority))
            confidence = max(0.3, min(0.95, base_confidence))

            # 生成推理说明
            reasoning_parts = [f"选择目标: {goal_term}"]
            if target_entity:
                reasoning_parts.append(f"目标实体: {target_entity}")
            reasoning_parts.append("（默认决策，LLM未初始化）")
            reasoning = "，".join(reasoning_parts)
            
            candidate = SynthesisResult(
                final_goal=goal_term,
                target_entity=target_entity,
                reasoning=reasoning,
                confidence=confidence,
                contributing_factors={
                    "values": 0.2 + (i * 0.05) % 0.1,
                    "emotion": 0.2 + ((i * 0.07) % 0.1),
                    "background": 0.2 + ((i * 0.09) % 0.1),
                    "environment": 0.2 + ((i * 0.11) % 0.1),
                    "priority": priority
                }
            )
            candidates.append(candidate)
        
        return candidates

    def _default_multiple_decision(self, agent: AgentProfile, num_candidates: int) -> Dict[str, Any]:
        """Mock 模式：返回多个默认候选目标。"""
        results = self._generate_diverse_default_candidates(agent, num_candidates)
        candidate_goals = []
        for result in results:
            goal = result.final_goal
            if not goal_term_exists(goal):
                goal = "Gain information"
            candidate_goals.append({
                "goal": goal,
                "target_entity": result.target_entity,
                "reason": result.reasoning,
                "confidence": result.confidence,
                "priority": result.contributing_factors.get("priority", 0.5)
                if isinstance(result.contributing_factors, dict)
                else 0.5,
                "metadata": {"contributing_factors": result.contributing_factors},
            })
        decision_process = DecisionProcess(
            values_assessment=ValuesAssessment([], [], 0.5, "mock", []),
            emotion_analysis=EmotionAnalysis("neutral", 0.5, "", "", []),
            background_consideration=BackgroundConsideration([], [], [], "", []),
            environment_analysis=EnvironmentAnalysis([], [], [], [], []),
            synthesis_result=results[0] if results else self._default_synthesis(),
        )
        return {
            "candidate_goals": candidate_goals,
            "decision_process": decision_process.to_dict(),
        }

    def _default_decision(self) -> Dict[str, Any]:
        """返回默认决策"""
        default_values = ValuesAssessment(
            relevant_values=[],
            value_conflicts=[],
            value_alignment=0.5,
            reasoning="默认值（LLM未初始化）",
            recommended_actions=[]
        )
        
        default_emotion = EmotionAnalysis(
            current_emotion="neutral",
            emotion_intensity=0.5,
            emotion_cause="默认值",
            emotion_impact="无影响",
            recommended_actions=[]
        )
        
        default_background = BackgroundConsideration(
            relevant_background=[],
            past_experiences=[],
            learned_lessons=[],
            background_influence="无影响",
            recommended_actions=[]
        )
        
        default_environment = EnvironmentAnalysis(
            opportunities=[],
            threats=[],
            available_resources=[],
            constraints=[],
            recommended_actions=[]
        )
        
        default_synthesis = SynthesisResult(
            final_goal="Gain information",
            target_entity=None,
            reasoning="默认决策（LLM未初始化）",
            confidence=0.5,
            contributing_factors={}
        )
        
        decision_process = DecisionProcess(
            values_assessment=default_values,
            emotion_analysis=default_emotion,
            background_consideration=default_background,
            environment_analysis=default_environment,
            synthesis_result=default_synthesis
        )
        
        return {
            "goal_data": {
                "goal": "Gain information",
                "target_entity": None,
                "reason": "默认决策（LLM未初始化）"
            },
            "decision_process": decision_process.to_dict()
        }

