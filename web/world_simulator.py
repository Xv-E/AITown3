"""
Web world simulator — bridges GameLoop with UI plan state (continuous clock).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from agent_profile import AgentProfile
from game_loop import AgentState, GameLoop, PlanStep

PlanStep = Tuple[str, List[str]]


@dataclass
class AgentUIOverlay:
    decision_mode: Optional[str] = None
    decision_process: Optional[Dict[str, Any]] = None
    candidate_goals: List[Dict[str, Any]] = field(default_factory=list)
    decision_steps: List[Dict[str, Any]] = field(default_factory=list)
    inject_error: Optional[str] = None


def _derive_status(state: AgentState, overlay: AgentUIOverlay) -> str:
    if state.status in ("acting", "deciding", "planning", "failed", "idle"):
        if state.pending_action:
            return "acting"
        if overlay.inject_error or state.plan_error:
            if state.status == "failed" or overlay.inject_error:
                return "failed"
        if state.status != "idle":
            return state.status
    if overlay.inject_error or state.plan_error:
        return "failed"
    if state.last_action_result and not state.last_action_result.success:
        return "failed"
    if state.pending_action:
        return "acting"
    if state.current_plan and state.plan_index < len(state.current_plan):
        return "ready"
    if state.current_plan and state.plan_index >= len(state.current_plan):
        return "completed"
    if state.last_goal and not state.current_plan:
        if state.last_action_result and state.last_action_result.success:
            return "completed"
        if state.plan_error:
            return "failed"
        if state.last_action_result is None and overlay.inject_error is None:
            return "ready" if state.last_goal else "idle"
        return "completed"
    return "idle"


def agent_state_to_dict(state: AgentState, overlay: AgentUIOverlay, *, sim_time: float = 0.0) -> Dict[str, Any]:
    plan = state.current_plan or []
    status = _derive_status(state, overlay)
    steps = []
    for i, (action, args) in enumerate(plan):
        if i < state.plan_index:
            st = "done"
        elif i == state.plan_index and status in ("ready", "executing", "acting"):
            st = "current"
        else:
            st = "pending"
        steps.append({
            "index": i + 1,
            "action": action,
            "args": args,
            "label": f"{action}({', '.join(args)})",
            "status": st,
        })

    last_action = None
    if state.last_action_result:
        ar = state.last_action_result
        last_action = {
            "action": ar.action_type,
            "args": [],
            "success": ar.success,
            "message": ar.message,
            "step": state.plan_index,
        }
        if ar.metadata.get("dialogue"):
            last_action["dialogue"] = ar.metadata["dialogue"]

    error = overlay.inject_error or state.plan_error
    if state.last_action_result and not state.last_action_result.success:
        error = state.last_action_result.message

    pending = state.pending_action.to_dict() if state.pending_action else None
    busy_remaining = max(0.0, state.sim_busy_until - sim_time) if state.sim_busy_until else 0.0

    # Prefer live GameLoop state; overlay supplements API decisions only
    decision_mode = state.decision_mode or overlay.decision_mode
    decision_process = state.decision_process or overlay.decision_process
    candidate_goals = state.candidate_goals or overlay.candidate_goals
    decision_steps = state.decision_steps or overlay.decision_steps

    return {
        "character": state.agent.name,
        "goal": state.last_goal,
        "plan": steps,
        "plan_index": state.plan_index,
        "plan_total": len(plan),
        "plan_remaining": max(0, len(plan) - state.plan_index),
        "status": status,
        "last_action": last_action,
        "error": error,
        "engine": state.plan_engine,
        "decision_mode": decision_mode,
        "decision_process": decision_process,
        "candidate_goals": candidate_goals,
        "decision_steps": decision_steps,
        "pending_action": pending,
        "busy_remaining": busy_remaining,
        "sim_busy_until": state.sim_busy_until,
        "replan_reason": state.replan_reason,
        "mission_complete": bool(state.mission_complete),
        "mission_complete_reason": state.mission_complete_reason or "",
        "location": state.agent.location,
        "hp": state.agent.hp,
    }


class WorldSimulator:
    """Singleton holding GameLoop and UI overlay state."""

    def __init__(self) -> None:
        self._game_loop: Optional[GameLoop] = None
        self._overlays: Dict[str, AgentUIOverlay] = {}
        self.turn_count = 0
        self.tick_count = 0
        self.sim_time = 0.0
        self._last_result: Optional[Dict[str, Any]] = None

    def reset(self) -> None:
        self._game_loop = None
        self._overlays.clear()
        self.turn_count = 0
        self.tick_count = 0
        self.sim_time = 0.0
        self._last_result = None

    def dedupe_agents(self) -> None:
        """Remove duplicate-named agents from GameLoop (keep the first)."""
        loop = self._game_loop
        if loop is None:
            return
        seen: set = set()
        unique = []
        for state in loop.agents:
            name = state.agent.name
            if name in seen:
                continue
            seen.add(name)
            unique.append(state)
        loop.agents = unique

    def _overlay(self, name: str) -> AgentUIOverlay:
        if name not in self._overlays:
            self._overlays[name] = AgentUIOverlay()
        return self._overlays[name]

    def _ensure_loop(self, world) -> GameLoop:
        if self._game_loop is None:
            memory = None
            try:
                from llm_service import get_web_memory

                memory = get_web_memory()
            except Exception:
                pass
            self._game_loop = GameLoop(
                world,
                memory=memory,
                verbose=False,
                plan_engines=["unified_planning", "bfs"],
            )
        else:
            self._game_loop.world = world
        return self._game_loop

    def sync_agents(self, world, *, force_mock: bool = False) -> None:
        loop = self._ensure_loop(world)
        loop.force_mock = force_mock
        seen: set = set()
        for name in world.list_entities("Person") or []:
            if not name or name in seen:
                continue
            seen.add(name)
            state = loop.get_agent_state(name)
            if state is not None:
                state.agent.refresh_from_world()
                decider = loop.decider_factory(
                    world, memory=loop.memory, force_mock=force_mock
                )
                state.agent._decider = decider  # noqa: SLF001
                continue
            agent = AgentProfile.from_world(world, name)
            loop.add_agent(agent, force_mock=force_mock)

    def inject_plan(
        self,
        name: str,
        goal_data: Dict[str, Any],
        plan: List[PlanStep],
        *,
        engine: str = "",
        error: Optional[str] = None,
        world=None,
    ) -> None:
        if world is not None:
            self.sync_agents(world)
        loop = self._ensure_loop(world) if world is not None else self._game_loop
        if loop is None:
            return
        state = loop.get_agent_state(name)
        if state is None and world is not None:
            self.sync_agents(world)
            state = loop.get_agent_state(name)
        if state is None:
            return
        overlay = self._overlay(name)
        overlay.inject_error = error
        # After injecting a plan, clear stale candidates so UI shows steps, not only LLM candidates
        if plan and not error:
            overlay.candidate_goals = list(state.candidate_goals or overlay.candidate_goals)
            overlay.inject_error = None
        state.last_goal = goal_data
        state.current_plan = list(plan) if plan else None
        state.plan_index = 0
        state.plan_engine = engine
        state.plan_error = error
        state.is_interrupted = False
        state.last_action_result = None
        state.pending_action = None
        state.sim_busy_until = 0.0
        state.replan_reason = error
        state.status = "failed" if error else ("ready" if plan else "idle")

    def clear_plan(self, name: str) -> None:
        loop = self._game_loop
        if loop is None:
            self._overlays.pop(name, None)
            return
        state = loop.get_agent_state(name)
        if state:
            state.last_goal = None
            state.current_plan = None
            state.plan_index = 0
            state.plan_engine = ""
            state.plan_error = None
            state.last_action_result = None
            state.is_interrupted = False
            state.pending_action = None
            state.sim_busy_until = 0.0
            state.status = "idle"
            state.replan_reason = None
        self._overlays[name] = AgentUIOverlay()

    def set_decision_overlay(
        self,
        name: str,
        *,
        decision_mode: Optional[str] = None,
        decision_process: Optional[Dict[str, Any]] = None,
        candidate_goals: Optional[List[Dict[str, Any]]] = None,
        decision_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        overlay = self._overlay(name)
        if decision_mode is not None:
            overlay.decision_mode = decision_mode
        if decision_process is not None:
            overlay.decision_process = decision_process
        if candidate_goals is not None:
            overlay.candidate_goals = candidate_goals
        if decision_steps is not None:
            overlay.decision_steps = decision_steps

    def get_plan_dict(self, world, name: str) -> Dict[str, Any]:
        self.sync_agents(world)
        loop = self._game_loop
        sim_time = loop.sim_time if loop else 0.0
        if loop is None:
            return agent_state_to_dict(
                AgentState(agent=AgentProfile(world, name)),
                self._overlay(name),
                sim_time=sim_time,
            )
        state = loop.get_agent_state(name)
        if state is None:
            return agent_state_to_dict(
                AgentState(agent=AgentProfile(world, name)),
                self._overlay(name),
                sim_time=sim_time,
            )
        return agent_state_to_dict(state, self._overlay(name), sim_time=sim_time)

    def list_plan_dicts(self, world) -> List[Dict[str, Any]]:
        self.sync_agents(world)
        self.dedupe_agents()
        loop = self._game_loop
        if loop is None:
            return []
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for s in loop.agents:
            name = s.agent.name
            if name in seen:
                continue
            seen.add(name)
            out.append(agent_state_to_dict(s, self._overlay(name), sim_time=loop.sim_time))
        return out

    def npc_activity_summary(self, world) -> List[Dict[str, Any]]:
        """Character activity summary for the play page (includes auto-planning players)."""
        plans = self.list_plan_dicts(world)
        out = []
        for p in plans:
            name = p.get("character")
            if not name:
                continue
            goal = p.get("goal") or {}
            pending = p.get("pending_action")
            last = p.get("last_action") or {}
            pending_label = None
            if isinstance(pending, dict) and pending.get("name"):
                args = pending.get("args") or []
                pending_label = f"{pending['name']}({', '.join(str(a) for a in args)})"
            out.append({
                "name": name,
                "status": p.get("status"),
                "location": p.get("location"),
                "hp": p.get("hp"),
                "goal": goal.get("goal") if isinstance(goal, dict) else None,
                "target": goal.get("target_entity") if isinstance(goal, dict) else None,
                "reason": (
                    p.get("mission_complete_reason")
                    or (goal.get("reason") if isinstance(goal, dict) else None)
                ),
                "pending_action": pending.get("name") if isinstance(pending, dict) else None,
                "pending_label": pending_label,
                "busy_remaining": p.get("busy_remaining", 0),
                "plan_remaining": p.get("plan_remaining", 0),
                "replan_reason": p.get("replan_reason"),
                "last_action": last.get("action"),
                "last_message": last.get("message"),
                "decision_mode": p.get("decision_mode"),
                "mission_complete": bool(p.get("mission_complete")),
            })
        return out

    def _tick_result_to_agent_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        name = entry.get("agent") or entry.get("character", "")
        loop = self._game_loop
        state_dict = {}
        if loop:
            state = loop.get_agent_state(name)
            if state:
                state_dict = agent_state_to_dict(
                    state, self._overlay(name), sim_time=loop.sim_time
                )
                # Sync overlay from AgentState
                if state.decision_mode:
                    self.set_decision_overlay(
                        name,
                        decision_mode=state.decision_mode,
                        decision_process=state.decision_process,
                        candidate_goals=state.candidate_goals,
                        decision_steps=state.decision_steps,
                    )
                    state_dict = agent_state_to_dict(
                        state, self._overlay(name), sim_time=loop.sim_time
                    )
        return {
            "character": name,
            "skipped": entry.get("skipped", False),
            "reason": entry.get("reason"),
            "success": entry.get("success"),
            "message": entry.get("message"),
            "action": entry.get("action"),
            "dialogue": entry.get("dialogue"),
            "state": state_dict,
        }

    def tick(
        self,
        world,
        dt: float = 0.25,
        *,
        characters: Optional[List[str]] = None,
        auto_plan: bool = True,
        use_llm: bool = False,
        force_mock: bool = False,
        instant: bool = False,
        include_player: bool = False,
    ) -> Dict[str, Any]:
        use_mock = force_mock or not use_llm
        self.sync_agents(world, force_mock=use_mock)
        loop = self._ensure_loop(world)
        loop.force_mock = use_mock

        tick_result = loop.tick(
            dt,
            auto_plan=auto_plan,
            characters=characters,
            instant=instant,
            include_player=include_player,
        )
        self.tick_count = tick_result.get("tick", loop.tick_count)
        self.turn_count = self.tick_count
        self.sim_time = tick_result.get("sim_time", loop.sim_time)
        agents = [self._tick_result_to_agent_entry(a) for a in tick_result.get("agents", [])]
        missions_done = bool(
            auto_plan
            and loop.all_auto_agents_mission_complete(
                include_player=include_player,
                characters=characters,
            )
        )
        result = {
            "turn": self.tick_count,
            "tick": self.tick_count,
            "sim_time": self.sim_time,
            "agents": agents,
            "plans": self.list_plan_dicts(world),
            "npc_activity": self.npc_activity_summary(world),
            "missions_complete": missions_done,
        }
        self._last_result = result
        return result

    def run_turn(
        self,
        world,
        *,
        characters: Optional[List[str]] = None,
        auto_plan: bool = False,
        use_llm: bool = False,
        force_mock: bool = False,
        include_player: bool = False,
    ) -> Dict[str, Any]:
        """Legacy API compatibility: instant tick."""
        return self.tick(
            world,
            dt=1.0,
            characters=characters,
            auto_plan=auto_plan,
            use_llm=use_llm,
            force_mock=force_mock,
            instant=True,
            include_player=include_player,
        )

    def _auto_decide_llm(self, world, name: str, *, force_mock: bool = False) -> None:
        """Manual decide API remains available; simulation path uses GameLoop internal decisions."""
        from llm_service import run_llm_decision
        from character_plans import _sanitize_goal_target
        from goal_feasibility import assess_goal

        state = self._game_loop.get_agent_state(name) if self._game_loop else None
        last_goal = state.last_goal if state else None
        last_action = None
        if state and state.last_action_result:
            last_action = {
                "message": state.last_action_result.message,
                "success": state.last_action_result.success,
            }
        decision = run_llm_decision(
            world,
            name,
            force_mock=force_mock,
            last_goal=last_goal,
            last_action=last_action,
        )
        self.set_decision_overlay(
            name,
            decision_mode=decision.get("mode"),
            decision_process=decision.get("decision_process"),
            candidate_goals=decision.get("candidate_goals") or [],
            decision_steps=decision.get("steps") or [],
        )
        selected = decision.get("selected_goal")
        if not selected or not selected.get("goal"):
            self.inject_plan(name, {}, [], error="LLM did not produce a valid goal", world=world)
            return

        goal_data = dict(selected)
        goal_data["target_entity"] = _sanitize_goal_target(
            name,
            goal_data.get("goal", ""),
            goal_data.get("target_entity"),
        )
        assessed = assess_goal(world, name, goal_data, engines=["unified_planning", "bfs"])
        if not assessed.ok:
            self.inject_plan(
                name,
                assessed.goal_data,
                [],
                engine=assessed.engine,
                error=assessed.reason or "Planning failed",
                world=world,
            )
            return
        self.inject_plan(
            name,
            assessed.goal_data,
            list(assessed.plan or []),
            engine=assessed.engine,
            world=world,
        )

    def _auto_decide_mock(self, world, name: str) -> None:
        from profile_planner import profile_aware_pick_goal
        from goal_feasibility import assess_goal

        state = self._game_loop.get_agent_state(name) if self._game_loop else None
        agent = state.agent if state else AgentProfile.from_world(world, name)
        agent.refresh_from_world()
        selected = profile_aware_pick_goal(world, agent)
        self.set_decision_overlay(
            name,
            decision_mode="profile",
            decision_process={
                "settings": agent.get_settings_summary(),
                "planner": "profile_aware",
            },
            candidate_goals=[selected],
            decision_steps=[],
        )
        goal_data = {
            "goal": selected["goal"],
            "target_entity": selected.get("target_entity"),
            "reason": selected.get("reason", "Rule-based decision"),
            "priority": selected.get("priority", 0.5),
            "confidence": selected.get("confidence", 0.5),
        }
        assessed = assess_goal(world, name, goal_data, engines=["unified_planning", "bfs"])
        if not assessed.ok:
            self.inject_plan(
                name,
                assessed.goal_data,
                [],
                engine=assessed.engine,
                error=assessed.reason or "Planning failed",
                world=world,
            )
            return
        self.inject_plan(
            name,
            assessed.goal_data,
            list(assessed.plan or []),
            engine=assessed.engine,
            world=world,
        )


_simulator: Optional[WorldSimulator] = None


def get_world_simulator() -> WorldSimulator:
    global _simulator
    if _simulator is None:
        _simulator = WorldSimulator()
    return _simulator
