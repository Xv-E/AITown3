"""Frontier Village 端到端演示（需要 Neo4j）：看营地一步步建成村子。

用法:  python test/run_village.py [--days 3] [--trigger-mode] [--quiet]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("", "neo4j", "actions", "planner", "agent", "utility", "memory", "director", "web"):
    p = str(ROOT / sub) if sub else str(ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_profile import AgentProfile  # noqa: E402
from director import build_from_blueprint, load_world  # noqa: E402
from game_loop import GameLoop  # noqa: E402
from neo4j_world import Neo4jWorld  # noqa: E402
from routine import DAY_LENGTH_SECONDS, format_clock  # noqa: E402

NPCS = ("Oren", "Kai", "Lia")


def _sites(world) -> str:
    rows = world.run_query(
        """
        MATCH (i:Item {item_kind: 'site'})
        RETURN i.name AS n, i.need_log AS log, i.need_stone AS stone,
               i.need_plank AS plank, i.need_bread AS bread ORDER BY n
        """
    ) or []
    parts = []
    for r in rows:
        needs = [f"{k}={r[k]}" for k in ("log", "stone", "plank", "bread") if r.get(k)]
        parts.append(f"{r['n']}({', '.join(needs) or 'done'})")
    return ", ".join(parts) or "-"


def _buildings(world, zone: str = "Village") -> str:
    rows = world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(:Location {name: $z})
        WHERE coalesce(i.portable, true) = false
        RETURN i.name AS n, i.x AS x, i.y AS y,
               coalesce(i.footprint_w, 1) AS w, coalesce(i.footprint_h, 1) AS h ORDER BY n
        """,
        z=zone,
    ) or []
    return ", ".join(f"{r['n']}@({r['x']},{r['y']} {r['w']}x{r['h']})" for r in rows)


def _snapshot(world) -> str:
    rows = world.run_query(
        """
        MATCH (p:Person)-[:LOCATED_AT]->(l:Location) WHERE p.name IN $names
        OPTIONAL MATCH (p)-[:HAS_ITEM]->(i:Item)
        RETURN p.name AS n, l.name AS loc, p.state IS NOT NULL AS zz,
               collect(i.name) AS items ORDER BY n
        """,
        names=list(NPCS),
    )
    return " | ".join(
        f"{r['n']}@{r['loc']}{' zZ' if r['zz'] else ''}{r['items'] or ''}" for r in rows
    )


def _player_places_house(world, loop, clock: str) -> bool:
    import spatial
    from building import list_blueprints, place_blueprint

    house = next(
        (b for b in list_blueprints(world, "Traveler", "Village") if b["archetype"] == "house_site"), None
    )
    if not house or not house["available"]:
        return False
    spot = spatial.find_spot(world, "Village", house["w"], house["h"], near=(20, 9))
    if not spot:
        return False
    result = place_blueprint(world, "Traveler", "house_site", spot[0], spot[1])
    print(f"\n>>> [{clock}] player builds: {result['message']}")
    if result.get("success"):
        loop._record_announcements({"events": [{"type": "announce", "text": result["message"]}]})
    return bool(result.get("success"))


def main() -> None:
    import logging

    logging.disable(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--trigger-mode", action="store_true",
                    help="trigger-gated decisions with a stub LLM (routine / alerts as fallback)")
    ap.add_argument("--quiet", action="store_true", help="only print news and site progress")
    ap.add_argument("--player-house", action="store_true",
                    help="the player places a House blueprint as soon as one is available")
    args = ap.parse_args()

    world = Neo4jWorld()
    build_from_blueprint(world, load_world("frontier_village"), clear=True)
    loop = GameLoop(world, verbose=False)
    loop.force_mock = not args.trigger_mode
    for name in NPCS:
        loop.add_agent(AgentProfile.from_world(world, name), force_mock=True)
    if args.trigger_mode:
        for st in loop.agents:
            st.agent.generate_candidate_goals = lambda **_: []

    dt = 0.5
    total = args.days * DAY_LENGTH_SECONDS
    last_sites = ""
    seen_news = 0
    house_placed = not args.player_house
    print(f"Village layout: {_buildings(world)}")
    while loop.sim_time < total:
        res = loop.tick(dt, auto_plan=True, max_decisions_per_tick=3)
        clock = format_clock(loop.sim_time)
        if not house_placed:
            house_placed = _player_places_house(world, loop, clock)
        if not args.quiet:
            for a in res.get("agents") or []:
                act = a.get("action") or {}
                if a.get("phase") == "started" and act.get("action") and act["action"] != "wait":
                    print(f"[{clock}] {a['agent']}: {act['action']}({', '.join(act.get('args') or [])})")
        for news in loop.chronicle[seen_news:]:
            print(f"\n*** [{news['clock']}] {news['text']}")
            print(f"    Village: {_buildings(world)}\n")
        seen_news = len(loop.chronicle)
        sites = _sites(world)
        if sites != last_sites:
            print(f"   [{clock}] sites: {sites} :: {_snapshot(world)}")
            last_sites = sites

    print("\n=== Chronicle ===")
    for news in loop.chronicle:
        print(f"  {news['clock']}: {news['text']}")
    if not loop.chronicle:
        print("  (nothing built yet)")


if __name__ == "__main__":
    main()
