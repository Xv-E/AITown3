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
from registry import act

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

PlanStep = Tuple[str, List[str]]

HARD_INTERRUPT_HP = 20
DECISION_COOLDOWN = 5.0
REPLAN_BACKOFF = 3.0
# LLM 模式下闲置多久（游戏小时）没有任何触发，就主动想一次
IDLE_REDECIDE_HOURS = 2.0


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
    # once 槽位已完成："<day>:<slot key>"
    routine_done: List[str] = field(default_factory=list)
    # 持续状态（内容包 states/，如 asleep）：名字 / 进入时的日程段 / 进入时刻
    held_state: Optional[str] = None
    state_slot: Optional[str] = None
    state_since: float = 0.0
    # 决策触发器（日程换段 / 突发事件）
    observation: Optional[Any] = None
    pending_triggers: List[Any] = field(default_factory=list)
    last_triggers: List[Dict[str, Any]] = field(default_factory=list)
    next_scan_at: float = 0.0
    last_decision_at: float = -1e9
    # 上次观察（knowledge.observe）时所在地点；换地点时才更新「知道的地点」
    seen_loc: Optional[str] = None
    # 行为日志：决策 / 动作开始与结果 / 打断（最近 HISTORY_LIMIT 条）
    history: List[Dict[str, Any]] = field(default_factory=list)


HISTORY_LIMIT = 120


def _goal_text(goal_data: Optional[Dict[str, Any]]) -> str:
    if not goal_data:
        return "(no goal)"
    goal = goal_data.get("goal") or goal_data.get("final_goal") or "?"
    target = goal_data.get("target_entity")
    ia = goal_data.get("interact_action") or (goal_data.get("metadata") or {}).get("interact_action")
    text = f"{goal} -> {target}" if target else str(goal)
    return f"{text} [{ia}]" if ia else text


def _step_text(step: Any) -> str:
    try:
        name, args = step
        return f"{name}({', '.join(str(a) for a in args)})"
    except (TypeError, ValueError):
        return str(step)


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
        # 世界大事记：物品脚本 rule 的 announce（建成、解锁……）
        self.chronicle: List[Dict[str, Any]] = []

    def _record_announcements(self, world_report: Dict[str, Any]) -> None:
        from routine import format_clock
        from triggers import Trigger

        for ev in world_report.get("events") or []:
            if not isinstance(ev, dict) or ev.get("type") != "announce" or not ev.get("text"):
                continue
            clock = format_clock(self.sim_time)
            self.chronicle.append({"t": round(self.sim_time, 2), "clock": clock, "text": ev["text"]})
            del self.chronicle[:-HISTORY_LIMIT]
            if self.verbose:
                print(f"[NEWS] {ev['text']}")
            for state in self.agents:
                self._log(state, "news", ev["text"])
                self._push_triggers(state, [Trigger(
                    "event", "news", f"{clock}: {ev['text']}", 0.5, self.sim_time,
                )])

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
            print(f"[+] Added agent: {agent.name}")

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
        self._record_announcements(world_report)
        if self.verbose:
            print(f"\n{'=' * 60}")
            clock = world_report.get("clock") or f"t={self.sim_time:.2f}"
            print(f"[TICK] #{self.tick_count} {clock} dt={dt}")
            alerts = world_report.get("alerts") or []
            if alerts:
                a0 = alerts[0]
                print(f"   script alert: [{a0.get('signal')}] {a0.get('item')}: {a0.get('message')}")
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

        if auto_plan:
            for state in targets:
                if not self._may_auto_plan(state, include_player=include_player):
                    continue
                try:
                    self._scan_triggers(state, world_report.get("alerts") or [])
                except Exception as e:
                    if self.verbose:
                        print(f"[WARN] trigger scan {state.agent.name}: {e}")

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
                    print(f"[ERR] Agent {state.agent.name} tick failed: {e}")
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
                in_plan = bool(state.current_plan and state.plan_index < len(state.current_plan))
                # 第一轮里下一步预检失败（status=failed）的计划也要在这一轮重规划
                if in_plan and not self._wants_interrupt(state) and state.status != "failed":
                    continue
                if not in_plan and not self._wants_decision(state):
                    continue
                if not self._may_auto_plan(state, include_player=include_player):
                    continue
                urgent = self._wants_interrupt(state) or in_plan
                if self.sim_time < state.replan_backoff_until and not urgent:
                    continue
                if (
                    self.sim_time < state.decision_cooldown_until
                    and not state.is_interrupted
                    and not urgent
                ):
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
                        print(f"[ERR] Agent {state.agent.name} decision tick failed: {e}")
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
                    print(f"\n[END] Reached max tick count {limit}, stopping")
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

        if not state.pending_action and self._state_gate(state):
            state.status = state.held_state or "idle"
            result["skipped"] = True
            result["reason"] = f"state:{state.held_state}"
            result["status"] = state.status
            return result

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
                self._failure_replan(state, reason=state.replan_reason or "Hard interrupt", instant=instant)
                result["goal"] = state.last_goal
            result["status"] = state.status
            return result

        # 软打断（规则模式）：日程换段 / 同地遇见他人 → 改计划（不取消进行中动作）
        # LLM 模式下这些都是触发器，交给 LLM 决策
        if (
            can_plan
            and self.force_mock
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

        # 有剩余计划：步骤间隙若有紧急事件，先让 LLM 决定要不要改主意
        if (
            state.current_plan
            and state.plan_index < len(state.current_plan)
            and self._wants_interrupt(state)
        ):
            if not can_plan:
                state.status = "deciding"
                result["skipped"] = True
                result["reason"] = "awaiting_decision"
                result["status"] = state.status
                return result
            decided = self._decide_and_plan(state, instant=instant)
            result["decided"] = True
            result["interrupted"] = not decided.get("kept")
            result["goal"] = state.last_goal

        # 有剩余计划：启动下一步
        if state.current_plan and state.plan_index < len(state.current_plan):
            started = self._start_next_step(state, instant=instant)
            if started is None:
                # 预检失败 → 重规划
                if can_plan:
                    self._failure_replan(
                        state,
                        reason=state.replan_reason or "Next step precheck failed",
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

        if not self._wants_decision(state):
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "awaiting_trigger"
            result["status"] = state.status
            return result

        urgent = self._wants_interrupt(state)
        if self.sim_time < state.replan_backoff_until and not urgent:
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "backoff"
            result["status"] = state.status
            return result

        if self.sim_time < state.decision_cooldown_until and not state.is_interrupted and not urgent:
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
            state.replan_reason = "Character is dead"
            return True
        if agent.hp < HARD_INTERRUPT_HP and (state.pending_action or state.current_plan):
            # 进行中才硬打断
            if state.pending_action:
                self._cancel_pending(state, apply=False)
            state.current_plan = None
            state.plan_index = 0
            state.is_interrupted = True
            state.interrupt_count += 1
            state.replan_reason = "HP too low"
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
        if any(str(a).startswith("kind:") for a in args):
            from item_chain import resolve_step_args

            args = resolve_step_args(self.world, state.agent.name, list(args))
            state.current_plan[state.plan_index] = (action_name, args)
        ok, reason = self._check_plan_step(state.agent, action_name, args)
        if not ok:
            state.replan_reason = reason
            state.plan_error = reason
            state.status = "failed"
            if self.verbose:
                print(f"[WARN] [{state.agent.name}] precheck failed: {reason}")
            return None

        duration = get_action_duration(
            action_name, instant=instant, args=list(args), world=self.world
        )
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
        if action_name != act("idle"):
            self._log(
                state,
                "start",
                _step_text((action_name, args)),
                goal=_goal_text(state.last_goal) if state.last_goal else None,
                step=f"{state.plan_index + 1}/{len(state.current_plan)}",
            )
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
                f"[EXEC] [{agent.name}] completed {state.plan_index + 1}/"
                f"{len(state.current_plan or [])}: {action_name}({', '.join(args)})"
            )
        inventory_before = None
        if state.observation is not None:
            from triggers import held_items

            inventory_before = held_items(self.world, agent.name)
        try:
            action_result = self._execute_plan_step(agent, action_name, args)
        except Exception as e:
            action_result = ActionResult(
                success=False,
                message=f"Execution error: {e}",
                agent_name=agent.name,
                action_type=action_name,
            )
        finally:
            # 无论 apply 是否抛错，都必须清掉 pending，否则会永远卡在「执行 move」
            state.pending_action = None
            state.sim_busy_until = 0.0
        try:
            self._absorb_own_changes(state, inventory_before)
        except Exception:
            pass
        self._observe(state, places=True)

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
        dialogue = action_result.metadata.get("dialogue")
        lines = dialogue.get("lines") if isinstance(dialogue, dict) else None
        if action_name != act("idle") or not action_result.success:
            self._log(
                state,
                "done" if action_result.success else "failed",
                _step_text((action_name, args)),
                message=action_result.message,
                dialogue=[
                    f"{ln.get('speaker')}: {ln.get('text')}"
                    for ln in lines or []
                    if isinstance(ln, dict) and ln.get("text")
                ],
            )

        if action_name == act("converse") and action_result.success and len(args) >= 2:
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
                print(f"[DONE] [{agent.name}] plan complete")
            self._mark_routine_done(state)
            agent.complete_current_goal()
            last_actions = {a for a, _ in state.current_plan}
            if self.trigger_driven and last_actions != {"wait"}:
                from triggers import Trigger

                goal = state.last_goal or {}
                self._push_triggers(state, [Trigger(
                    "event", "plan_done",
                    f"You finished: {goal.get('goal')} -> {goal.get('target_entity')}.",
                    0.4, self.sim_time,
                )])
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

            note = sync_quest_progress(self.world, agent.name, memory=self.memory)
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
            return False, f"Unknown action: {action_name}"
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
            load_person_routine,
            pick_social_target,
            routine_goal_candidate,
            should_social_interrupt,
            social_interrupt_goal,
        )
        from world_systems import urgent_alert_goal

        agent = state.agent

        # 0) 物品脚本告警（缺货 / 有订单 / 燃料不足…）；是否分内事由 alert.audience 决定
        fire_goal = urgent_alert_goal(self.world, agent.name, sim_time=self.sim_time)
        if fire_goal:
            same = (
                state.last_goal
                and state.last_goal.get("goal") == fire_goal.get("goal")
                and state.last_goal.get("target_entity") == fire_goal.get("target_entity")
                and state.current_plan
            )
            if not same:
                assessed = assess_goal(
                    self.world, agent.name, fire_goal, engines=self.plan_engines
                )
                if assessed.ok and not assessed.already_satisfied and assessed.plan:
                    if self.verbose:
                        print(f"[SCRIPT] [{agent.name}] {fire_goal.get('reason')}")
                    self._adopt_planned_goal(
                        state, assessed, reason=fire_goal.get("reason") or "Item script alert"
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
                    print(f"[MEET] [{agent.name}] met {social_target}; switching to talk")
                self._adopt_planned_goal(state, assessed, reason=goal.get("reason") or "Met someone to talk to")
                state.last_social_at = self.sim_time
                return True

        # 2) 日程时段切换：离开旧计划，跟随新槽位
        if not slot_changed:
            return False
        if not state.current_plan and not state.last_goal:
            return False
        if self._routine_slot_done(state, slot):
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
                    "reason": f"Routine slot change: go to {slot.location}",
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
                f"[ROUTINE] [{agent.name}] routine slot change → "
                f"{cand.get('goal')}->{cand.get('target_entity')} ({slot.activity})"
            )
        self._adopt_planned_goal(
            state,
            assessed,
            reason=cand.get("reason") or "Routine slot change",
        )
        return True

    # ----- 决策触发器 -----

    @property
    def trigger_driven(self) -> bool:
        """LLM 模式：只在触发器出现时决策；规则模式沿用每拍规则判断。"""
        return not self.force_mock

    def _wants_decision(self, state: AgentState) -> bool:
        if not self.trigger_driven:
            return True
        return bool(state.pending_triggers)

    def _wants_interrupt(self, state: AgentState) -> bool:
        if not self.trigger_driven:
            return False
        from triggers import INTERRUPT_PRIORITY

        return any(t.priority >= INTERRUPT_PRIORITY for t in state.pending_triggers)

    def _push_triggers(self, state: AgentState, new: List[Any]) -> None:
        from triggers import MAX_QUEUE

        if not new:
            return
        queue = {t.key(): t for t in state.pending_triggers}
        for t in new:
            queue[t.key()] = t
            if self.verbose:
                print(f"[TRIGGER] [{state.agent.name}] ({t.source}/{t.kind}) {t.text}")
        state.pending_triggers = sorted(queue.values(), key=lambda t: (-t.priority, -t.at))[:MAX_QUEUE]

    def _scan_triggers(self, state: AgentState, alerts: List[Dict[str, Any]]) -> None:
        from routine import DAY_LENGTH_SECONDS, format_clock
        from triggers import SCAN_INTERVAL, Trigger, diff_triggers, observe

        if self.sim_time < state.next_scan_at:
            return
        state.next_scan_at = self.sim_time + SCAN_INTERVAL
        agent = state.agent
        agent.refresh_from_world()
        self._observe(state, agent.location)
        clock = format_clock(self.sim_time)
        obs, mine, slot = observe(
            self.world,
            agent.name,
            location=agent.location,
            hp=agent.hp,
            sim_time=self.sim_time,
            alerts=alerts,
        )
        new = diff_triggers(
            state.observation, obs, alerts=mine, slot=slot, clock=clock, sim_time=self.sim_time
        )
        held = self._held_state_def(state)
        if held is not None and state.observation is not None:
            from dataclasses import replace

            new = [t for t in new if t.priority >= held.wake_priority]
            # 状态中错过的换段 / 信号，结束后再触发
            obs = replace(obs, slot=state.observation.slot, alerts=state.observation.alerts)
        state.observation = obs
        idle_for = self.sim_time - state.last_decision_at
        if (
            self.trigger_driven
            and not new
            and not state.pending_triggers
            and not state.current_plan
            and not state.pending_action
            and held is None
            and idle_for > IDLE_REDECIDE_HOURS * DAY_LENGTH_SECONDS / 24.0
        ):
            new = [Trigger("event", "idle", f"{clock}: nothing has happened for a while.",
                           0.3, self.sim_time)]
        self._push_triggers(state, new)

    def _observe(self, state: AgentState, location: Optional[str] = None, *, places: bool = False) -> None:
        """更新人物对当前地点的信念；有变化时写一条 observation 记忆。

        places：顺带刷新「知道的地点」（换了地点，或刚做完动作，可能打开了新通路）。
        """
        from knowledge import current_location, describe_sighting, knowledge_mode, observe

        if knowledge_mode() == "omniscient":
            return
        name = state.agent.name
        try:
            loc = location or current_location(self.world, name)
            new, gone, kinds = observe(
                self.world, name, loc, t=self.sim_time, places=places or loc != state.seen_loc
            )
            state.seen_loc = loc
        except Exception as e:
            if self.verbose:
                print(f"[KNOW] [{name}] observe failed: {e}")
            return
        if (new or gone) and self.memory:
            self.memory.add(
                name,
                describe_sighting(loc, new, gone, kinds),
                type="observation",
                location=loc or "",
            )

    def _absorb_own_changes(self, state: AgentState, before: Optional[frozenset]) -> None:
        """自己动作造成的背包变化不算「收到」事件；别人给的仍会被下次扫描发现。"""
        if state.observation is None or before is None:
            return
        from dataclasses import replace

        from triggers import held_items

        after = held_items(self.world, state.agent.name)
        inv = (state.observation.inventory - (before - after)) | (after - before)
        state.observation = replace(state.observation, inventory=frozenset(inv))

    def _has_routine(self, state: AgentState) -> bool:
        from routine import load_person_routine

        return bool(load_person_routine(self.world, state.agent.name))

    def _routine_done_key(self, slot_key: str) -> str:
        from routine import day_index

        return f"{day_index(self.sim_time)}:{slot_key}"

    def _routine_slot_done(self, state: AgentState, slot) -> bool:
        return bool(slot.once) and self._routine_done_key(slot.key) in state.routine_done

    def _mark_routine_done(self, state: AgentState) -> None:
        goal = state.last_goal or {}
        key = goal.get("routine_key")
        if goal.get("source") != "routine" or not key:
            return
        done = self._routine_done_key(str(key))
        if done not in state.routine_done:
            state.routine_done = (state.routine_done + [done])[-32:]

    # ----- 持续状态（内容包 states/） -----

    def _current_slot_key(self, state: AgentState) -> Optional[str]:
        from routine import current_slot, load_person_routine

        slots = load_person_routine(self.world, state.agent.name)
        slot = current_slot(slots, self.sim_time) if slots else None
        return slot.key if slot else None

    def _held_state_def(self, state: AgentState):
        from content import get_content

        return get_content().state(state.held_state)

    def _state_gate(self, state: AgentState) -> bool:
        """角色处于持续状态时暂停决策；日程换段 / 足够紧急的触发器会结束状态。返回是否仍被挡住。"""
        from action import get_property
        from content import get_content

        name = get_property(self.world, state.agent.name, "Person", "state")
        sdef = get_content().state(name)
        if sdef is None:
            state.held_state = None
            return False
        if state.held_state != name:
            state.held_state = name
            state.state_slot = self._current_slot_key(state)
            state.state_since = self.sim_time
            state.pending_triggers = []
            if self.verbose:
                print(f"[STATE] [{state.agent.name}] enters '{name}'")
        reason = None
        if sdef.ends_on_routine_change and self._current_slot_key(state) != state.state_slot:
            reason = "the routine moved on"
        else:
            urgent = [t for t in state.pending_triggers if t.priority >= sdef.wake_priority]
            if urgent:
                reason = urgent[0].text
        if reason is None:
            return True
        self._exit_state(state, sdef, reason)
        return False

    def _exit_state(self, state: AgentState, sdef, reason: str) -> None:
        from graph_ops import apply_all
        from routine import format_clock
        from triggers import Trigger

        agent = state.agent
        ctx = {"actor": agent.name}
        apply_all(self.world, [{"op": "set_state", "entity": "@actor", "state": None}] + sdef.on_exit, ctx)
        state.held_state = None
        state.state_slot = None
        state.decision_cooldown_until = 0.0
        state.replan_backoff_until = 0.0
        self._log(state, "state", f"No longer {sdef.name}", reason=reason)
        if self.verbose:
            print(f"[STATE] [{agent.name}] leaves '{sdef.name}': {reason}")
        self._push_triggers(state, [Trigger(
            "routine", "state_end",
            f"{format_clock(self.sim_time)}: you are no longer {sdef.name} ({reason}).",
            0.6, self.sim_time,
        )])

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
        self._log(
            state,
            "decision",
            _goal_text(state.last_goal),
            reason=reason,
            source=state.last_goal.get("source"),
            mode="interrupt",
            plan=self._remaining_plan(state),
        )

    def _routine_candidates(self, state: AgentState) -> List[Dict[str, Any]]:
        from routine import (
            current_slot,
            load_person_routine,
            pick_social_target,
            routine_goal_candidate,
            social_interrupt_goal,
        )
        from world_systems import urgent_alert_goal

        out: List[Dict[str, Any]] = []
        fire_goal = urgent_alert_goal(self.world, state.agent.name, sim_time=self.sim_time)
        if fire_goal:
            out.append(fire_goal)

        slots = load_person_routine(self.world, state.agent.name)
        if not slots:
            return out
        slot = current_slot(slots, self.sim_time)
        if slot is None:
            return out
        state.routine_slot_key = slot.key
        if self._routine_slot_done(state, slot):
            if state.agent.location != slot.location:
                out.append({
                    "goal": "Go to location",
                    "target_entity": slot.location,
                    "reason": f"Routine: stay at {slot.location} ({slot.activity})",
                    "priority": 0.72,
                    "confidence": 0.8,
                    "source": "routine",
                })
            return out
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
                        "reason": f"Routine: go to {slot.location} to meet others",
                        "priority": 0.72,
                        "confidence": 0.8,
                        "source": "routine",
                    }
                )
            return out
        out.append(cand)
        if cand.get("goal") != "Go to location" and state.agent.location != slot.location:
            # 目标暂时做不成时，至少先去日程地点
            out.append({
                "goal": "Go to location",
                "target_entity": slot.location,
                "reason": f"Routine: head to {slot.location} ({slot.activity})",
                "priority": 0.7,
                "confidence": 0.8,
                "source": "routine",
            })
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

    def _log(self, state: AgentState, kind: str, text: str, **detail: Any) -> None:
        from routine import format_clock

        entry: Dict[str, Any] = {
            "t": round(self.sim_time, 2),
            "clock": format_clock(self.sim_time),
            "kind": kind,
            "text": text,
        }
        entry.update({k: v for k, v in detail.items() if v not in (None, "", [], {})})
        state.history.append(entry)
        if len(state.history) > HISTORY_LIMIT:
            del state.history[:-HISTORY_LIMIT]

    def _remaining_plan(self, state: AgentState) -> List[str]:
        if not state.current_plan:
            return []
        return [_step_text(s) for s in state.current_plan[state.plan_index:]]

    def _decide_and_plan(
        self,
        state: AgentState,
        *,
        instant: bool = False,
    ) -> Dict[str, Any]:
        out = self._decide_and_plan_core(state, instant=instant)
        triggers = [t.get("text") for t in state.last_triggers if isinstance(t, dict) and t.get("text")]
        goal = state.last_goal or {}
        if out.get("kept"):
            text, reason = f"Keeps current plan: {_goal_text(goal)}", None
        elif out.get("fallback") == "wait":
            text, reason = "Nothing actionable — waits", goal.get("reason")
        elif out.get("goal"):
            g = out["goal"]
            text = _goal_text(g)
            if not state.current_plan:
                text += " (already satisfied)"
            reason = g.get("reason") or g.get("reasoning")
        else:
            text, reason = "Could not decide", state.plan_error
        self._log(
            state,
            "decision",
            text,
            reason=reason,
            source=(out.get("goal") or {}).get("source"),
            mode=state.decision_mode,
            triggers=triggers,
            plan=self._remaining_plan(state),
        )
        return out

    def _decide_and_plan_core(
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
            print(f"[THINK] [{agent.name}] deciding | location: {agent.location} | HP: {agent.hp}")

        # 供 PromptBuilder 显示正确的一日时段
        agent._sim_time = self.sim_time  # noqa: SLF001

        # 消费触发器：告诉 LLM「为什么现在要做决定」
        from triggers import describe_triggers

        triggers = list(state.pending_triggers)
        state.pending_triggers = []
        state.last_triggers = [t.to_dict() for t in triggers]
        state.last_decision_at = self.sim_time
        agent._trigger_context = describe_triggers(triggers)  # noqa: SLF001
        in_plan = bool(state.current_plan and state.plan_index < len(state.current_plan))
        if self.verbose and triggers:
            print(f"   triggered by: {', '.join(t.kind for t in triggers)}")

        # 清空旧候选，重新生成
        agent.goal_manager.clear_all()
        routine_cands = self._routine_candidates(state)
        ephemeral_cands = self._ephemeral_action_candidates(state)
        has_routine = self._has_routine(state)
        if self.force_mock:
            from profile_planner import profile_aware_pick_goal

            # 有日程的人：日程满足后就待在原地，不随机找事做
            fallback = [] if has_routine else [profile_aware_pick_goal(self.world, agent)]
            candidates = list(routine_cands) + list(ephemeral_cands) + fallback
            state.decision_mode = "mock"
            state.decision_process = {
                "planner": "profile_aware",
                "settings": agent.get_settings_summary(),
                "note": "Rule-based decision: routine + ephemeral interact actions + personality",
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
            llm_cands = [dict(c or {}, source=(c or {}).get("source") or "llm") for c in candidates or []]
            candidates = (
                list(routine_cands)
                + list(ephemeral_cands)
                + llm_cands
            )
            state.decision_mode = "llm"
            state.decision_process = {
                "force_mock": False,
                "settings": agent.get_settings_summary(),
                "note": "Triggered LLM decision; routine / item signals are fallbacks",
                "triggers": state.last_triggers,
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

        picked = None
        if not self.force_mock:
            # LLM 自己的选择优先；与当前目标相同则继续原计划
            for c in candidates:
                if (c or {}).get("source") != "llm":
                    continue
                c0 = dict(c)
                if not c0.get("goal") and c0.get("final_goal"):
                    c0["goal"] = c0.get("final_goal")
                if in_plan and _goal_key(c0) == _goal_key(state.last_goal):
                    state.status = "ready"
                    out.update({"decided": True, "kept": True})
                    if self.verbose:
                        print(f"[GOAL] [{agent.name}] keeps current plan")
                    return out
                assessed = assess_goal(self.world, agent.name, c0, engines=self.plan_engines)
                if _goal_key(assessed.goal_data) in exclude:
                    continue
                if assessed.ok and not assessed.already_satisfied and assessed.plan:
                    picked = assessed
                    break
            if picked is None and in_plan:
                # 被打断但 LLM 没给出更好的可行目标：继续原计划
                state.status = "ready"
                out.update({"decided": True, "kept": True})
                return out

        # 物品脚本告警 / 日程优先（规则模式，或 LLM 目标不可行时的兜底）
        for rc in ([] if picked is not None else routine_cands):
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

        if picked is None and not has_routine:
            # profile 兜底
            try:
                from profile_planner import profile_aware_pick_goal

                fallback = profile_aware_pick_goal(self.world, agent)
                state.decision_mode = "profile"
                state.decision_process = {
                    "planner": "profile_aware",
                    "settings": agent.get_settings_summary(),
                    "note": "Rule fallback after all candidates infeasible or already satisfied",
                    "llm_goal_overridden": llm_goal,
                    "fallback_goal": {
                        "goal": fallback.get("goal"),
                        "target_entity": fallback.get("target_entity"),
                    },
                }
                state.candidate_goals = [fallback]
                if self.verbose and llm_goal:
                    print(
                        f"[GOAL] [{agent.name}] LLM goal overridden: "
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
                "reason": "No actionable goal; waiting",
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
                f"[GOAL] [{agent.name}] selected: {goal_data.get('goal')} -> "
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
                    message=f"Goal already satisfied: {goal_data.get('goal')}",
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
            print(f"   plan {len(state.current_plan)} steps (engine={state.plan_engine})")
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
        self._log(state, "replan", f"Plan broke: {_goal_text(state.last_goal)}", reason=reason)
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
            print(f"[REPLAN] [{agent.name}] failure replan: {reason}")

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
                    print(f"   same-goal replan succeeded: {len(state.current_plan)} steps")
                return
            if key and key not in state.tried_goal_keys:
                state.tried_goal_keys.append(key)

        # 2) 换次优候选 / 重新决策
        state.decision_cooldown_until = 0.0  # 允许立即决策
        if self.trigger_driven:
            from triggers import Trigger

            goal = state.last_goal or {}
            self._push_triggers(state, [Trigger(
                "event", "plan_failed",
                f"Your plan ({goal.get('goal')} -> {goal.get('target_entity')}) failed: {reason}",
                0.8, self.sim_time,
            )])
        decided = self._decide_and_plan(state, instant=instant)
        if state.current_plan:
            state.is_interrupted = False
            if self.verbose:
                print(f"   alternate-goal replan succeeded (fallback={decided.get('fallback')})")
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
            print(f"   replan failed; waiting backoff={self.replan_backoff}s")

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
                message=f"Unknown action: {action_name}",
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
        if ok and action_name == act("converse") and len(args) >= 3:
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
