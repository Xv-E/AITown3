"""
Web character plan system — goal setting, PDDL planning, step-by-step execution.

Execution state is managed centrally by WorldSimulator + GameLoop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from goal_terms import get_all_goal_terms, get_goal_definition
from goal_planner import plan_for_goal, _item_pickability
from world_simulator import get_world_simulator

COLLECT_GOALS = frozenset({
    "Collect item",
    "Accumulate items",
    "Strengthen abilities",
    "Enhance capabilities",
})

PlanStep = Tuple[str, List[str]]

SOCIAL_GOALS = frozenset({"Gain information", "Defeat enemy"})


def _sanitize_goal_target(
    character: str,
    goal: str,
    target_entity: Optional[str],
) -> Optional[str]:
    """Social/combat goals cannot target self."""
    if not target_entity:
        return None
    t = target_entity.strip()
    if goal in SOCIAL_GOALS and t == character:
        return None
    return t or None


def _validate_plan_step(character: str, action_name: str, args: List[str]) -> Optional[str]:
    if action_name == "talk" and len(args) >= 2 and args[0] == args[1]:
        return f"Cannot talk to self: talk({args[0]}, {args[1]})"
    if action_name == "attack" and len(args) >= 2 and args[0] == args[1]:
        return f"Cannot attack self: attack({args[0]}, {args[1]})"
    if action_name == "talk" and len(args) >= 2 and args[0] != character and args[1] != character:
        return f"talk must include character {character}"
    return None


def _validate_full_plan(character: str, plan: List[PlanStep]) -> Optional[str]:
    for action_name, args in plan:
        err = _validate_plan_step(character, action_name, args)
        if err:
            return err
    return None


@dataclass
class CharacterPlanState:
    character: str
    goal: Optional[Dict[str, Any]] = None
    plan: List[PlanStep] = field(default_factory=list)
    plan_index: int = 0
    status: str = "idle"
    last_action: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    engine: str = ""
    decision_mode: Optional[str] = None
    decision_process: Optional[Dict[str, Any]] = None
    candidate_goals: List[Dict[str, Any]] = field(default_factory=list)
    decision_steps: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CharacterPlanState":
        raw_plan = data.get("plan") or []
        plan: List[PlanStep] = []
        for step in raw_plan:
            if isinstance(step, dict):
                plan.append((step.get("action", ""), list(step.get("args") or [])))
            elif isinstance(step, (list, tuple)) and len(step) >= 2:
                plan.append((step[0], list(step[1])))
        return cls(
            character=data.get("character", ""),
            goal=data.get("goal"),
            plan=plan,
            plan_index=int(data.get("plan_index") or 0),
            status=data.get("status") or "idle",
            last_action=data.get("last_action"),
            error=data.get("error"),
            engine=data.get("engine") or "",
            decision_mode=data.get("decision_mode"),
            decision_process=data.get("decision_process"),
            candidate_goals=list(data.get("candidate_goals") or []),
            decision_steps=list(data.get("decision_steps") or []),
        )

    def to_dict(self) -> Dict[str, Any]:
        steps = []
        for i, (action, args) in enumerate(self.plan):
            if i < self.plan_index:
                st = "done"
            elif i == self.plan_index and self.status in ("ready", "executing"):
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
        return {
            "character": self.character,
            "goal": self.goal,
            "plan": steps,
            "plan_index": self.plan_index,
            "plan_total": len(self.plan),
            "plan_remaining": max(0, len(self.plan) - self.plan_index),
            "status": self.status,
            "last_action": self.last_action,
            "error": self.error,
            "engine": self.engine,
            "decision_mode": self.decision_mode,
            "decision_process": self.decision_process,
            "candidate_goals": self.candidate_goals,
            "decision_steps": self.decision_steps,
        }


def _mock_pick_goal(world, character: str) -> Dict[str, Any]:
    """Pick a goal from Neo4j character profile (profile mode)."""
    from agent_profile import AgentProfile
    from profile_planner import profile_aware_pick_goal

    agent = AgentProfile.from_world(world, character)
    return profile_aware_pick_goal(world, agent)


class CharacterPlanManager:
    """Manage plan state for all characters (delegates to WorldSimulator)."""

    def __init__(self) -> None:
        self._sim = get_world_simulator()

    @property
    def turn_count(self) -> int:
        return self._sim.turn_count

    @property
    def tick_count(self) -> int:
        return self._sim.tick_count

    @property
    def sim_time(self) -> float:
        return self._sim.sim_time

    def reset_all(self) -> None:
        self._sim.reset()

    def ensure_character(self, name: str, world=None) -> CharacterPlanState:
        if world is not None:
            return CharacterPlanState.from_dict(self._sim.get_plan_dict(world, name))
        return CharacterPlanState(character=name)

    def get_state(self, name: str, world=None) -> Optional[CharacterPlanState]:
        if world is None:
            return None
        return CharacterPlanState.from_dict(self._sim.get_plan_dict(world, name))

    def list_states(self, world) -> List[Dict[str, Any]]:
        return self._sim.list_plan_dicts(world)

    def clear_plan(self, name: str) -> Dict[str, Any]:
        self._sim.clear_plan(name)
        return CharacterPlanState(character=name).to_dict()

    def set_goal_and_plan(
        self,
        world,
        name: str,
        goal: str,
        target_entity: Optional[str] = None,
        *,
        reason: str = "",
    ) -> Dict[str, Any]:
        goal = (goal or "").strip()
        target_entity = _sanitize_goal_target(name, goal, (target_entity or "").strip() or None)

        goal_data = {
            "goal": goal,
            "target_entity": target_entity,
            "reason": reason or f"Manual goal: {goal}",
            "priority": 0.8,
            "confidence": 0.9,
        }

        if goal in COLLECT_GOALS and target_entity:
            held = world.run_query(
                "MATCH (p:Person {name: $c})-[:HAS_ITEM]->(i:Item {name: $i}) RETURN i.name AS n",
                c=name,
                i=target_entity,
            )
            if held:
                self._sim.inject_plan(
                    name, goal_data, [], engine="rule:collect", world=world
                )
                return self._sim.get_plan_dict(world, name)

            ok, pick_reason, _loc = _item_pickability(world, target_entity)
            if not ok:
                self._sim.inject_plan(
                    name,
                    goal_data,
                    [],
                    engine="rule:collect",
                    error=pick_reason,
                    world=world,
                )
                return self._sim.get_plan_dict(world, name)

        result = plan_for_goal(
            world, name, goal_data, engines=["unified_planning", "bfs"]
        )

        if not result.success:
            self._sim.inject_plan(
                name,
                goal_data,
                [],
                engine=result.engine,
                error=result.error or "Planning failed",
                world=world,
            )
            return self._sim.get_plan_dict(world, name)

        plan = list(result.plan)
        plan_err = _validate_full_plan(name, plan)
        if plan_err:
            self._sim.inject_plan(
                name,
                goal_data,
                [],
                engine=result.engine,
                error=plan_err,
                world=world,
            )
            return self._sim.get_plan_dict(world, name)

        self._sim.inject_plan(name, goal_data, plan, engine=result.engine, world=world)
        return self._sim.get_plan_dict(world, name)

    def auto_plan(
        self,
        world,
        name: str,
        *,
        parent_goal: str = "explore the town and survive",
        use_llm: bool = False,
        force_mock: bool = False,
    ) -> Dict[str, Any]:
        if use_llm:
            return self.llm_decide_and_plan(
                world, name, force_mock=force_mock, parent_goal=parent_goal
            )
        selected = _mock_pick_goal(world, name)
        self._sim.set_decision_overlay(
            name,
            decision_mode="profile",
            decision_process=None,
            candidate_goals=[selected],
            decision_steps=[],
        )
        return self.set_goal_and_plan(
            world,
            name,
            selected["goal"],
            selected.get("target_entity"),
            reason=selected.get("reason", "Rule-based decision"),
        )

    def llm_decide(
        self,
        world,
        name: str,
        *,
        force_mock: bool = False,
        parent_goal: str = "explore the town and survive",
    ) -> Dict[str, Any]:
        from llm_service import run_llm_decision

        cur = self._sim.get_plan_dict(world, name)
        decision = run_llm_decision(
            world,
            name,
            force_mock=force_mock,
            parent_goal=parent_goal,
            last_goal=cur.get("goal"),
            last_action=cur.get("last_action"),
        )
        self._sim.set_decision_overlay(
            name,
            decision_mode=decision.get("mode"),
            decision_process=decision.get("decision_process"),
            candidate_goals=decision.get("candidate_goals") or [],
            decision_steps=decision.get("steps") or [],
        )
        return {**self._sim.get_plan_dict(world, name), "selected_goal": decision.get("selected_goal")}

    def llm_decide_and_plan(
        self,
        world,
        name: str,
        *,
        force_mock: bool = False,
        parent_goal: str = "explore the town and survive",
    ) -> Dict[str, Any]:
        from llm_service import run_llm_decision

        cur = self._sim.get_plan_dict(world, name)
        decision = run_llm_decision(
            world,
            name,
            force_mock=force_mock,
            parent_goal=parent_goal,
            last_goal=cur.get("goal"),
            last_action=cur.get("last_action"),
        )
        self._sim.set_decision_overlay(
            name,
            decision_mode=decision.get("mode"),
            decision_process=decision.get("decision_process"),
            candidate_goals=decision.get("candidate_goals") or [],
            decision_steps=decision.get("steps") or [],
        )

        selected = decision.get("selected_goal")
        if selected:
            selected = dict(selected)
            selected["target_entity"] = _sanitize_goal_target(
                name,
                selected.get("goal", ""),
                selected.get("target_entity"),
            )
        if not selected or not selected.get("goal"):
            self._sim.inject_plan(
                name, {}, [], error="LLM did not produce a valid goal", world=world
            )
            return self._sim.get_plan_dict(world, name)

        return self.set_goal_and_plan(
            world,
            name,
            selected["goal"],
            selected.get("target_entity"),
            reason=selected.get("reason", "LLM decision"),
        )

    def execute_next_step(self, world, name: str) -> Dict[str, Any]:
        result = self._sim.tick(
            world,
            dt=1.0,
            characters=[name],
            auto_plan=False,
            instant=True,
        )
        agents = result.get("agents") or []
        if agents:
            return agents[0]
        return {
            "character": name,
            "skipped": True,
            "reason": "No agent state",
            "state": self._sim.get_plan_dict(world, name),
        }

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
        return self._sim.run_turn(
            world,
            characters=characters,
            auto_plan=auto_plan,
            use_llm=use_llm,
            force_mock=force_mock,
            include_player=include_player,
        )

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
        return self._sim.tick(
            world,
            dt,
            characters=characters,
            auto_plan=auto_plan,
            use_llm=use_llm,
            force_mock=force_mock,
            instant=instant,
            include_player=include_player,
        )


def list_goal_terms() -> List[Dict[str, str]]:
    out = []
    for term in get_all_goal_terms():
        defn = get_goal_definition(term)
        out.append({
            "goal": term,
            "description": defn.description if defn else "",
            "example": defn.example if defn else "",
        })
    return out


def get_goal_target_options(world, character: str, goal: str) -> Dict[str, Any]:
    """Return optional target_entity list for the given goal type."""
    locations = world.list_entities("Location")
    items = world.list_entities("Item")
    persons = [p for p in world.list_entities("Person") if p != character]

    rows = world.run_query(
        """
        MATCH (p:Person {name: $name})-[:LOCATED_AT]->(l:Location)
        OPTIONAL MATCH (l)-[:ADJACENT_TO]->(adj:Location)
        RETURN collect(DISTINCT adj.name) AS adjacent
        """,
        name=character,
    )
    adjacent = [x for x in (rows[0].get("adjacent") or []) if x] if rows else []

    rows2 = world.run_query(
        """
        MATCH (p:Person {name: $name})
        OPTIONAL MATCH (p)-[:VISITED]->(v:Location)
        RETURN collect(DISTINCT v.name) AS visited
        """,
        name=character,
    )
    visited = set(rows2[0].get("visited") or []) if rows2 else set()
    unvisited = [loc for loc in locations if loc not in visited]

    if goal in ("Go to location",):
        options = locations
        kind = "Location"
    elif goal == "Explore unknown location":
        options = unvisited or adjacent or locations
        kind = "Location"
    elif goal in ("Collect item", "Accumulate items", "Strengthen abilities", "Enhance capabilities"):
        rows = world.run_query(
            """
            MATCH (i:Item)-[:LOCATED_AT]->(l:Location)
            WHERE NOT (:Person)-[:HAS_ITEM]->(i)
              AND coalesce(i.item_kind, '') <> 'door'
              AND coalesce(i.portable, true) <> false
            RETURN i.name AS name ORDER BY name
            """
        )
        options = [r["name"] for r in rows if r.get("name")]
        kind = "Item"
    elif goal == "Use item on":
        rows = world.run_query(
            """
            MATCH (i:Item)
            WHERE coalesce(i.item_kind, '') = 'door' AND coalesce(i.locked, false) = true
            RETURN i.name AS name ORDER BY name
            """
        )
        options = [r["name"] for r in rows if r.get("name")]
        kind = "Item"
    elif goal in ("Defeat enemy", "Gain information"):
        options = persons
        kind = "Person"
    else:
        options = locations + items + persons
        kind = "any"

    return {
        "goal": goal,
        "target_kind": kind,
        "options": options,
        "suggested": options[0] if options else None,
        "hint": _target_hint(goal, options),
    }


def _target_hint(goal: str, options: List[str]) -> str:
    if goal in COLLECT_GOALS:
        if not options:
            return "No pickable items on the ground (already held or not placed at a location)"
        return f"Pickable ground items: {', '.join(options)}"
    if goal == "Use item on":
        if not options:
            return "No locked doors"
        return f"Interactable locked doors: {', '.join(options)}"
    return ""


_manager: Optional[CharacterPlanManager] = None


def get_plan_manager() -> CharacterPlanManager:
    global _manager
    if _manager is None:
        _manager = CharacterPlanManager()
    return _manager
