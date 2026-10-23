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
        """规划/决策共用：触发原因 + 周围节点 + 已知事实 + 最近记忆。"""
        parts: List[str] = []
        trigger_text = getattr(agent, "_trigger_context", "") or ""
        if trigger_text:
            parts += [
                "=== Why You Are Deciding Now ===",
                trigger_text,
                "Decide whether this changes what you should do. "
                "If your current goal is still best, choose it again.",
                "",
            ]
        talk = getattr(agent, "_recent_talk", "") or ""
        if talk:
            parts += [
                "=== What Was Just Said ===",
                talk,
                "If you were asked for something or promised something, decide now whether to do it; "
                "if you learned something useful, act on it. Your feelings about the speaker matter. "
                "Don't go back to talk about the same thing again unless something new happened.",
                "",
            ]
        feeling = self._get_player_feeling_text(agent)
        if feeling:
            parts += ["=== How You Feel About the Player ===", feeling, ""]
        secret = self._get_secret_text(agent)
        if secret:
            parts += ["=== Your Secret ===", secret,
                      "Nobody else knows this. It shapes what you do; don't give it away lightly.", ""]
        people = self._get_relations_text(agent)
        if people:
            parts += ["=== People You Know ===", people, ""]
        trade = self._get_trade_text(agent)
        if trade:
            parts += ["=== Money and Trade ===", trade, ""]
        asks = self._get_requests_text(agent)
        if asks:
            parts += ["=== Requests From Others ===", asks, ""]
        tensions = self._get_tensions_text(agent)
        if tensions:
            parts += [
                "=== Grudges / Friction ===",
                tensions,
                "You may confront, avoid, make up with, or compete with these people.",
                "",
            ]
        heart = self._get_heart_text(agent)
        if heart:
            parts += ["=== Your Heart, Factions & Promises ===", heart, ""]
        parts += [
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

        prompt = f"""{self._get_world_premise()}
Decide the next goal from your work, the needs of the world around you, food, rest, and people.

=== Character Status ===
{status}

{situation}

=== Available Entities in World ===
{available_entities}

=== Recent Actions ===
{recent_actions}

=== Task Context ===
Persona is fixed; goals are chosen each time from the situation.
When something in the world needs attention and it fits your role, use "Use item on" that thing; the planner fetches or crafts the needed materials.
To eat or consume something, use "Use item" with the item.
Interactive mechanisms use goal "Interact with" plus interact_action when temporary actions are listed nearby; the plan binds that specific action, not a vague auto pick.
Stories emerge from shared work, shortages, favours, and secrets.

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
1. Handle urgent needs that belong to your role; otherwise follow your routine
2. Use the known ways to make things when you need a product
3. If someone is here, talking ("Gain information") can start small personal stories
4. The goal must be one of the predefined goal terms (see below)
5. When Temporary Actions are listed, prefer Interact with + that interact_action id over a bare Interact with
6. Weigh your factions: serve their goals as far as your loyalty goes, help allies, and think twice around rivals — what you do to one member, their whole faction remembers

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

    def _get_trade_text(self, agent: "AgentProfile") -> str:
        """你有多少钱；谁在卖什么（可以用 Collect item 去买，规划器会走 buy）。"""
        if self.world is None:
            return ""
        try:
            me = self.world.run_query(
                "MATCH (p:Person {name: $n}) RETURN coalesce(p.coins, 0) AS c, coalesce(p.trader, false) AS t",
                n=agent.name,
            ) or []
            shops = self.world.run_query(
                """
                MATCH (t:Person)-[:HAS_ITEM]->(i:Item)
                WHERE coalesce(t.trader, false) AND coalesce(i.for_sale, false) AND t.name <> $n
                RETURN t.name AS trader, collect(i.name + ' (' + toString(coalesce(i.price, i.value, 0)) + ' coins)') AS goods
                """,
                n=agent.name,
            ) or []
        except Exception:
            return ""
        if not me:
            return ""
        lines = [f"You have {int(float(me[0].get('c') or 0))} coins."]
        if me[0].get("t"):
            lines.append("You are a trader: people buy your goods and sell you theirs for half their worth.")
            try:
                from economy import shop_shortages

                missing = shop_shortages(self.world, agent.name)
            except Exception:
                missing = []
            if missing:
                lines.append(f"Your shop is out of: {', '.join(missing)}. Gather, make or buy them "
                             "(Collect item kind:<kind>) - what you hold goes on sale by itself.")
        shops = [s for s in shops if s.get("trader") and s.get("goods")]
        for shop in shops:
            lines.append(f"{shop['trader']} sells: {', '.join(shop['goods'][:8])}")
        if shops:
            lines.append("To buy something, choose Collect item with that item; traders also buy what you make.")
        return "\n".join(lines)

    def _get_secret_text(self, agent: "AgentProfile") -> str:
        if self.world is None:
            return ""
        try:
            rows = self.world.run_query(
                "MATCH (p:Person {name: $n}) RETURN coalesce(p.secret, '') AS s", n=agent.name
            ) or []
        except Exception:
            return ""
        return str(rows[0].get("s") or "").strip() if rows else ""

    def _get_requests_text(self, agent: "AgentProfile") -> str:
        if self.world is None:
            return ""
        try:
            from job_board import prompt_text

            return prompt_text(self.world, agent.name)
        except Exception:
            return ""

    def _get_relations_text(self, agent: "AgentProfile") -> str:
        if self.world is None:
            return ""
        try:
            from relations import describe

            return describe(self.world, agent.name)
        except Exception:
            return ""

    def _get_player_feeling_text(self, agent: "AgentProfile") -> str:
        """NPC 对玩家的信任（对话里攒下来的），让它也影响「要不要帮玩家」。"""
        if self.world is None:
            return ""
        try:
            rows = self.world.run_query(
                """
                MATCH (p:Person) WHERE coalesce(p.is_player, false) OR p.role = 'player'
                MATCH (me:Person {name: $me})
                RETURN p.name AS player, coalesce(me.trust_toward_player, 0) AS trust,
                       coalesce(me.is_player, false) OR me.role = 'player' AS me_player
                LIMIT 1
                """,
                me=agent.name,
            ) or []
        except Exception:
            return ""
        if not rows or rows[0].get("me_player"):
            return ""
        player, trust = rows[0]["player"], int(rows[0].get("trust") or 0)
        if trust >= 5:
            feel = f"You trust {player} a lot and are glad to help them."
        elif trust >= 2:
            feel = f"You like {player}; helping them is reasonable if it doesn't cost you much."
        elif trust <= -2:
            feel = f"You distrust {player}; you are reluctant to do what they ask."
        else:
            feel = f"You don't know {player} well yet."
        return f"{feel} (trust {trust})"

    def _get_tensions_text(self, agent: "AgentProfile") -> str:
        if self.world is None:
            return ""
        try:
            from conflicts import describe_tensions

            return describe_tensions(self.world, agent.name)
        except Exception:
            return ""

    def _get_heart_text(self, agent: "AgentProfile") -> str:
        """深层渴望 / 恐惧 + 势力与在场者立场 + 未了的人情：让选择有个人动机。"""
        try:
            from inner_life import favors_text, load_inner_life
        except Exception:
            return ""
        lines: List[str] = []
        try:
            inner = load_inner_life(self.world, agent.name)
            if inner.desire:
                lines.append(f"You long for: {inner.desire}")
            if inner.fear:
                lines.append(f"You fear: {inner.fear}")
            try:
                from factions import faction_situation_text
                from routine import people_at_location

                loc = self._person_location(agent.name)
                here = people_at_location(self.world, loc, exclude=agent.name) if loc else []
                ftext = faction_situation_text(self.world, agent.name, here)
                if ftext:
                    lines.append(ftext)
            except Exception:
                pass
            try:
                from street_economy import economy_text

                money = economy_text(self.world, agent.name)
                if money:
                    lines.append(money)
            except Exception:
                pass
            favors = favors_text(self.world, agent.name)
            if favors:
                lines.append("Open favors (keeping or breaking a promise changes how people feel about you):")
                lines.append(favors)
        except Exception:
            return ""
        return "\n".join(lines)

    def _get_world_premise(self) -> str:
        try:
            rows = self.world.run_query(
                """
                MATCH (m {name: 'WorldMeta'})
                WHERE m.premise IS NOT NULL
                RETURN m.title AS title, m.premise AS premise LIMIT 1
                """
            )
            if rows and rows[0].get("premise"):
                title = rows[0].get("title") or ""
                return f"World: {title}. {rows[0]['premise']}".strip()
        except Exception:
            pass
        return "You live in a small town."

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
                RETURN coalesce(l.description, '') AS d,
                       coalesce(l.indoor, false) AS indoor, coalesce(l.parent, '') AS parent
                LIMIT 1
                """,
                loc=loc,
            )
            if desc_rows and desc_rows[0].get("indoor"):
                parent = desc_rows[0].get("parent")
                lines.append(f"Indoors (a building in {parent})" if parent else "Indoors")
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
                from combat import hostility

                bits = []
                for r in people:
                    if not r.get("n"):
                        continue
                    why = hostility(self.world, name, r["n"])
                    against = hostility(self.world, r["n"], name)
                    tag = f", YOUR ENEMY ({why})" if why else ""
                    tag += f", hostile to you ({against})" if against and against != why else ""
                    bits.append(f"{r['n']}(hp={r.get('hp')}, mood={r.get('mood') or '-'}, role={r.get('role')}{tag})")
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
                OPTIONAL MATCH (o:Person)-[:OWNS]->(i)
                RETURN i.name AS n,
                       coalesce(i.item_kind, '') AS kind,
                       coalesce(i.locked, false) AS locked,
                       o.name AS owner
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
                    if r.get("owner") and r["owner"] != name:
                        extra += f", {r['owner']}'s: taking it is stealing"
                    elif r.get("owner") == name:
                        extra += ", yours"
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
            text = describe_routine_context(slot, t)
            # 物品行为 lore（给 LLM 读 script）
            try:
                from item_scripts import nearby_item_lores

                lores = nearby_item_lores(self.world, getattr(agent, "location", None))
                if lores:
                    text += "\nItem behaviors here:\n" + "\n".join(lores)
            except Exception:
                pass
            try:
                from item_scripts import describe_active_alerts

                needs = describe_active_alerts(self.world)
                if needs:
                    text += (
                        "\nThings in the world that need attention "
                        "(decide yourself whether it is your business):\n" + "\n".join(needs)
                    )
            except Exception:
                pass
            try:
                from item_chain import describe_recipes, load_world_view

                recipes = describe_recipes(load_world_view(self.world))
                if recipes:
                    text += (
                        "\nKnown ways to make things (Use item on the target; "
                        "missing materials are fetched automatically):\n" + "\n".join(recipes)
                    )
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

        # 先放最近的几段对话（按时间，不靠语义碰运气），再按眼下的情况去检索
        if mem is not None:
            try:
                talks = [m for m in mem.list(agent.name) if (m.get("metadata") or {}).get("type") == "dialogue"]
                talks.sort(key=lambda m: str((m.get("metadata") or {}).get("timestamp") or ""), reverse=True)
                for m in talks[:6]:
                    _add(m.get("text") or "")
            except Exception:
                pass
            focus = " ".join(
                str(x) for x in (
                    (getattr(agent, "_trigger_context", "") or "")[:300],
                    getattr(agent, "goal", "") or "",
                    getattr(agent, "location", "") or "",
                ) if x
            ) or f"what matters to {agent.name} right now"
            try:
                for q, where in (
                    (f"what people told me about: {focus}", {"type": "dialogue"}),
                    (f"facts and clues about: {focus}", {"type": "fact"}),
                    (f"what I did recently: {focus}", {"type": "action"}),
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
