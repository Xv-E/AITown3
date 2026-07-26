"""
游戏主循环 — 持续时钟 tick：决策 → 规划 → 带耗时执行 → 评估/重规划。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from action_timing import get_action_duration
from agent_profile import AgentProfile
from goal_feasibility import assess_goal, pick_best_feasible, sanitize_goal_target
from goal_planner import plan_for_goal, wait_plan

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

PlanStep = Tuple[str, List[str]]

HARD_INTERRUPT_HP = 20
DECISION_COOLDOWN = 5.0
REPLAN_BACKOFF = 3.0


@dataclass
class ActionResult:
    success: bool
    message: str
    agent_name: str
    action_type: str
    target: Optional[str] = None
    should_interrupt: bool = False
    interrupt_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingAction:
    name: str
    args: List[str]
    started_at: float
    duration: float
    completes_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "args": list(self.args),
            "started_at": self.started_at,
            "duration": self.duration,
            "completes_at": self.completes_at,
        }


@dataclass
class AgentState:
    agent: AgentProfile
    current_plan: Optional[List[PlanStep]] = None
    plan_index: int = 0
    last_action_result: Optional[ActionResult] = None
    last_goal: Optional[Dict[str, Any]] = None
    is_interrupted: bool = False
    interrupt_count: int = 0
    plan_engine: str = ""
    plan_error: Optional[str] = None
    # 持续时钟字段
    sim_busy_until: float = 0.0
    pending_action: Optional[PendingAction] = None
    replan_backoff_until: float = 0.0
    decision_cooldown_until: float = 0.0
    status: str = "idle"  # idle | acting | deciding | planning | failed
    tried_goal_keys: List[str] = field(default_factory=list)
    replan_reason: Optional[str] = None
    mission_complete: bool = False
    mission_complete_reason: str = ""
    # UI overlay 缓存
    decision_mode: Optional[str] = None
    decision_process: Optional[Dict[str, Any]] = None
    candidate_goals: List[Dict[str, Any]] = field(default_factory=list)
    decision_steps: List[Dict[str, Any]] = field(default_factory=list)
    # 日常日程 / 社交打断
    routine_slot_key: Optional[str] = None
    last_social_at: float = -1e9
    recent_talk_targets: List[str] = field(default_factory=list)


def _goal_key(goal_data: Optional[Dict[str, Any]]) -> str:
    if not goal_data:
        return ""
    ia = (
        goal_data.get("interact_action")
        or (goal_data.get("metadata") or {}).get("interact_action")
        or ""
    )
    return f"{goal_data.get('goal')}|{goal_data.get('target_entity')}|{ia}"


class GameLoop:
    def __init__(
        self,
        world: "Neo4jWorld",
        *,
        decider_factory: Optional[Callable] = None,
        memory=None,
        max_turns: Optional[int] = None,
        verbose: bool = True,
        plan_engines: Optional[List[str]] = None,
        decision_cooldown: float = DECISION_COOLDOWN,
        replan_backoff: float = REPLAN_BACKOFF,
    ):
        self.world = world
        self.memory = memory
        self.agents: List[AgentState] = []
        self.turn_count = 0  # 兼容：等同 tick_count
        self.tick_count = 0
        self.sim_time = 0.0
        self.max_turns = max_turns
        self.verbose = verbose
        self.plan_engines = plan_engines or ["unified_planning", "bfs"]
        self.decider_factory = decider_factory or self._default_decider_factory
        self.force_mock = False
        self.decision_cooldown = decision_cooldown
        self.replan_backoff = replan_backoff

    def _default_decider_factory(self, world, memory=None, force_mock=False):
        from multi_step_decider import MultiStepDecider

        return MultiStepDecider(world, memory=memory, force_mock=force_mock)

    def add_agent(
        self,
        agent: AgentProfile,
        decider=None,
        *,
        force_mock: bool = False,
    ) -> None:
        if self.get_agent_state(agent.name) is not None:
            return
        if self.memory:
            agent.set_memory(self.memory)
        if decider is None:
            decider = self.decider_factory(self.world, memory=self.memory, force_mock=force_mock)
        agent._decider = decider  # noqa: SLF001
        self.agents.append(AgentState(agent=agent))
        if self.verbose:
            print(f"[+] 添加 Agent: {agent.name}")

    def get_agent_state(self, name: str) -> Optional[AgentState]:
        for state in self.agents:
            if state.agent.name == name:
                return state
        return None

    def _is_player(self, state: AgentState) -> bool:
        rows = self.world.run_query(
            """
            MATCH (p:Person {name: $n})
            RETURN coalesce(p.is_player, false) AS is_player,
                   coalesce(p.role, '') AS role
            LIMIT 1
            """,
            n=state.agent.name,
        )
        if not rows:
            return False
        return bool(rows[0].get("is_player")) or str(rows[0].get("role") or "") == "player"

    def _is_dead(self, state: AgentState) -> bool:
        rows = self.world.run_query(
            """
            MATCH (p:Person {name: $n})
            RETURN coalesce(p.dead, false) AS dead, coalesce(p.hp, 0) AS hp
            LIMIT 1
            """,
            n=state.agent.name,
        )
        if not rows:
            return True
        return bool(rows[0].get("dead")) or int(rows[0].get("hp") or 0) <= 0

    def run_turn(
        self,
        *,
        auto_plan: bool = True,
        characters: Optional[List[str]] = None,
        include_player: bool = False,
    ) -> Dict[str, Any]:
        """兼容旧回合 API：一次 tick，动作立即完成（instant）。"""
        return self.tick(
            1.0,
            auto_plan=auto_plan,
            characters=characters,
            instant=True,
            include_player=include_player,
        )

    def tick(
        self,
        dt: float = 0.25,
        *,
        auto_plan: bool = True,
        characters: Optional[List[str]] = None,
        instant: bool = False,
        include_player: bool = False,
        max_decisions_per_tick: int = 1,
    ) -> Dict[str, Any]:
        self.sim_time += max(0.0, float(dt))
        self.tick_count += 1
        self.turn_count = self.tick_count
        world_report: Dict[str, Any] = {}
        try:
            from world_systems import tick_world_systems

            world_report = tick_world_systems(self.world, float(dt), self.sim_time) or {}
        except Exception as e:
            if self.verbose:
                print(f"[WARN] world_systems: {e}")
        if self.verbose:
            print(f"\n{'=' * 60}")
            clock = world_report.get("clock") or f"t={self.sim_time:.2f}"
            print(f"[TICK] #{self.tick_count} {clock} dt={dt}")
            fire = world_report.get("campfire") or {}
            if fire.get("name") is not None:
                print(
                    f"   物品 {fire.get('name')}: fuel={float(fire.get('fuel') or 0):.1f} "
                    f"lit={fire.get('lit')}"
                )
            alerts = world_report.get("alerts") or []
            if alerts:
                a0 = alerts[0]
                print(f"   script告警: [{a0.get('signal')}] {a0.get('item')}: {a0.get('message')}")
            print(f"{'=' * 60}\n")

        results = {
            "turn": self.tick_count,
            "tick": self.tick_count,
            "sim_time": self.sim_time,
            "world": world_report,
            "agents": [],
        }
        targets = self.agents
        if characters is not None:
            char_set = set(characters)
            targets = [s for s in self.agents if s.agent.name in char_set]

        # 两阶段：先推进所有到期动作/计划步骤，再做决策。
        # 避免某个角色的 LLM/PDDL 卡住整拍，导致其他人的 move 永远完不成。
        by_name: Dict[str, Dict[str, Any]] = {}
        for state in targets:
            try:
                by_name[state.agent.name] = self._process_agent_tick(
                    state,
                    auto_plan=False,
                    instant=instant,
                    include_player=include_player,
                )
            except Exception as e:
                if self.verbose:
                    print(f"[ERR] Agent {state.agent.name} tick 失败: {e}")
                state.status = "failed"
                # 防止执行异常后 pending 永远挂起
                if state.pending_action and self.sim_time >= state.pending_action.completes_at:
                    state.pending_action = None
                    state.sim_busy_until = 0.0
                by_name[state.agent.name] = {
                    "agent": state.agent.name,
                    "error": str(e),
                }

        if auto_plan:
            decided = 0
            limit = max(0, int(max_decisions_per_tick))
            for state in targets:
                if limit and decided >= limit:
                    break
                if state.pending_action:
                    continue
                if state.current_plan and state.plan_index < len(state.current_plan):
                    continue
                if not self._may_auto_plan(state, include_player=include_player):
                    continue
                if self.sim_time < state.replan_backoff_until:
                    continue
                if self.sim_time < state.decision_cooldown_until and not state.is_interrupted:
                    continue
                try:
                    by_name[state.agent.name] = self._process_agent_tick(
                        state,
                        auto_plan=True,
                        instant=instant,
                        include_player=include_player,
                    )
                    if by_name[state.agent.name].get("phase") in (
                        "deciding",
                        "started",
                        "executing",
                    ) or by_name[state.agent.name].get("decided"):
                        decided += 1
                except Exception as e:
                    if self.verbose:
                        print(f"[ERR] Agent {state.agent.name} 决策 tick 失败: {e}")
                    state.status = "failed"
                    by_name[state.agent.name] = {
                        "agent": state.agent.name,
                        "error": str(e),
                    }
                    decided += 1

        results["agents"] = [by_name[s.agent.name] for s in targets if s.agent.name in by_name]
        return results

    def run(self, max_turns: Optional[int] = None) -> Dict[str, Any]:
        limit = max_turns if max_turns is not None else self.max_turns
        game_result = {"total_turns": 0, "turns": [], "final_state": {}}
        while True:
            if limit and self.tick_count >= limit:
                if self.verbose:
                    print(f"\n[END] 达到最大 tick 数 {limit}，结束")
                break
            turn = self.run_turn()
            game_result["turns"].append(turn)
            game_result["total_turns"] = self.tick_count
        game_result["final_state"] = {
            s.agent.name: {
                "location": s.agent.location,
                "hp": s.agent.hp,
                "plan_remaining": len(s.current_plan or []) - s.plan_index
                if s.current_plan
                else 0,
                "status": s.status,
                "sim_busy_until": s.sim_busy_until,
            }
            for s in self.agents
        }
        return game_result

    def _may_auto_plan(self, state: AgentState, *, include_player: bool) -> bool:
        if self._is_dead(state):
            return False
        if self._is_player(state) and not include_player:
            return False
        return True

    def _process_agent_tick(
        self,
        state: AgentState,
        *,
        auto_plan: bool = True,
        instant: bool = False,
        include_player: bool = False,
    ) -> Dict[str, Any]:
        agent = state.agent
        result: Dict[str, Any] = {
            "agent": agent.name,
            "character": agent.name,
            "phase": None,
            "goal": None,
            "action": None,
            "interrupted": False,
            "skipped": False,
            "sim_time": self.sim_time,
            "status": state.status,
        }
        can_plan = auto_plan and self._may_auto_plan(state, include_player=include_player)

        if not auto_plan and not state.current_plan and not state.pending_action:
            result["skipped"] = True
            result["reason"] = "idle"
            state.status = "idle"
            return result

        agent.refresh_from_world()

        # 硬中断：死亡 / 极低 HP — 可取消进行中动作（不 apply）
        if self._hard_interrupt(state):
            result["interrupted"] = True
            result["phase"] = "hard_interrupt"
            if can_plan and not self._is_dead(state):
                self._failure_replan(state, reason=state.replan_reason or "硬中断", instant=instant)
                result["goal"] = state.last_goal
            result["status"] = state.status
            return result

        # 软打断：日程换段 / 同地遇见他人 → 改计划（不取消进行中动作）
        if (
            can_plan
            and not state.pending_action
            and not self._is_dead(state)
            and self._soft_opportunity_interrupt(state, instant=instant)
        ):
            result["interrupted"] = True
            result["phase"] = "opportunity_interrupt"
            result["goal"] = state.last_goal
            result["replan_reason"] = state.replan_reason
            result["status"] = state.status
            # 打断后立刻开跑新计划第一步
            if state.current_plan and state.plan_index < len(state.current_plan):
                started = self._start_next_step(state, instant=instant)
                if started:
                    result["phase"] = "started"
                    result["action"] = started
                    if instant and state.pending_action:
                        action_entry = self._complete_pending(state)
                        result["phase"] = "executing"
                        result["action"] = action_entry
                        result["success"] = action_entry.get("success") if action_entry else None
                        result["message"] = action_entry.get("message") if action_entry else None
            return result

        # 完成到期动作
        if state.pending_action and self.sim_time >= state.pending_action.completes_at:
            result["phase"] = "completing"
            action_entry = self._complete_pending(state)
            result["action"] = action_entry
            result["success"] = action_entry.get("success") if action_entry else None
            result["message"] = action_entry.get("message") if action_entry else None
            if action_entry and action_entry.get("dialogue"):
                result["dialogue"] = action_entry["dialogue"]

            if state.last_action_result and not state.last_action_result.success:
                if can_plan:
                    self._failure_replan(
                        state,
                        reason=state.last_action_result.message,
                        instant=instant,
                    )
                    result["goal"] = state.last_goal
                    result["interrupted"] = True
                result["status"] = state.status
                return result

        # 仍在忙碌
        if state.pending_action and self.sim_time < state.pending_action.completes_at:
            state.status = "acting"
            result["skipped"] = True
            result["reason"] = "busy"
            result["phase"] = "acting"
            result["pending"] = state.pending_action.to_dict()
            result["busy_remaining"] = max(0.0, state.sim_busy_until - self.sim_time)
            result["status"] = state.status
            return result

        # 有剩余计划：启动下一步
        if state.current_plan and state.plan_index < len(state.current_plan):
            started = self._start_next_step(state, instant=instant)
            if started is None:
                # 预检失败 → 重规划
                if can_plan:
                    self._failure_replan(
                        state,
                        reason=state.replan_reason or "下一步预检失败",
                        instant=instant,
                    )
                    result["goal"] = state.last_goal
                    result["interrupted"] = True
                result["status"] = state.status
                return result
            result["phase"] = "started"
            result["action"] = started
            # instant：同 tick 内立即完成
            if instant and state.pending_action:
                action_entry = self._complete_pending(state)
                result["phase"] = "executing"
                result["action"] = action_entry
                result["success"] = action_entry.get("success") if action_entry else None
                result["message"] = action_entry.get("message") if action_entry else None
                if action_entry and action_entry.get("dialogue"):
                    result["dialogue"] = action_entry["dialogue"]
                if state.last_action_result and not state.last_action_result.success:
                    if can_plan:
                        self._failure_replan(
                            state,
                            reason=state.last_action_result.message,
                            instant=instant,
                        )
                        result["interrupted"] = True
            result["status"] = state.status
            result["goal"] = state.last_goal
            return result

        # 空闲：决策 + 规划（默认跳过玩家，除非 include_player）
        if not auto_plan:
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "idle"
            result["status"] = state.status
            return result

        if self._is_dead(state):
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "player_or_dead"
            result["status"] = state.status
            return result

        if self._is_player(state) and not include_player:
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "player_or_dead"
            result["status"] = state.status
            return result

        # 任务节点仅软同步 UI；不因「任务完成」硬停自动规划
        self._sync_quest_ui(state)

        if self.sim_time < state.replan_backoff_until:
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "backoff"
            result["status"] = state.status
            return result

        if self.sim_time < state.decision_cooldown_until and not state.is_interrupted:
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "decision_cooldown"
            result["status"] = state.status
            return result

        decided = self._decide_and_plan(state, instant=instant)
        result["phase"] = "deciding"
        result["goal"] = state.last_goal
        result.update(decided)
        result["status"] = state.status

        # 决策后立即启动第一步（若有计划）
        if state.current_plan and state.plan_index < len(state.current_plan):
            started = self._start_next_step(state, instant=instant)
            if started:
                result["phase"] = "started"
                result["action"] = started
                if instant and state.pending_action:
                    action_entry = self._complete_pending(state)
                    result["phase"] = "executing"
                    result["action"] = action_entry
                    result["success"] = action_entry.get("success") if action_entry else None
                    result["message"] = action_entry.get("message") if action_entry else None
        return result

    def _hard_interrupt(self, state: AgentState) -> bool:
        agent = state.agent
        if self._is_dead(state):
            self._cancel_pending(state, apply=False)
            state.current_plan = None
            state.plan_index = 0
            state.is_interrupted = True
            state.interrupt_count += 1
            state.status = "failed"
            state.replan_reason = "角色已死亡"
            return True
        if agent.hp < HARD_INTERRUPT_HP and (state.pending_action or state.current_plan):
            # 进行中才硬打断
            if state.pending_action:
                self._cancel_pending(state, apply=False)
            state.current_plan = None
            state.plan_index = 0
            state.is_interrupted = True
            state.interrupt_count += 1
            state.replan_reason = "HP 过低"
            state.status = "failed"
            return True
        return False

    def _cancel_pending(self, state: AgentState, *, apply: bool) -> None:
        if apply and state.pending_action:
            self._complete_pending(state)
            return
        state.pending_action = None
        state.sim_busy_until = 0.0

    def _start_next_step(
        self,
        state: AgentState,
        *,
        instant: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if not state.current_plan or state.plan_index >= len(state.current_plan):
            return None
        action_name, args = state.current_plan[state.plan_index]
        ok, reason = self._check_plan_step(state.agent, action_name, args)
        if not ok:
            state.replan_reason = reason
            state.plan_error = reason
            state.status = "failed"
            if self.verbose:
                print(f"[WARN] [{state.agent.name}] 预检失败: {reason}")
            return None

        duration = get_action_duration(action_name, instant=instant)
        pending = PendingAction(
            name=action_name,
            args=list(args),
            started_at=self.sim_time,
            duration=duration,
            completes_at=self.sim_time + duration,
        )
        state.pending_action = pending
        state.sim_busy_until = pending.completes_at
        state.status = "acting"
        if self.verbose:
            print(
                f"[START] [{state.agent.name}] {action_name}({', '.join(args)}) "
                f"duration={duration:.2f} until={pending.completes_at:.2f}"
            )
        return {
            "action": action_name,
            "args": args,
            "started": True,
            "duration": duration,
            "completes_at": pending.completes_at,
            "step": state.plan_index + 1,
        }

    def _complete_pending(self, state: AgentState) -> Optional[Dict[str, Any]]:
        pending = state.pending_action
        if not pending:
            return None
        agent = state.agent
        action_name, args = pending.name, pending.args
        if self.verbose:
            print(
                f"[EXEC] [{agent.name}] 完成 {state.plan_index + 1}/"
                f"{len(state.current_plan or [])}: {action_name}({', '.join(args)})"
            )
        try:
            action_result = self._execute_plan_step(agent, action_name, args)
        except Exception as e:
            action_result = ActionResult(
                success=False,
                message=f"执行异常: {e}",
                agent_name=agent.name,
                action_type=action_name,
            )
        finally:
            # 无论 apply 是否抛错，都必须清掉 pending，否则会永远卡在「执行 move」
            state.pending_action = None
            state.sim_busy_until = 0.0

        state.last_action_result = action_result
        state.plan_index += 1
        self._remember_action(agent, action_name, args, action_result)

        entry = {
            "action": action_name,
            "args": args,
            "type": action_result.action_type,
            "target": action_result.target,
            "success": action_result.success,
            "message": action_result.message,
            "step": state.plan_index,
        }
        if action_result.metadata.get("dialogue"):
            entry["dialogue"] = action_result.metadata["dialogue"]
        if action_result.metadata.get("interaction"):
            entry["interaction"] = action_result.metadata["interaction"]

        if action_name == "talk" and action_result.success and len(args) >= 2:
            other = args[1] if args[0] == agent.name else args[0]
            if other and other != agent.name:
                state.recent_talk_targets = [
                    t for t in state.recent_talk_targets if t != other
                ][-4:]
                state.recent_talk_targets.append(other)
                state.last_social_at = self.sim_time

        if self.verbose:
            icon = "OK" if action_result.success else "FAIL"
            print(f"   {icon} {action_result.message}")

        if action_result.success and state.current_plan and state.plan_index >= len(state.current_plan):
            if self.verbose:
                print(f"[DONE] [{agent.name}] 计划完成")
            agent.complete_current_goal()
            state.current_plan = None
            state.plan_index = 0
            state.status = "idle"
            state.decision_cooldown_until = self.sim_time + self.decision_cooldown
            state.tried_goal_keys.clear()
            state.replan_reason = None
            self._sync_quest_ui(state)
        elif action_result.success:
            state.status = "idle"
            self._sync_quest_ui(state)
        else:
            state.status = "failed"
            state.is_interrupted = True

        return entry

    def _sync_quest_ui(self, state: AgentState) -> None:
        """更新 Quest stage 供 UI；不设置 mission_complete 硬停。"""
        agent = state.agent
        try:
            from mission import sync_quest_progress

            note = sync_quest_progress(self.world, agent.name)
            if note and self.verbose:
                print(f"[QUEST] [{agent.name}] {note}")
        except Exception:
            pass
        # 兼容字段：不再用于跳过 tick
        state.mission_complete = False
        state.mission_complete_reason = ""

    def all_auto_agents_mission_complete(
        self,
        *,
        include_player: bool = False,
        characters: Optional[List[str]] = None,
    ) -> bool:
        """不再自动停仿真；由玩家停止，或由角色自行改目标。"""
        return False

    def _refresh_mission_complete(self, state: AgentState) -> bool:
        """兼容旧名：只做 UI 同步，永不返回「应停规划」。"""
        self._sync_quest_ui(state)
        return False

    def _check_plan_step(
        self,
        agent: AgentProfile,
        action_name: str,
        args: List[str],
    ) -> Tuple[bool, str]:
        from registry import get_registry

        registry = get_registry()
        action = registry.get(action_name)
        if action is None:
            if action_name in ("noop",):
                return True, "ok"
            return False, f"未知动作: {action_name}"
        kwargs = {p.name: v for p, v in zip(action.parameters, args)}
        return action.check(self.world, **kwargs)

    def _soft_opportunity_interrupt(
        self,
        state: AgentState,
        *,
        instant: bool = False,
    ) -> bool:
        """篝火告急 / 日程换段 / 同地遇见 → 清空当前计划并立刻采纳新目标。"""
        from routine import (
            current_slot,
            hour_of_day,
            is_night,
            load_person_routine,
            pick_social_target,
            routine_goal_candidate,
            should_social_interrupt,
            social_interrupt_goal,
        )
        from world_systems import fire_emergency_goal

        agent = state.agent

        # 0) 物品脚本告警（燃料不足等）
        fire_goal = fire_emergency_goal(self.world, agent.name)
        if fire_goal:
            role_bg = f"{agent.background or ''} {agent.personality or ''} {agent.name}".lower()
            cares = any(
                k in role_bg
                for k in ("fire", "campfire", "篝火", "lumber", "wood", "伐木", "ash", "rowan")
            )
            respond = bool(
                fire_goal.get("tool_hint")
                or cares
                or is_night(hour_of_day(self.sim_time))
                or fire_goal.get("script_signal") == "critical"
            )
            if respond:
                same = (
                    state.last_goal
                    and state.last_goal.get("goal") == fire_goal.get("goal")
                    and state.last_goal.get("target_entity") == fire_goal.get("target_entity")
                )
                if not same:
                    assessed = assess_goal(
                        self.world, agent.name, fire_goal, engines=self.plan_engines
                    )
                    if assessed.ok and not assessed.already_satisfied and assessed.plan:
                        if self.verbose:
                            print(f"[SCRIPT] [{agent.name}] {fire_goal.get('reason')}")
                        self._adopt_planned_goal(
                            state, assessed, reason=fire_goal.get("reason") or "物品脚本告警"
                        )
                        return True

        slots = load_person_routine(self.world, agent.name)
        if not slots:
            return False
        slot = current_slot(slots, self.sim_time)
        if slot is None:
            return False

        slot_changed = state.routine_slot_key is not None and state.routine_slot_key != slot.key
        state.routine_slot_key = slot.key

        # 1) 同地社交机会（优先级高于「继续旧日程」）
        social_target = pick_social_target(
            self.world,
            agent.name,
            agent.location,
            recent_talk_targets=state.recent_talk_targets,
        )
        if should_social_interrupt(
            slot=slot,
            current_goal=state.last_goal,
            social_target=social_target,
            last_social_at=state.last_social_at,
            sim_time=self.sim_time,
        ):
            goal = social_interrupt_goal(social_target or "", activity=slot.activity)
            assessed = assess_goal(self.world, agent.name, goal, engines=self.plan_engines)
            if assessed.ok and not assessed.already_satisfied and assessed.plan:
                if self.verbose:
                    print(f"[MEET] [{agent.name}] 遇见 {social_target}，改计划交谈")
                self._adopt_planned_goal(state, assessed, reason=goal.get("reason") or "遇见交流")
                state.last_social_at = self.sim_time
                return True

        # 2) 日程时段切换：离开旧计划，跟随新槽位
        if not slot_changed:
            return False
        if not state.current_plan and not state.last_goal:
            return False

        cand = routine_goal_candidate(
            slot,
            character=agent.name,
            current_location=agent.location,
        )
        # 社交槽位已到场但尚无对象：不必打断
        if cand.get("goal") == "Gain information" and not cand.get("target_entity"):
            social_target = pick_social_target(
                self.world,
                agent.name,
                agent.location,
                recent_talk_targets=state.recent_talk_targets,
            )
            if social_target:
                cand = social_interrupt_goal(social_target, activity=slot.activity)
            else:
                cand = {
                    "goal": "Go to location",
                    "target_entity": slot.location,
                    "reason": f"日程换段：前往 {slot.location}",
                    "priority": 0.72,
                    "confidence": 0.8,
                    "source": "routine",
                }

        # 与当前目标相同则不打断
        if (
            state.last_goal
            and state.last_goal.get("goal") == cand.get("goal")
            and state.last_goal.get("target_entity") == cand.get("target_entity")
        ):
            return False

        assessed = assess_goal(self.world, agent.name, cand, engines=self.plan_engines)
        if not assessed.ok or assessed.already_satisfied:
            return False
        if not assessed.plan:
            return False
        if self.verbose:
            print(
                f"[ROUTINE] [{agent.name}] 日程换段 → "
                f"{cand.get('goal')}->{cand.get('target_entity')} ({slot.activity})"
            )
        self._adopt_planned_goal(
            state,
            assessed,
            reason=cand.get("reason") or "日程换段",
        )
        return True

    def _adopt_planned_goal(
        self,
        state: AgentState,
        assessed,
        *,
        reason: str,
    ) -> None:
        state.replan_reason = reason
        state.interrupt_count += 1
        state.is_interrupted = False
        state.current_plan = list(assessed.plan or [])
        state.plan_index = 0
        state.plan_engine = assessed.engine
        state.plan_error = None
        state.last_goal = dict(assessed.goal_data)
        state.agent.set_current_goal(state.last_goal)
        state.status = "ready" if state.current_plan else "idle"
        state.decision_cooldown_until = 0.0

    def _routine_candidates(self, state: AgentState) -> List[Dict[str, Any]]:
        from routine import (
            current_slot,
            load_person_routine,
            pick_social_target,
            routine_goal_candidate,
            social_interrupt_goal,
        )
        from world_systems import fire_emergency_goal

        out: List[Dict[str, Any]] = []
        fire_goal = fire_emergency_goal(self.world, state.agent.name)
        if fire_goal:
            out.append(fire_goal)

        slots = load_person_routine(self.world, state.agent.name)
        if not slots:
            return out
        slot = current_slot(slots, self.sim_time)
        if slot is None:
            return out
        state.routine_slot_key = slot.key
        cand = routine_goal_candidate(
            slot,
            character=state.agent.name,
            current_location=state.agent.location,
        )
        if cand.get("goal") == "Gain information" and not cand.get("target_entity"):
            target = pick_social_target(
                self.world,
                state.agent.name,
                state.agent.location,
                recent_talk_targets=state.recent_talk_targets,
            )
            if target:
                out.append(social_interrupt_goal(target, activity=slot.activity))
            else:
                out.append(
                    {
                        "goal": "Go to location",
                        "target_entity": slot.location,
                        "reason": f"日程：前往 {slot.location} 以便遇见他人",
                        "priority": 0.72,
                        "confidence": 0.8,
                        "source": "routine",
                    }
                )
            return out
        out.append(cand)
        return out

    def _ephemeral_action_candidates(self, state: AgentState) -> List[Dict[str, Any]]:
        """同地临时 Cypher action → 候选（带剪枝）。"""
        try:
            from item_interact import ephemeral_action_goal_candidates

            ctx = {
                "goal": (state.last_goal or {}).get("goal"),
                "current_goal": getattr(state.agent, "goal", None),
                "reason": (state.last_goal or {}).get("reason"),
                "routine_activity": getattr(state, "routine_slot_key", None),
            }
            return ephemeral_action_goal_candidates(
                self.world,
                state.agent.name,
                force_mock=bool(self.force_mock),
                memory=self.memory,
                same_location_only=True,
                prune=True,
                max_per_item=2,
                max_total=4,
                context=ctx,
            )
        except Exception as e:
            if self.verbose:
                print(f"[WARN] ephemeral candidates: {e}")
            return []

    def _decide_and_plan(
        self,
        state: AgentState,
        *,
        instant: bool = False,
    ) -> Dict[str, Any]:
        agent = state.agent
        decider = getattr(agent, "_decider", None)
        out: Dict[str, Any] = {"decided": False}

        state.status = "deciding"
        if self.verbose:
            print(f"[THINK] [{agent.name}] 决策 | 位置: {agent.location} | HP: {agent.hp}")

        # 供 PromptBuilder 显示正确的一日时段
        agent._sim_time = self.sim_time  # noqa: SLF001

        # 清空旧候选，重新生成
        agent.goal_manager.clear_all()
        routine_cands = self._routine_candidates(state)
        ephemeral_cands = self._ephemeral_action_candidates(state)
        if self.force_mock:
            from profile_planner import profile_aware_pick_goal

            fallback = profile_aware_pick_goal(self.world, agent)
            candidates = list(routine_cands) + list(ephemeral_cands) + [fallback]
            state.decision_mode = "mock"
            state.decision_process = {
                "planner": "profile_aware",
                "settings": agent.get_settings_summary(),
                "note": "规则决策：日程 + 临时 interact actions + 性格",
                "routine": routine_cands[0] if routine_cands else None,
                "ephemeral_n": len(ephemeral_cands),
            }
        else:
            candidates = agent.generate_candidate_goals(
                decider=decider,
                parent_goal=None,
                current_goal=(
                    (state.last_goal.get("goal") if state.last_goal else None)
                    or agent.goal
                    or None
                ),
                last_action_result=state.last_action_result.message
                if state.last_action_result
                else None,
                last_action_success=state.last_action_result.success
                if state.last_action_result
                else True,
                num_candidates=1,
                verbose=self.verbose,
            )
            # 日程 + 临时 actions 插到前面；可行性按分数排序
            candidates = (
                list(routine_cands)
                + list(ephemeral_cands)
                + list(candidates or [])
            )
            state.decision_mode = "llm"
            state.decision_process = {
                "force_mock": False,
                "settings": agent.get_settings_summary(),
                "note": "日程 + 临时 interact actions + LLM，经可行性筛选",
                "routine": routine_cands[0] if routine_cands else None,
                "ephemeral_n": len(ephemeral_cands),
            }
        state.candidate_goals = list(candidates)
        state.decision_steps = []

        exclude = list(state.tried_goal_keys)
        llm_goal = None
        if candidates:
            # 展示「非日程」的首个 LLM/规则候选，便于 UI 对比
            for c in candidates:
                if (c or {}).get("source") in (
                    "routine",
                    "social_interrupt",
                    "item_script",
                    "fire_emergency",
                    "ephemeral_action",
                ):
                    continue
                c0 = dict(c or {})
                if not c0.get("goal") and c0.get("final_goal"):
                    c0["goal"] = c0.get("final_goal")
                llm_goal = {
                    "goal": c0.get("goal"),
                    "target_entity": c0.get("target_entity"),
                    "interact_action": c0.get("interact_action"),
                }
                break

        # 物品脚本告警 / 日程优先
        picked = None
        for rc in routine_cands:
            if rc.get("source") not in ("item_script", "fire_emergency"):
                continue
            assessed = assess_goal(
                self.world,
                agent.name,
                rc,
                engines=self.plan_engines,
            )
            key = _goal_key(assessed.goal_data)
            if key in exclude:
                continue
            if assessed.ok and not assessed.already_satisfied and assessed.plan:
                picked = assessed
                break

        if picked is None:
            for rc in routine_cands:
                need_travel = (
                    rc.get("goal") == "Go to location"
                    and rc.get("target_entity")
                    and rc.get("target_entity") != agent.location
                )
                if not need_travel and rc.get("source") == "routine":
                    continue
                if rc.get("source") in ("item_script", "fire_emergency"):
                    continue
                assessed = assess_goal(
                    self.world,
                    agent.name,
                    rc,
                    engines=self.plan_engines,
                )
                key = _goal_key(assessed.goal_data)
                if key in exclude:
                    continue
                if assessed.ok and not assessed.already_satisfied and assessed.plan:
                    picked = assessed
                    break

        if picked is None:
            picked = pick_best_feasible(
                self.world,
                agent.name,
                candidates,
                engines=self.plan_engines,
                exclude_goals=exclude,
            )

        if picked is None:
            # profile 兜底
            try:
                from profile_planner import profile_aware_pick_goal

                fallback = profile_aware_pick_goal(self.world, agent)
                state.decision_mode = "profile"
                state.decision_process = {
                    "planner": "profile_aware",
                    "settings": agent.get_settings_summary(),
                    "note": "候选均不可行/已满足后的规则兜底",
                    "llm_goal_overridden": llm_goal,
                    "fallback_goal": {
                        "goal": fallback.get("goal"),
                        "target_entity": fallback.get("target_entity"),
                    },
                }
                state.candidate_goals = [fallback]
                if self.verbose and llm_goal:
                    print(
                        f"[GOAL] [{agent.name}] LLM目标被覆盖: "
                        f"{llm_goal.get('goal')}->{llm_goal.get('target_entity')} "
                        f"=> {fallback.get('goal')}->{fallback.get('target_entity')}"
                    )
                picked = assess_goal(
                    self.world,
                    agent.name,
                    fallback,
                    engines=self.plan_engines,
                )
                if not picked.ok:
                    picked = None
            except Exception:
                picked = None

        if picked is None:
            # wait + backoff
            wp = wait_plan(agent.name)
            state.last_goal = {
                "goal": "Go to location",
                "target_entity": agent.location,
                "reason": "无可行动目标，等待",
                "priority": 0.1,
                "confidence": 0.1,
            }
            state.current_plan = list(wp.plan)
            state.plan_index = 0
            state.plan_engine = wp.engine
            state.plan_error = None
            state.is_interrupted = False
            state.status = "planning"
            state.replan_backoff_until = self.sim_time + self.replan_backoff
            state.decision_cooldown_until = self.sim_time + self.decision_cooldown
            out["decided"] = True
            out["fallback"] = "wait"
            return out

        goal_data = picked.goal_data
        target, _ = sanitize_goal_target(
            self.world,
            agent.name,
            goal_data.get("goal", ""),
            goal_data.get("target_entity"),
        )
        goal_data = dict(goal_data)
        goal_data["target_entity"] = target
        state.last_goal = goal_data
        agent.set_current_goal(goal_data)
        out["goal"] = goal_data
        out["decided"] = True

        if self.verbose:
            ia = goal_data.get("interact_action") or ""
            extra = f" [{ia}]" if ia else ""
            print(
                f"[GOAL] [{agent.name}] 选择: {goal_data.get('goal')} -> "
                f"{goal_data.get('target_entity')}{extra}"
            )

        state.status = "planning"
        if picked.already_satisfied or not picked.plan:
            agent.complete_current_goal()
            state.current_plan = None
            state.plan_index = 0
            state.is_interrupted = False
            state.plan_engine = picked.engine or "satisfied"
            state.plan_error = None
            state.status = "idle"
            state.decision_cooldown_until = self.sim_time + self.decision_cooldown
            self._remember_action(
                agent,
                "noop",
                [],
                ActionResult(
                    success=True,
                    message=f"目标已满足: {goal_data.get('goal')}",
                    agent_name=agent.name,
                    action_type="noop",
                ),
            )
            return out

        state.current_plan = list(picked.plan or [])
        state.plan_index = 0
        state.plan_engine = picked.engine
        state.plan_error = None
        state.is_interrupted = False
        state.status = "ready" if state.current_plan else "idle"
        # 有待执行计划时不进入决策冷却，否则同 tick/下一 tick 容易看起来像「只决策不动」
        if not state.current_plan:
            state.decision_cooldown_until = self.sim_time + self.decision_cooldown
        state.replan_reason = None
        if self.verbose:
            print(f"   计划 {len(state.current_plan)} 步 (engine={state.plan_engine})")
            for i, step in enumerate(state.current_plan, 1):
                print(f"     {i}. {step[0]}({', '.join(step[1])})")
        return out

    def _failure_replan(
        self,
        state: AgentState,
        *,
        reason: str,
        instant: bool = False,
    ) -> None:
        agent = state.agent
        state.replan_reason = reason
        state.interrupt_count += 1
        state.is_interrupted = True
        self._cancel_pending(state, apply=False)
        state.current_plan = None
        state.plan_index = 0
        state.plan_error = reason
        state.status = "failed"

        if self._is_dead(state):
            return

        key = _goal_key(state.last_goal)
        if self.verbose:
            print(f"[REPLAN] [{agent.name}] 失败重规划: {reason}")

        # 1) 同目标再规划一次
        if state.last_goal:
            retry = assess_goal(
                self.world,
                agent.name,
                state.last_goal,
                engines=self.plan_engines,
            )
            if retry.ok and not retry.already_satisfied and retry.plan:
                state.current_plan = list(retry.plan)
                state.plan_index = 0
                state.plan_engine = retry.engine
                state.plan_error = None
                state.is_interrupted = False
                state.status = "idle"
                state.last_goal = retry.goal_data
                if self.verbose:
                    print(f"   同目标重规划成功: {len(state.current_plan)} 步")
                return
            if key and key not in state.tried_goal_keys:
                state.tried_goal_keys.append(key)

        # 2) 换次优候选 / 重新决策
        state.decision_cooldown_until = 0.0  # 允许立即决策
        decided = self._decide_and_plan(state, instant=instant)
        if state.current_plan:
            state.is_interrupted = False
            if self.verbose:
                print(f"   换目标重规划成功 (fallback={decided.get('fallback')})")
            return

        # 3) wait + backoff
        wp = wait_plan(agent.name)
        state.current_plan = list(wp.plan)
        state.plan_index = 0
        state.plan_engine = wp.engine
        state.is_interrupted = False
        state.status = "idle"
        state.replan_backoff_until = self.sim_time + self.replan_backoff
        if self.verbose:
            print(f"   重规划失败，等待 backoff={self.replan_backoff}s")

    def _execute_plan_step(
        self,
        agent: AgentProfile,
        action_name: str,
        args: List[str],
    ) -> ActionResult:
        if action_name == "noop":
            return ActionResult(
                success=True,
                message="noop",
                agent_name=agent.name,
                action_type="noop",
            )
        from registry import get_registry

        registry = get_registry()
        action = registry.get(action_name)
        if action is None:
            return ActionResult(
                success=False,
                message=f"未知动作: {action_name}",
                agent_name=agent.name,
                action_type=action_name,
            )
        kwargs = {p.name: v for p, v in zip(action.parameters, args)}
        if action_name == "interact":
            try:
                import item_interact as _ii

                _ii.FORCE_MOCK = bool(self.force_mock)
            except Exception:
                pass
        ok, reason = registry.execute(self.world, action_name, **kwargs)
        target = args[-1] if args else None
        metadata: Dict[str, Any] = {}
        if ok and action_name == "talk" and len(args) >= 3:
            try:
                from dialogue_service import handle_talk_dialogue

                dialogue = handle_talk_dialogue(
                    self.world,
                    args[0],
                    args[1],
                    args[2],
                    memory=self.memory,
                    force_mock=self.force_mock,
                )
                metadata["dialogue"] = dialogue
            except Exception:
                pass
        if ok and action_name == "interact":
            try:
                from item_interact import take_last_interact_result

                inter = take_last_interact_result()
                if inter:
                    metadata["interaction"] = inter
                    if inter.get("narrative"):
                        reason = inter["narrative"]
            except Exception:
                pass
        return ActionResult(
            success=ok,
            message=reason,
            agent_name=agent.name,
            action_type=action_name,
            target=target,
            metadata=metadata,
        )

    def _remember_action(
        self,
        agent: AgentProfile,
        action_name: str,
        args: List[str],
        result: ActionResult,
    ) -> None:
        if self.memory is None:
            return
        text = f"{action_name}({', '.join(args)}) -> {result.message}"
        self.memory.add(
            agent.name,
            text,
            type="action",
            location=agent.location or "",
            success=result.success,
            result=result.message,
        )
