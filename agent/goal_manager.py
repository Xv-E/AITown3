"""
目标管理器 — 管理 NPC 的多个候选目标并提供选择机制。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class GoalStatus(Enum):
    PENDING = "pending"
    SELECTED = "selected"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class CandidateGoal:
    goal: str
    target_entity: Optional[str]
    reason: str
    priority: float = 0.5
    confidence: float = 0.5
    created_at: datetime = field(default_factory=datetime.now)
    status: GoalStatus = GoalStatus.PENDING
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "goal": self.goal,
            "target_entity": self.target_entity,
            "reason": self.reason,
            "priority": self.priority,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
            "status": self.status.value,
            "metadata": self.metadata,
        }
        ia = (self.metadata or {}).get("interact_action")
        if ia:
            d["interact_action"] = ia
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CandidateGoal":
        return cls(
            goal=data["goal"],
            target_entity=data.get("target_entity"),
            reason=data.get("reason", ""),
            priority=data.get("priority", 0.5),
            confidence=data.get("confidence", 0.5),
            created_at=datetime.fromisoformat(data["created_at"])
            if isinstance(data.get("created_at"), str)
            else datetime.now(),
            status=GoalStatus(data.get("status", "pending")),
            metadata=data.get("metadata", {}),
        )


class GoalManager:
    def __init__(self, max_candidates: int = 10):
        self.candidates: List[CandidateGoal] = []
        self.max_candidates = max_candidates
        self.selected_goal: Optional[CandidateGoal] = None

    def add_candidate(
        self,
        goal: str,
        target_entity: Optional[str] = None,
        reason: str = "",
        priority: float = 0.5,
        confidence: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CandidateGoal:
        candidate = CandidateGoal(
            goal=goal,
            target_entity=target_entity,
            reason=reason,
            priority=priority,
            confidence=confidence,
            metadata=metadata or {},
        )
        if len(self.candidates) >= self.max_candidates:
            self.candidates.sort(key=lambda x: x.priority)
            self.candidates.pop(0)
        self.candidates.append(candidate)
        return candidate

    def add_candidate_from_dict(self, goal_data: Dict[str, Any]) -> CandidateGoal:
        meta = dict(goal_data.get("metadata") or {})
        ia = goal_data.get("interact_action") or meta.get("interact_action")
        if ia:
            meta["interact_action"] = ia
        return self.add_candidate(
            goal=goal_data.get("goal", "Gain information"),
            target_entity=goal_data.get("target_entity"),
            reason=goal_data.get("reason", ""),
            priority=goal_data.get("priority", 0.5),
            confidence=goal_data.get("confidence", 0.5),
            metadata=meta,
        )

    def get_pending_candidates(self) -> List[CandidateGoal]:
        return [c for c in self.candidates if c.status == GoalStatus.PENDING]

    def select_goal(
        self,
        strategy: str = "priority_confidence",
        goal: Optional[str] = None,
        target_entity: Optional[str] = None,
    ) -> Optional[CandidateGoal]:
        pending = self.get_pending_candidates()
        if not pending:
            return None

        if strategy == "manual" and goal:
            for candidate in pending:
                if candidate.goal == goal and (
                    target_entity is None or candidate.target_entity == target_entity
                ):
                    self.selected_goal = candidate
                    candidate.status = GoalStatus.SELECTED
                    return candidate
            return None

        if strategy == "first":
            selected = pending[0]
        elif strategy == "priority":
            selected = max(pending, key=lambda x: x.priority)
        elif strategy == "confidence":
            selected = max(pending, key=lambda x: x.confidence)
        else:
            selected = max(pending, key=lambda x: x.priority * 0.6 + x.confidence * 0.4)

        self.selected_goal = selected
        selected.status = GoalStatus.SELECTED
        return selected

    def get_selected_goal(self) -> Optional[CandidateGoal]:
        return self.selected_goal

    def complete_goal(self, goal: Optional[str] = None) -> bool:
        if goal:
            for candidate in self.candidates:
                if candidate.goal == goal:
                    candidate.status = GoalStatus.COMPLETED
                    if self.selected_goal == candidate:
                        self.selected_goal = None
                    return True
            return False
        if self.selected_goal:
            self.selected_goal.status = GoalStatus.COMPLETED
            self.selected_goal = None
            return True
        return False

    def clear_completed(self) -> int:
        initial = len(self.candidates)
        self.candidates = [
            c
            for c in self.candidates
            if c.status in (GoalStatus.PENDING, GoalStatus.SELECTED)
        ]
        return initial - len(self.candidates)

    def clear_all(self) -> int:
        count = len(self.candidates)
        self.candidates.clear()
        self.selected_goal = None
        return count
