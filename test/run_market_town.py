"""Market Town 端到端演示（需要 Neo4j）：规则决策跑一整天，打印日程 / 补货 / 做饭 / 睡觉。

用法:  python test/run_market_town.py [--hours 24] [--buy-out]
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
from registry import get_registry  # noqa: E402
from routine import DAY_LENGTH_SECONDS, format_clock  # noqa: E402


def _prop(world, item: str, prop: str):
    rows = world.run_query(f"MATCH (i:Item {{name: $n}}) RETURN i.`{prop}` AS v", n=item)
    return rows[0]["v"] if rows else None


def _snapshot(world) -> str:
    rows = world.run_query(
        """
        MATCH (p:Person)-[:LOCATED_AT]->(l:Location)
        OPTIONAL MATCH (p)-[:HAS_ITEM]->(i:Item)
        RETURN p.name AS n, l.name AS loc, p.state IS NOT NULL AS zz,
               collect(i.name) AS items ORDER BY n
        """
    )
    parts = []
    for r in rows:
        zz = " zZ" if r["zz"] else ""
        parts.append(f"{r['n']}@{r['loc']}{zz}{r['items'] or ''}")
    return " | ".join(parts)


def main() -> None:
    import logging

    logging.disable(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--buy-out", action="store_true", help="Traveler buys the stall empty at 8:00")
    ap.add_argument(
        "--trigger-mode",
        action="store_true",
        help="LLM-style trigger gating with a stub LLM that proposes nothing (routine/alerts as fallback)",
    )
    args = ap.parse_args()

    world = Neo4jWorld()
    build_from_blueprint(world, load_world("market_town"), clear=True)
    loop = GameLoop(world, verbose=False)
    loop.force_mock = not args.trigger_mode
    for name in ("Mira", "Bruno", "Tilda"):
        loop.add_agent(AgentProfile.from_world(world, name), force_mock=True)
    decisions = {}
    if args.trigger_mode:
        for st in loop.agents:
            st.agent.generate_candidate_goals = lambda **_: []
        original = loop._decide_and_plan

        def traced(state, **kw):
            why = ", ".join(t.kind for t in state.pending_triggers) or "-"
            decisions[state.agent.name] = decisions.get(state.agent.name, 0) + 1
            print(f"[{format_clock(loop.sim_time)}] {state.agent.name} ?? decide because: {why}")
            return original(state, **kw)

        loop._decide_and_plan = traced

    dt = 0.5
    total = args.hours * DAY_LENGTH_SECONDS / 24.0
    last_line = ""
    bought = False
    reg = get_registry()
    while loop.sim_time < total:
        res = loop.tick(dt, auto_plan=True, max_decisions_per_tick=3)
        if args.buy_out and not bought and loop.sim_time >= 2 * DAY_LENGTH_SECONDS / 24.0:
            bought = True
            world.run_query(
                """
                MATCH (p:Person {name: 'Traveler'})-[r:LOCATED_AT]->() DELETE r
                WITH 1 AS _ MATCH (p:Person {name: 'Traveler'}), (l:Location {name: 'Market'})
                MERGE (p)-[:LOCATED_AT]->(l)
                """
            )
            for coin in ("Coin1", "Coin2"):
                ok, msg = reg.execute(
                    world, "use_on", c="Traveler", i=coin, target="MarketStall",
                    **{"from": "Market", "to": "Market"},
                )
                print(f"   >> Traveler buys with {coin}: {ok} {msg}")
        for a in res.get("agents") or []:
            act = a.get("action") or {}
            if a.get("phase") in ("started",) and act.get("action"):
                print(
                    f"[{format_clock(loop.sim_time)}] {a['agent']}: "
                    f"{act['action']}({', '.join(act.get('args') or [])})"
                )
            if a.get("interrupted") and a.get("replan_reason"):
                print(f"[{format_clock(loop.sim_time)}] {a['agent']} !! {a['replan_reason']}")
        line = (
            f"stall stock={_prop(world, 'MarketStall', 'stock')} "
            f"orders={_prop(world, 'OrderBoard', 'orders')} :: {_snapshot(world)}"
        )
        if line != last_line:
            print(f"   [{format_clock(loop.sim_time)}] {line}")
            last_line = line
    if decisions:
        print(f"decisions per NPC: {decisions}")


if __name__ == "__main__":
    main()
