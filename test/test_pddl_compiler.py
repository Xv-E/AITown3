"""内容 JSON → PDDL 自动编译（不依赖 Neo4j）：手工快照 + 真实规划器求解。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("actions", "planner", "agent"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from compiler import PlanningConfig, Snapshot, compile_pddl  # noqa: E402
from content import apply_archetype  # noqa: E402
from solver import solve_pddl  # noqa: E402


def _item(arch: str, **extra):
    spec = apply_archetype({"archetype": arch, **extra})
    props = {
        k: (json.dumps(v) if k in ("effects_on", "accepts_from") else v)
        for k, v in spec.items()
        if k not in ("script", "blueprint", "footprint")
    }
    props.update(((spec.get("script") or {}).get("initial_state") or {}))
    props.update(extra)
    return ("Item", props)


def _village() -> Snapshot:
    snap = Snapshot()
    snap.objects.update(
        Village=("Location", {}),
        Forest=("Location", {}),
        Kai=("Person", {"hp": 100}),
        Oren=("Person", {"hp": 100}),
        Log1=_item("log"),
        Log2=_item("log"),
        Workshop=_item("workbench"),
        Site=_item("house_site", need_log=2),
    )
    snap.edges["ADJACENT_TO"] = {("Village", "Forest"), ("Forest", "Village")}
    snap.location.update(
        Kai="Village", Oren="Village", Log1="Forest", Log2="Forest", Workshop="Village", Site="Village"
    )
    return snap


def _solve(compiled):
    result = solve_pddl(compiled.domain, compiled.problem, engines=["unified_planning"])
    assert result["success"], result.get("error")
    return result, compiled.decode(result["plan"])


def test_actions_compile_from_requires_and_effects():
    c = compile_pddl(_village(), "Kai", [{"op": "has_item", "person": "@self", "item": "kind:plank"}],
                     config=PlanningConfig())
    assert "(:action move" in c.domain and "(:action pick_up" in c.domain
    assert "(adjacent-to ?from ?to)" in c.domain
    assert "(not (held ?i))" in c.domain
    assert "use_on--log--workbench" in c.domain


def test_relevance_prunes_actions_that_cannot_help():
    c = compile_pddl(_village(), "Kai", [{"op": "has_item", "person": "@self", "item": "kind:plank"}],
                     config=PlanningConfig())
    names = c.action_names()
    assert "attack" not in names and "talk" not in names and "sleep" not in names
    assert "pruned: cannot help reach the goal" in c.report.text()


def test_crafting_chain_through_spawn_pool():
    c = compile_pddl(_village(), "Kai", [{"op": "has_item", "person": "@self", "item": "kind:plank"}],
                     config=PlanningConfig())
    _, plan = _solve(c)
    assert plan[0] == ("move", ["Kai", "Village", "Forest"])
    assert plan[1][0] == "pick_up"
    assert plan[-1][0] == "use_on" and plan[-1][1][2] == "Workshop"


def test_numeric_goal_delivers_materials():
    c = compile_pddl(_village(), "Kai", [{"op": "prop_lte", "entity": "Site", "prop": "need_log", "value": 0}],
                     config=PlanningConfig())
    assert ":numeric-fluents" in c.domain
    result, plan = _solve(c)
    assert "enhsp" in result["engine"]
    delivered = [s for s in plan if s[0] == "use_on" and s[1][2] == "Site"]
    assert len(delivered) == 2


def test_spawned_items_decode_to_kind_placeholders():
    goal = [{"op": "prop_lte", "entity": "Site", "prop": "need_plank", "value": 3}]
    c = compile_pddl(_village(), "Kai", goal, config=PlanningConfig())
    _, plan = _solve(c)
    assert plan[-1] == ("use_on", ["Kai", "kind:plank", "Site", "Village", "Village"])


def test_manual_pruning_turns_problem_into_strips():
    cfg = PlanningConfig.from_dict({
        "exclude_actions": ["give", "use_item", "drop", "use_on--*--site", "craft_*", "gather_*", "buy", "sell"],
        "drop_props": ["hp"],
    })
    c = compile_pddl(_village(), "Kai", [{"op": "has_item", "person": "@self", "item": "kind:plank"}], config=cfg)
    assert ":numeric-fluents" not in c.domain
    assert set(c.action_names()) == {"move", "pick_up", "use_on--log--workbench"}
    assert "excluded by planning.json" in c.report.text()
    result, plan = _solve(c)
    assert "enhsp" not in result["engine"]
    assert plan[-1][0] == "use_on"


def test_drop_selectors_and_hand_written_override():
    cfg = PlanningConfig.from_dict({
        "actions": {
            "use_item": {"drop_effects": ["set_prop:mood"]},
            "move": {"pddl": {
                "pre": ["(located-at ?c ?from)", "(adjacent-to ?from ?to)"],
                "eff": ["(not (located-at ?c ?from))", "(located-at ?c ?to)"],
            }},
        },
        "relevance": False,
    })
    c = compile_pddl(_village(), "Kai", [{"op": "person_at", "person": "@self", "location": "Forest"}], config=cfg)
    report = c.report.text()
    assert "effects[1] set_prop: dropped by planning.json" in report
    assert "hand-written PDDL from planning.json" in report
    _, plan = _solve(c)
    assert plan == [("move", ["Kai", "Village", "Forest"])]


PLANK = [{"op": "has_item", "person": "@self", "item": "kind:plank"}]


def test_kinds_become_types_and_actor_becomes_constant():
    c = compile_pddl(_village(), "Kai", PLANK, config=PlanningConfig())
    assert "k-log k-plank" in c.domain or ("k-log" in c.domain and "k-plank" in c.domain)
    assert "?tool - k-log" in c.domain
    assert "(has-item Kai ?" in c.domain
    assert "is-me" not in c.domain


def test_monotone_hp_condition_drops_numbers():
    c = compile_pddl(_village(), "Kai", PLANK, config=PlanningConfig())
    assert ":numeric-fluents" not in c.domain
    assert "use_item" not in " ".join(c.action_names())


def test_negative_needs_only_for_initially_true_facts():
    names = compile_pddl(_village(), "Kai", PLANK, config=PlanningConfig()).action_names()
    assert "give" not in names and "use_on--log--site" not in names
    relaxed = compile_pddl(_village(), "Kai", PLANK, config=PlanningConfig.from_dict({"negative_needs": "all"}))
    assert "give" in relaxed.action_names()


def test_objects_of_unused_kinds_are_dropped():
    snap = _village()
    for i in range(5):
        snap.objects[f"Flower{i}"] = ("Item", {"item_kind": "flower", "portable": True})
        snap.location[f"Flower{i}"] = "Forest"
    c = compile_pddl(snap, "Kai", PLANK, config=PlanningConfig())
    assert "Flower0" not in c.domain
    assert "dropped 5 of unused kinds" in c.report.text() or "of unused kinds" in c.report.text()
    _solve(c)


def test_max_per_kind_keeps_the_nearest():
    snap = _village()
    snap.objects["Far"] = ("Location", {})
    snap.edges["ADJACENT_TO"] |= {("Forest", "Far"), ("Far", "Forest")}
    snap.objects["LogFar"] = _item("log")
    snap.location["LogFar"] = "Far"
    c = compile_pddl(snap, "Kai", PLANK, config=PlanningConfig.from_dict({"max_per_kind": 2}))
    assert "LogFar" not in c.domain and "Log1" in c.domain
    c = compile_pddl(snap, "Kai", PLANK, config=PlanningConfig.from_dict({"max_hops": 1}))
    assert "LogFar" not in c.domain and " Far " not in c.domain


def test_only_the_planning_character_acts():
    c = compile_pddl(_village(), "Oren", [{"op": "has_item", "person": "@self", "item": "kind:log"}],
                     config=PlanningConfig.from_dict({"drop_props": ["hp"]}))
    _, plan = _solve(c)
    assert plan and all(args[0] == "Oren" for _, args in plan)


def _with_ephemeral(snap: Snapshot, item: str, actions) -> Snapshot:
    label, props = snap.objects[item]
    snap.objects[item] = (label, dict(props, ephemeral_actions=json.dumps(actions)))
    return snap


def test_llm_invented_actions_are_planned_with():
    """物品上的声明式临时动作 = 普通 PDDL 动作：规划器能把它串进链里，解码回 interact。"""
    snap = _village()
    snap.objects["Tree"] = ("Item", {"item_kind": "tree", "portable": False, "logs": 2})
    snap.location["Tree"] = "Forest"
    del snap.location["Log1"], snap.location["Log2"]
    del snap.objects["Log1"], snap.objects["Log2"]
    _with_ephemeral(snap, "Tree", [
        {"id": "chop", "requires": [{"op": "prop_gte", "entity": "@target", "prop": "logs", "value": 1}],
         "effects": [{"op": "spawn_item", "archetype": "log", "held_by": "@actor"},
                     {"op": "add_prop", "entity": "@target", "prop": "logs", "delta": -1}]},
        {"id": "old_style", "cypher": "MATCH (n) RETURN n"},
    ])
    c = compile_pddl(snap, "Kai", [{"op": "has_item", "person": "@self", "item": "kind:plank"}],
                     config=PlanningConfig())
    assert "interact--Tree--chop" in c.domain
    assert "legacy Cypher" in c.report.text()
    _, plan = _solve(c)
    assert ("interact", ["Kai", "Tree", "chop"]) in plan
    assert plan[-1][0] == "use_on"


def test_unchanging_ground_conditions_are_folded_away():
    """全常量、且没有动作会改的条件编译期求值（ENHSP 会把它误判成无解）。"""
    snap = _village()
    snap.objects["Kai"] = ("Person", dict(snap.objects["Kai"][1], mood="calm"))
    snap.objects["Tree"] = ("Item", {"item_kind": "tree", "portable": False})
    snap.location["Tree"] = "Village"
    _with_ephemeral(snap, "Tree", [
        {"id": "hug", "requires": [{"op": "prop_eq", "entity": "@actor", "label": "Person",
                                    "prop": "mood", "value": "calm"}],
         "effects": [{"op": "set_prop", "entity": "@target", "prop": "hugged", "value": True}]},
    ])
    goal = [{"op": "prop_eq", "entity": "Tree", "prop": "hugged", "value": True}]
    c = compile_pddl(snap, "Kai", goal, config=PlanningConfig())
    assert "mood-is" not in c.domain.split("(:action interact")[1]
    _, plan = _solve(c)
    assert plan == [("interact", ["Kai", "Tree", "hug"])]


# ----- RPG systems: gathering, crafting, trading are planned like any other action

def _camp() -> Snapshot:
    snap = Snapshot()
    snap.objects.update(
        Forest=("Location", {}),
        River=("Location", {}),
        Kai=("Person", {"hp": 100, "coins": 0}),
        Reed=("Person", {"hp": 100, "coins": 30, "trader": True}),
        Rock=_item("rock_outcrop"),
        Oak=("Item", {"item_kind": "tree", "portable": False, "logs": 3, "value": 0}),
        Stick=_item("firewood"),
    )
    snap.edges["ADJACENT_TO"] = {("Forest", "River"), ("River", "Forest")}
    snap.location.update(Kai="Forest", Reed="River", Rock="Forest", Oak="Forest", Stick="Forest")
    return snap


def test_an_npc_gathers_and_crafts_a_tool_it_needs():
    c = compile_pddl(_camp(), "Kai", [{"op": "has_item", "person": "@self", "item": "kind:axe"}],
                     config=PlanningConfig())
    _, plan = _solve(c)
    names = [a for a, _ in plan]
    assert "gather_collect_stone" in names and "craft_stone_axe" in names, plan
    assert names.index("gather_collect_stone") < names.index("craft_stone_axe")


def test_a_log_needs_an_axe_first():
    c = compile_pddl(_camp(), "Kai", [{"op": "has_item", "person": "@self", "item": "kind:log"}],
                     config=PlanningConfig())
    _, plan = _solve(c)
    names = [a for a, _ in plan]
    assert names.index("craft_stone_axe") < names.index("gather_fell_log"), plan


def test_with_coins_buying_is_a_way_to_get_things():
    snap = _camp()
    del snap.objects["Rock"], snap.objects["Stick"]
    snap.location.pop("Rock"), snap.location.pop("Stick")
    snap.objects["Kai"] = ("Person", {"hp": 100, "coins": 20})
    snap.objects["Axe1"] = _item("axe", for_sale=True, price=8)
    snap.holder["Axe1"] = "Reed"
    c = compile_pddl(snap, "Kai", [{"op": "has_item", "person": "@self", "item": "kind:axe"}],
                     config=PlanningConfig())
    _, plan = _solve(c)
    assert plan[-1][0] == "buy" and plan[-1][1][1] == "Axe1", plan
    assert "(>= (coins" in c.domain and "(price ?" in c.domain


def test_an_npc_walks_over_to_propose_an_alliance():
    snap = _camp()
    want = [{"op": "has_rel", "a": "@self", "b": "Reed", "rel": "ALLY", "a_label": "Person", "b_label": "Person",
             "directed": True}]
    c = compile_pddl(snap, "Kai", want, config=PlanningConfig())
    _, plan = _solve(c)
    assert plan[0] == ("move", ["Kai", "Forest", "River"]), plan
    assert plan[-1] == ("propose_alliance", ["Kai", "Reed", "River"]), plan
    assert "feels_gte" in c.report.text(), "whether they agree is only known face to face"
