"""
轻量 Agent 配置 — 从 Neo4j 读取状态，供 LLM 决策使用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

from goal_manager import GoalManager


def _parse_values(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(v).strip() for v in raw if str(v).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            import json
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:
            pass
    return [v.strip() for v in text.replace(";", ",").split(",") if v.strip()]


class AgentProfile:
    """AITown3 Agent：不继承 BaseEntity，只暴露决策所需接口。"""

    def __init__(
        self,
        world: "Neo4jWorld",
        name: str,
        *,
        parent_goal: str = "",
        goal: str = "",
        personality: Optional[str] = None,
        background: Optional[str] = None,
        values: Optional[List[str]] = None,
        mood: str = "neutral",
    ):
        self.world = world
        self.name = name
        # 兼容旧字段：不变动机写在 background/personality
        self.parent_goal = parent_goal or ""
        # 当前目标：世界配置给初值，之后随决策更新
        self.goal = (goal or "").strip()
        self.personality = personality or "curious and friendly"
        self.background = background or f"{name} is an adventurer in a small town."
        self.values = values or ["honor", "friendship", "courage"]
        self.mood = mood
        self._goal_manager: Optional[GoalManager] = None
        self._memory = None  # ChromaMemory，由 GameLoop 注入

    @classmethod
    def from_world(cls, world: "Neo4jWorld", name: str) -> "AgentProfile":
        """从 Neo4j Person 节点加载人设与当前目标。"""
        entity = world.get_entity_by_name(name, label="Person")
        props: Dict[str, Any] = {}
        if entity:
            if hasattr(entity, "items"):
                props = dict(entity.items())
            elif isinstance(entity, dict):
                props = entity

        def _get(key: str, default: Any = None) -> Any:
            val = props.get(key)
            return val if val is not None and val != "" else default

        values = _parse_values(_get("values"))
        goal = _get("goal") or _get("initial_goal") or ""
        return cls(
            world,
            name,
            parent_goal=_get("parent_goal", "") or "",
            goal=str(goal or ""),
            personality=_get("personality"),
            background=_get("background"),
            values=values or None,
            mood=str(_get("mood", "neutral")),
        )

    def refresh_from_world(self) -> None:
        """同步 Neo4j 中的最新设定（位置/HP 每次读取，设定也可热更新）。"""
        fresh = AgentProfile.from_world(self.world, self.name)
        self.parent_goal = fresh.parent_goal
        # 运行中已有目标则不覆盖；否则用图上的初值
        if not self.goal:
            self.goal = fresh.goal
        self.personality = fresh.personality
        self.background = fresh.background
        self.values = fresh.values
        self.mood = fresh.mood

    def set_current_goal(self, goal_data: Optional[Dict[str, Any]]) -> None:
        """用最新决策更新可变当前目标，并写回 Neo4j 供前端展示。"""
        if not goal_data:
            return
        term = (goal_data.get("goal") or goal_data.get("final_goal") or "").strip()
        target = goal_data.get("target_entity")
        reason = (goal_data.get("reason") or "").strip()
        if term and target:
            text = f"{term} → {target}"
        elif term:
            text = term
        else:
            text = reason
        if not text:
            return
        self.goal = text
        try:
            self.world.run_query(
                """
                MATCH (p:Person {name: $n})
                SET p.goal = $g
                """,
                n=self.name,
                g=text,
            )
        except Exception:
            pass

    def get_settings_summary(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "goal": self.goal,
            "personality": self.personality,
            "background": self.background,
            "values": self.values,
            "mood": self.mood,
        }

    @property
    def goal_manager(self) -> GoalManager:
        if self._goal_manager is None:
            self._goal_manager = GoalManager()
        return self._goal_manager

    def set_memory(self, memory) -> None:
        self._memory = memory

    @property
    def location(self) -> Optional[str]:
        rows = self.world.run_query(
            "MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1",
            n=self.name,
        )
        return rows[0]["loc"] if rows else None

    @property
    def hp(self) -> int:
        rows = self.world.run_query(
            "MATCH (p:Person {name: $n}) RETURN coalesce(p.hp, 0) AS hp LIMIT 1",
            n=self.name,
        )
        return int(rows[0]["hp"]) if rows else 0

    @property
    def power_level(self) -> int:
        rows = self.world.run_query(
            "MATCH (p:Person {name: $n}) RETURN coalesce(p.power_level, 0) AS pl LIMIT 1",
            n=self.name,
        )
        return int(rows[0]["pl"]) if rows else 0

    def get_items(self) -> List[str]:
        rows = self.world.run_query(
            "MATCH (p:Person {name: $n})-[:HAS_ITEM]->(i:Item) RETURN i.name AS name",
            n=self.name,
        )
        return [r["name"] for r in rows]

    def get_status(self) -> str:
        parts = [f"name: {self.name}"]
        loc = self.location
        if loc:
            parts.append(f"location: {loc}")
        parts.append(f"hp: {self.hp}")
        parts.append(f"power level: {self.power_level}")
        items = self.get_items()
        parts.append(f"bag: {items if items else []}")
        parts.append(f"personality: {self.personality}")
        parts.append(f"mood: {self.mood}")
        if self.background:
            parts.append(f"background: {self.background}")
        if self.values:
            parts.append(f"values: {', '.join(self.values)}")
        return "\n".join(parts)

    def get_memories(self, limit: int = 10) -> List[Dict[str, Any]]:
        if self._memory is None:
            return []
        try:
            items = self._memory.list(self.name)
            out = []
            for item in items[-limit:]:
                meta = item.get("metadata") or {}
                out.append(
                    {
                        "description": item.get("text", ""),
                        "action": meta.get("type", "unknown"),
                        "result": meta.get("result", ""),
                    }
                )
            return out
        except Exception:
            return []

    def get_recent_memories(self, limit: int = 5) -> List[Dict[str, Any]]:
        return self.get_memories(limit=limit)

    def search_memories(self, query: str, top_k: int = 5) -> List[str]:
        """按语义检索记忆文本；无 memory 时返回空。"""
        if self._memory is None or not (query or "").strip():
            return []
        try:
            hits = self._memory.search(self.name, query.strip(), top_k=top_k)
            return [h.get("text", "") for h in hits if h.get("text")]
        except Exception:
            return [m.get("description", "") for m in self.get_recent_memories(top_k) if m.get("description")]

    def generate_candidate_goals(
        self,
        decider,
        parent_goal: Optional[str] = None,
        current_goal: Optional[str] = None,
        last_action_result: Optional[str] = None,
        last_action_success: bool = True,
        num_candidates: int = 1,
        verbose: bool = False,
    ) -> List[dict]:
        from multi_step_decider import MultiStepDecider

        parent = parent_goal or self.parent_goal
        if isinstance(decider, MultiStepDecider):
            result = decider.decide_goal_multi_step(
                agent=self,
                parent_goal=parent,
                current_goal=current_goal,
                last_action_result=last_action_result,
                last_action_success=last_action_success,
                generate_multiple=False,
                num_candidates=1,
                verbose=verbose,
            )
            if result.get("candidate_goals"):
                for candidate_data in result["candidate_goals"]:
                    self.goal_manager.add_candidate_from_dict(candidate_data)
                return result["candidate_goals"]
            goal_data = result.get("goal_data", {})
            candidate = self.goal_manager.add_candidate_from_dict(goal_data)
            return [candidate.to_dict()]
        raise TypeError("decider must be MultiStepDecider")

    def select_goal_to_execute(self, strategy: str = "priority_confidence") -> Optional[dict]:
        selected = self.goal_manager.select_goal(strategy=strategy)
        return selected.to_dict() if selected else None

    def complete_current_goal(self) -> bool:
        return self.goal_manager.complete_goal()
