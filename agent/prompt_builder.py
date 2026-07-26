"""
Prompt 构建器 — Neo4j 上下文 + Chroma 语义记忆。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from agent_profile import AgentProfile
    from neo4j_world import Neo4jWorld


class PromptBuilder:
    def __init__(self, world: Optional["Neo4jWorld"] = None, memory=None):
        if world is None:
            from neo4j_world import Neo4jWorld

            world = Neo4jWorld.get_instance()
        self.world = world
        self.memory = memory

    def build_situation_block(
        self,
        agent: "AgentProfile",
        *,
        memory_limit: int = 16,
        sim_time: Optional[float] = None,
    ) -> str:
        """规划/决策共用：周围节点 + 已知事实 + 最近记忆。"""
        parts = [
            "=== Nearby Graph (current place) ===",
            self._get_local_graph_text(agent),
            "",
            "=== Daily Routine / Time of Day ===",
            self._get_routine_text(agent, sim_time=sim_time),
            "",
            "=== Known Facts / Clues ===",
            self._get_known_facts_text(agent),
            "",
            "=== Recent Memories ===",
            self._get_memories_text(agent, limit=memory_limit),
        ]
        return "\n".join(parts)

    def build_goal_prompt(
        self,
        agent: "AgentProfile",
        parent_goal: Optional[str] = None,
        current_goal: Optional[str] = None,
        last_action_result: Optional[str] = None,
        last_action_success: bool = True,
        task_instructions: Optional[str] = None,
    ) -> str:
        status = agent.get_status()
        situation = self.build_situation_block(agent)
        available_entities = self._get_available_entities_text()
        recent_actions = self._get_recent_actions_text(agent)

        prompt = f"""You live in a small production camp that survives by chopping wood, gathering berries, keeping the campfire lit, and cooking meals together. Decide the next goal from work, fire, food, and people.

=== Character Status ===
{status}

{situation}

=== Available Entities in World ===
{available_entities}

=== Recent Actions ===
{recent_actions}

=== Task Context ===
Persona is fixed; goals are chosen each time from the situation.
Keep the campfire burning — if fuel is low, fetch wood and use it on the Campfire.
At meal times, bring berries to the Campfire to roast, then eat (use food items).
Interactive mechanisms use goal "Interact with" plus interact_action (e.g. chop, turn_clockwise) when temporary actions are listed nearby; the plan binds that specific action, not a vague auto pick.
Stories emerge from shared work: delayed wood, who forgot the fire, who found extra berries, secrets told at night.

{self._get_task_instructions(task_instructions)}

=== Goal Selection ===
"""
        if current_goal:
            prompt += f"Current goal: {current_goal}\n"
        if last_action_result:
            flag = "success" if last_action_success else "failed"
            prompt += f"Last action result ({flag}): {last_action_result}\n"

        prompt += f"""
=== Instructions ===
1. Prefer fire safety and meal routines when relevant; otherwise follow your work routine
2. Collect wood in Forest (Interact with OakTree + interact_action=chop when listed); collect berries in BerryGrove; Use item on Campfire to fuel or cook
3. If someone is here, talking ("Gain information") can start small personal stories
4. The goal must be one of the predefined goal terms (see below)
5. When Temporary Actions are listed, prefer Interact with + that interact_action id over a bare Interact with

=== Available Goal Terms ===
{self._format_goal_terms()}

=== Response Format ===
Respond with a JSON object:
{{
    "goal": "one of the goal terms above",
    "target_entity": "entity name (location, NPC, or item) if applicable, or null",
    "interact_action": "temporary action id when goal is Interact with (e.g. chop), or null",
    "reason": "brief reason for choosing this goal (1-2 sentences)"
}}
"""
        return prompt

    def _person_location(self, name: str) -> Optional[str]:
        try:
            rows = self.world.run_query(
                """
                MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location)
                RETURN l.name AS loc LIMIT 1
                """,
                n=name,
            )
            return rows[0]["loc"] if rows else None
        except Exception:
            return None

    def _get_local_graph_text(self, agent: "AgentProfile") -> str:
        name = agent.name
        loc = self._person_location(name) or getattr(agent, "location", None) or "Unknown"
        lines: List[str] = [f"You are at: {loc}"]
        try:
            desc_rows = self.world.run_query(
                """
                MATCH (l:Location {name: $loc})
                RETURN coalesce(l.description, '') AS d
                LIMIT 1
                """,
                loc=loc,
            )
            if desc_rows and desc_rows[0].get("d"):
                lines.append(f"Place: {desc_rows[0]['d']}")
        except Exception:
            pass

        try:
            adj = self.world.run_query(
                """
                MATCH (l:Location {name: $loc})-[:ADJACENT_TO]->(n:Location)
                RETURN n.name AS n ORDER BY n
                """,
                loc=loc,
            ) or []
            exits = [r["n"] for r in adj if r.get("n")]
            lines.append(f"Exits: {', '.join(exits) if exits else '(none)'}")
        except Exception:
            lines.append("Exits: (unknown)")

        try:
            people = self.world.run_query(
                """
                MATCH (p:Person)-[:LOCATED_AT]->(l:Location {name: $loc})
                WHERE p.name <> $me
                RETURN p.name AS n,
                       coalesce(p.hp, 0) AS hp,
                       coalesce(p.mood, '') AS mood,
                       coalesce(p.role, 'npc') AS role
                ORDER BY n
                """,
                loc=loc,
                me=name,
            ) or []
            if people:
                bits = [
                    f"{r['n']}(hp={r.get('hp')}, mood={r.get('mood') or '-'}, role={r.get('role')})"
                    for r in people
                    if r.get("n")
                ]
                lines.append("People here: " + "; ".join(bits))
            else:
                lines.append("People here: (none)")
        except Exception:
            lines.append("People here: (unknown)")

        try:
            items = self.world.run_query(
                """
                MATCH (i:Item)-[:LOCATED_AT]->(l:Location {name: $loc})
                WHERE NOT (:Person)-[:HAS_ITEM]->(i)
                RETURN i.name AS n,
                       coalesce(i.item_kind, '') AS kind,
                       coalesce(i.locked, false) AS locked
                ORDER BY n
                """,
                loc=loc,
            ) or []
            if items:
                bits = []
                for r in items:
                    if not r.get("n"):
                        continue
                    kind = r.get("kind") or "item"
                    extra = ""
                    if kind == "door":
                        extra = " locked" if r.get("locked") else " open"
                    bits.append(f"{r['n']}[{kind}{extra}]")
                lines.append("Ground items / interactables: " + ", ".join(bits))
            else:
                lines.append("Ground items / interactables: (none)")

            # 已缓存的临时 actions（只读缓存 + 剪枝，不触发 LLM 合成）
            try:
                from ephemeral_action_dsl import load_actions_from_item
                from item_interact import (
                    is_interactive_item,
                    prune_ephemeral_candidates,
                    _score_ephemeral_action,
                    _actor_need_hints,
                )

                raw: List[Dict[str, Any]] = []
                hints = _actor_need_hints(
                    self.world, name, {"goal": getattr(agent, "goal", None)}
                )
                for r in items or []:
                    iname = r.get("n")
                    if not iname or not is_interactive_item(self.world, iname):
                        continue
                    for a in load_actions_from_item(self.world, iname):
                        pri, tags = _score_ephemeral_action(
                            aid=a.id,
                            label=a.label or a.id,
                            item=iname,
                            need_hints=hints,
                        )
                        raw.append(
                            {
                                "target_entity": iname,
                                "interact_action": a.id,
                                "priority": pri,
                                "metadata": {
                                    "action_label": a.label or a.id,
                                    "prune_tags": tags,
                                },
                            }
                        )
                pruned = prune_ephemeral_candidates(raw, max_per_item=2, max_total=4)
                if pruned:
                    bits = [
                        f"{c['target_entity']}.{c['interact_action']}"
                        f" ({(c.get('metadata') or {}).get('action_label')})"
                        for c in pruned
                    ]
                    lines.append(
                        "Temporary Actions (pruned; use Interact with + interact_action): "
                        + "; ".join(bits)
                    )
            except Exception:
                pass
        except Exception:
            lines.append("Ground items / interactables: (unknown)")

        try:
            held = self.world.run_query(
                """
                MATCH (p:Person {name: $me})-[:HAS_ITEM]->(i:Item)
                RETURN i.name AS n ORDER BY n
                """,
                me=name,
            ) or []
            bag = [r["n"] for r in held if r.get("n")]
            lines.append(f"Inventory: {', '.join(bag) if bag else '(empty)'}")
        except Exception:
            lines.append("Inventory: (unknown)")

        try:
            ctx = self.world.get_entity_context_text(name, label="Person", natural_language=True)
            if ctx and "not found" not in ctx.lower():
                lines.append("Your node links:")
                for ln in ctx.splitlines()[1:]:
                    if ln.strip():
                        lines.append(f"  {ln.strip()}")
        except Exception:
            pass

        return "\n".join(lines)

    def _get_known_facts_text(self, agent: "AgentProfile") -> str:
        name = agent.name
        lines: List[str] = []
        try:
            facts = self.world.run_query(
                """
                MATCH (p:Person {name: $n})-[:KNOWS_FACT]->(f:Fact)
                RETURN f.name AS name, coalesce(f.text, '') AS text
                ORDER BY name
                """,
                n=name,
            ) or []
            for r in facts:
                if r.get("text") or r.get("name"):
                    lines.append(f"- [fact] {r.get('text') or r.get('name')}")
            rumors = self.world.run_query(
                """
                MATCH (p:Person {name: $n})-[:BELIEVES]->(r:Rumor)
                RETURN r.name AS name, coalesce(r.text, '') AS text
                ORDER BY name
                """,
                n=name,
            ) or []
            for r in rumors:
                if r.get("text") or r.get("name"):
                    lines.append(f"- [rumor] {r.get('text') or r.get('name')}")
        except Exception as e:
            return f"(could not load clues: {e})"
        return "\n".join(lines) if lines else "(no known facts yet — ask people)"

    def _get_environment_state(self, agent: "AgentProfile") -> str:
        return self._get_local_graph_text(agent)

    def _get_routine_text(
        self,
        agent: "AgentProfile",
        *,
        sim_time: Optional[float] = None,
    ) -> str:
        try:
            from routine import current_slot, describe_routine_context, load_person_routine

            if sim_time is None:
                sim_time = getattr(agent, "_sim_time", None)
            t = 0.0 if sim_time is None else float(sim_time)
            slots = load_person_routine(self.world, agent.name)
            slot = current_slot(slots, t) if slots else None
            campfire = None
            try:
                from world_systems import get_campfire_status

                campfire = get_campfire_status(self.world)
            except Exception:
                campfire = None
            text = describe_routine_context(slot, t, campfire=campfire)
            # 物品行为 lore（给 LLM 读 script）
            try:
                from item_scripts import nearby_item_lores

                lores = nearby_item_lores(self.world, getattr(agent, "location", None))
                if lores:
                    text += "\nItem behaviors here:\n" + "\n".join(lores)
            except Exception:
                pass
            return text
        except Exception as e:
            return f"(no routine info: {e})"

    def _get_memories_text(self, agent: "AgentProfile", limit: int = 16) -> str:
        mem = self.memory or getattr(agent, "_memory", None)
        lines: List[str] = []
        seen: set = set()

        def _add(text: str) -> None:
            text = (text or "").strip()
            if text and text not in seen:
                lines.append(f"- {text}")
                seen.add(text)

        # 优先对话 / 事实记忆（比 talk->ok 动作日志更有决策价值）
        if mem is not None:
            try:
                for q, where in (
                    ("what people told me about cure herbs items", {"type": "dialogue"}),
                    ("learned facts clues", {"type": "fact"}),
                    ("recent actions", {"type": "action"}),
                ):
                    hits = mem.search(agent.name, q, top_k=limit, where=where) or []
                    for hit in hits:
                        _add(hit.get("text") or "")
            except Exception:
                pass
            try:
                hits = mem.search(
                    agent.name,
                    f"recent events for {agent.name}. parent goal: {agent.parent_goal or ''}",
                    top_k=limit,
                ) or []
                for hit in hits:
                    _add(hit.get("text") or "")
            except Exception:
                pass

        try:
            recent = agent.get_recent_memories(limit=limit) or []
            for m in recent:
                _add(m.get("description") or m.get("text") or "")
        except Exception:
            pass

        if not lines:
            return "No memories yet."
        return "Recent memories:\n" + "\n".join(lines[: max(limit, 16)])

    def _get_available_entities_text(self) -> str:
        try:
            lines = []
            for label, title in (
                ("Location", "Locations"),
                ("Person", "NPCs"),
                ("Item", "Items"),
            ):
                names = self.world.list_entities(label)
                if names:
                    lines.append(f"{title}: {', '.join(names)}")
            return "\n".join(lines) if lines else "No entities available."
        except Exception as e:
            return f"Error retrieving entities: {e}"

    def _get_recent_actions_text(self, agent: "AgentProfile", limit: int = 5) -> str:
        mem = self.memory or getattr(agent, "_memory", None)
        if mem is None:
            return "No recent actions."
        try:
            hits = mem.search(
                agent.name, "recent actions executed", top_k=limit, where={"type": "action"}
            )
            if not hits:
                stored = agent.get_memories(limit=limit)
                if not stored:
                    return "No recent actions."
                lines = ["Recent actions:"]
                for m in stored:
                    lines.append(f"  - {m.get('description', 'N/A')}")
                return "\n".join(lines)
            lines = ["Recent actions:"]
            for hit in hits:
                lines.append(f"  - {hit.get('text', 'N/A')}")
            return "\n".join(lines)
        except Exception:
            return "No recent actions available."

    def _get_task_instructions(self, custom: Optional[str] = None) -> str:
        default = """Task Instructions:
1. Communicate with people to gain information when useful.
2. Consider items in your bag (potions restore HP).
3. Only attack enemies you can defeat (compare power levels).
4. Avoid repetitive actions; change strategy if stuck.
5. Use known facts and who/what is nearby before inventing a plan."""
        return custom or default

    def _format_goal_terms(self) -> str:
        from goal_terms import get_all_goal_terms, get_goal_definition

        lines = []
        for term in get_all_goal_terms():
            definition = get_goal_definition(term)
            if definition:
                lines.append(f'  - "{term}": {definition.description}')
            else:
                lines.append(f'  - "{term}"')
        return "\n".join(lines)
