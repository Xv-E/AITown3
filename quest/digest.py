"""
读当前世界给任务线作者看的摘要（只读数据库）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .spec import KnownWorld


@dataclass
class WorldDigest:
    player: str
    regions: List[Dict[str, Any]] = field(default_factory=list)
    persons: List[Dict[str, Any]] = field(default_factory=list)
    items: List[Dict[str, Any]] = field(default_factory=list)
    tensions: List[Dict[str, Any]] = field(default_factory=list)
    quests: List[Dict[str, Any]] = field(default_factory=list)
    story_arc: str = ""
    archetypes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    other_names: List[str] = field(default_factory=list)

    def known(self) -> KnownWorld:
        return KnownWorld(
            regions={r["name"] for r in self.regions},
            persons={p["name"] for p in self.persons},
            items={i["name"] for i in self.items},
            player=self.player,
            archetypes=set(self.archetypes),
            other_names=set(self.other_names),
        )


def read_digest(world, *, story_arc: str = "") -> WorldDigest:
    from content import get_content

    def q(cypher: str, **params) -> List[Dict[str, Any]]:
        return world.run_query(cypher, **params) or []

    player_rows = q(
        "MATCH (p:Person) WHERE coalesce(p.is_player, false) OR p.role = 'player' RETURN p.name AS n LIMIT 1"
    )
    regions = q(
        """
        MATCH (l:Location)
        OPTIONAL MATCH (l)-[:ADJACENT_TO]->(m:Location)
        RETURN l.name AS name, coalesce(l.description, '') AS description, coalesce(l.tags, '') AS tags,
               collect(DISTINCT m.name) AS next_to
        ORDER BY name
        """
    )
    persons = q(
        """
        MATCH (p:Person)
        OPTIONAL MATCH (p)-[:LOCATED_AT]->(l:Location)
        RETURN p.name AS name, coalesce(p.role, 'npc') AS role, l.name AS location,
               coalesce(p.personality, '') AS personality, coalesce(p.goal, '') AS goal,
               left(coalesce(p.background, ''), 160) AS background
        ORDER BY name
        """
    )
    items = q(
        """
        MATCH (i:Item)
        OPTIONAL MATCH (i)-[:LOCATED_AT]->(l:Location)
        OPTIONAL MATCH (h:Person)-[:HAS_ITEM]->(i)
        RETURN i.name AS name, coalesce(i.item_kind, '') AS kind, l.name AS location, h.name AS holder,
               coalesce(i.portable, true) AS portable, left(coalesce(i.description, ''), 100) AS description
        ORDER BY name
        """
    )
    quests = q(
        """
        MATCH (q:Quest)
        RETURN q.name AS name, coalesce(q.objective, '') AS objective, coalesce(q.state, '') AS state
        """
    )
    others = [r["n"] for r in q("MATCH (n) WHERE n:Fact OR n:Rumor OR n:Quest RETURN n.name AS n") if r.get("n")]
    try:
        from conflicts import all_tensions

        tensions = all_tensions(world, 6)
    except Exception:
        tensions = []
    return WorldDigest(
        player=player_rows[0]["n"] if player_rows else "",
        regions=regions,
        persons=persons,
        items=items,
        tensions=tensions,
        quests=quests,
        story_arc=story_arc,
        archetypes=dict(get_content().archetypes),
        other_names=others,
    )
