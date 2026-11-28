"""homestead 包：需求（饿、困）和床（rules/needs.pl），居民的职业和爱好（rules/jobs.pl）。

一天 300 模拟秒、sim_time = 0 是早上 6 点：T = 25 是 8 点，T = 137.5 是 17 点，T = 200 是 22 点。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pyswip")

from test_social_rules import answers, node, rel, person  # noqa: E402


def at(p, loc):
    return rel("LOCATED_AT", p, loc)


def phase(facts, name, prev, now):
    from prolog_engine import fact

    return answers(facts + [fact("g_tick", prev, now)], "E", f"tick({name}, E)", now=now)


def ops(effs, entity=None):
    return [(e["op"], e.get("prop") or e.get("state") or "", e.get("delta", e.get("value")))
            for e in effs if entity is None or e.get("entity") == entity]


def wants(facts, who, now):
    got = answers(facts, "G", f"want('{who}', G)", now=now)
    return sorted(got, key=lambda g: -g["priority"])


def home(**wren):
    """Hollowbrook 的缩小版：院子（储物处、劈柴墩），屋里一张 Wren 的床，地里一块田。"""
    w = dict(hunger=20, fatigue=10, work="farmer", hobby="gardening", skills='{"farming": 3}')
    w.update(wren)
    return (person("Keeper", is_player=True) + person("Wren", **w)
            + node("Faction", "Hollowbrook", kind="homestead") + rel("MEMBER_OF", "Keeper", "Hollowbrook", role="keeper")
            + rel("MEMBER_OF", "Wren", "Hollowbrook")
            + node("Location", "Yard", controlled_by="Hollowbrook") + node("Location", "Fields", controlled_by="Hollowbrook")
            + node("Location", "House", indoor=True, parent="Yard")
            + node("Item", "Stockpile", item_kind="stockpile") + at("Stockpile", "Yard")
            + node("Item", "Field", item_kind="field") + at("Field", "Fields")
            + node("Item", "WrenBed", item_kind="bed", owner="Wren", quality=3) + at("WrenBed", "House")
            + at("Wren", "Fields"))


# ---------------------------------------------------------------------
# 需求
# ---------------------------------------------------------------------

def test_only_people_who_track_needs_get_hungry_and_tired():
    facts = home() + person("Stranger") + at("Stranger", "Yard")
    effs = phase(facts, "needs", 20.0, 30.0)
    assert ops(effs, "Wren") == [("add_prop", "hunger", 3), ("add_prop", "fatigue", 5)]
    assert ops(effs, "Stranger") == [] and wants(facts, "Stranger", 30.0) == []


def test_a_proper_bed_rests_faster_and_the_rested_wake_with_the_sun():
    in_bed = [f for f in home(state="asleep", fatigue=40) if f != at("Wren", "Fields")[0]] + at("Wren", "House")
    assert ops(phase(in_bed, "needs", 195.0, 205.0), "Wren") == [("add_prop", "hunger", 1), ("add_prop", "fatigue", -17)], \
        "6 + 3 × quality 3 + 2 for the roof"
    on_ground = home(state="asleep", fatigue=40)
    assert ("add_prop", "fatigue", -4) in ops(phase(on_ground, "needs", 195.0, 205.0), "Wren"), "away from your bed you sleep badly"
    rested = [f for f in home(state="asleep", fatigue=12) if f != at("Wren", "Fields")[0]] + at("Wren", "House")
    assert ("set_state", "", None) in ops(phase(rested, "needs", 20.0, 30.0), "Wren"), "rested and it's morning: up"
    assert ("set_state", "", None) not in ops(phase(rested, "needs", 220.0, 230.0), "Wren"), "but not in the middle of the night"


def test_the_hungry_put_down_their_work_only_if_there_is_food():
    working = home(state="working", hunger=65)
    assert ("set_state", "", None) not in ops(phase(working, "needs", 45.0, 55.0), "Wren"), "nothing to eat: keep going"
    fed = working + node("Item", "Turnip1", item_kind="food", value=6) + at("Turnip1", "Yard")
    assert ("set_state", "", None) in ops(phase(fed, "needs", 45.0, 55.0), "Wren")


def test_bedless_residents_claim_a_free_bed_on_their_own_ground():
    facts = home() + person("Pell", hunger=10) + rel("MEMBER_OF", "Pell", "Hollowbrook") \
        + node("Item", "Spare", item_kind="bed", owner="") + at("Spare", "House") \
        + node("Item", "FarBed", item_kind="bed") + at("FarBed", "Elsewhere")
    claims = [e for e in phase(facts, "needs", 20.0, 30.0) if e.get("prop") == "owner"]
    assert claims == [{"op": "set_prop", "entity": "Spare", "label": "Item", "prop": "owner", "value": "Pell"}]


def test_hungry_people_eat_what_is_at_hand_then_the_best_in_the_stockpile():
    facts = (home(hunger=60) + node("Item", "Turnip1", item_kind="food", value=6) + at("Turnip1", "Yard")
             + node("Item", "Stew1", item_kind="meal", value=16) + at("Stew1", "Yard")
             + node("Item", "TheirPie", item_kind="food", value=30) + at("TheirPie", "Yard") + rel("OWNS", "Keeper", "TheirPie"))
    top = wants(facts, "Wren", 60.0)[0]
    assert top["goal"] == "Use item" and top["target"] == "Stew1", "the stew fills you most; someone else's pie is not yours"
    held = facts + node("Item", "Crust", item_kind="food", value=1) + rel("HAS_ITEM", "Wren", "Crust")
    assert wants(held, "Wren", 60.0)[0]["target"] == "Crust"


def test_the_tired_go_to_their_own_bed_and_the_starving_or_exhausted_act_at_once():
    sleepy = wants(home(fatigue=75), "Wren", 60.0)
    assert sleepy[0]["target"] == "sleep" and sleepy[0]["at_location"] == "House"
    starving = home(hunger=90) + node("Item", "Crust", item_kind="food", value=1) + rel("HAS_ITEM", "Wren", "Crust")
    (choice,) = answers(starving, "C", "react('Wren', C)", now=60.0)
    assert choice["plan"] == [["consume", ["Wren", "Crust"]]]
    (choice,) = answers(home(fatigue=96), "C", "react('Wren', C)", now=60.0)
    assert choice["plan"] == [["sleep", ["Wren"]]]


# ---------------------------------------------------------------------
# 职业和爱好
# ---------------------------------------------------------------------

def blockers(facts, who="Wren", kind="work"):
    got = answers(facts, "B", f"blockers('{who}', {kind}, B)")
    return got[0] if got else None


def test_residents_work_once_they_have_a_bed_a_workplace_and_a_stockpile():
    assert blockers(home()) == []
    no_bed = [f for f in home() if "WrenBed" not in f]
    assert blockers(no_bed) == ["has no bed of their own under a roof"]
    outdoor_bed = [f for f in home() if f != at("WrenBed", "House")[0]] + at("WrenBed", "Yard")
    assert blockers(outdoor_bed) == ["has no bed of their own under a roof"], "the bed must be under a roof"
    no_field = [f for f in home() if "'Field'" not in f]
    assert blockers(no_field) == ["no field on the homestead"]
    no_store = [f for f in home() if "'Stockpile'" not in f]
    assert blockers(no_store) == ["no stockpile to put things in"]
    cook = home(work="cook") + node("Item", "Hearth", item_kind="kitchen_hearth") + at("Hearth", "House")
    assert blockers(cook) == ["no turnip in the stockpile"]


def test_efficiency_comes_from_skill_bed_and_how_they_feel_about_the_keeper():
    assert answers(home(), "E", "efficiency('Wren', work, E)") == [1.17], "(1 + 3/10) × (0.7 + 0.3) × (0.9 + 0)"
    assert answers(home(trust_toward_player=5), "E", "efficiency('Wren', work, E)") == [1.43], "× (0.9 + 0.04 × 5)"


def test_residents_keep_hours_on_their_own():
    morning = wants(home(), "Wren", 30.0)
    assert morning[0]["target"] == "work" and morning[0]["at_location"] == "Fields"
    facts = home() + node("Item", "HerbBed", item_kind="herb_bed") + at("HerbBed", "Yard")
    evening = wants(facts, "Wren", 150.0)
    assert evening[0]["target"] == "pastime" and evening[0]["at_location"] == "Yard"
    assert wants(facts, "Wren", 10.0)[0]["target"] == "wait", "before work: stay around home"
    routine = home(routine='[{"start": 8, "end": 17, "location": "Fields"}]')
    assert all(w["target"] != "work" for w in wants(routine, "Wren", 30.0)), "a written routine wins"
    off = phase(home(state="working"), "jobs", 140.0, 150.0)
    assert ("set_state", "", None) in ops(off, "Wren"), "17:00: knock off"


def test_work_builds_up_into_things_in_the_stockpile_and_skill():
    working = home(state="working", work_progress=0.8)
    effs = phase(working, "jobs", 45.0, 55.0)
    assert {"op": "spawn_item", "archetype": "turnip", "located_at": "Yard", "near": "Stockpile"} in effs
    assert ("set_prop", "work_progress", 0.268) in ops(effs, "Wren"), "0.8 + 0.4 × 1.17 = 1.268: one turnip, 0.268 left"
    skills = next(e["value"] for e in effs if e.get("prop") == "skills")
    assert json.loads(skills) == {"farming": 3.05}
    away = [f for f in working if f != at("Wren", "Fields")[0]] + at("Wren", "Yard")
    assert phase(away, "jobs", 45.0, 55.0) == [], "you have to be at the field"


def test_a_cook_turns_stockpiled_turnips_into_stew():
    cook = (home(work="cook", skills='{"cooking": 5}', state="working", work_progress=0.9)
            + node("Item", "Hearth", item_kind="kitchen_hearth") + at("Hearth", "House")
            + node("Item", "Turnip1", item_kind="food", archetype="turnip", value=6) + at("Turnip1", "Yard"))
    cook = [f for f in cook if f != at("Wren", "Fields")[0]] + at("Wren", "House")
    effs = phase(cook, "jobs", 45.0, 55.0)
    assert {"op": "spawn_item", "archetype": "stew", "located_at": "Yard", "near": "Stockpile"} in effs
    assert {"op": "delete_entity", "entity": "Turnip1", "label": "Item"} in effs


def test_the_home_panel_lists_residents_and_the_stockpile():
    facts = home() + node("Item", "Turnip1", item_kind="food", portable=True) + at("Turnip1", "Yard")
    (card,) = answers(facts, "C", "panel('Keeper', home, C)")
    assert card["homestead"] == "Hollowbrook" and [r["name"] for r in card["residents"]] == ["Wren"]
    assert card["residents"][0]["working"] is True and card["stockpile"] == [{"kind": "food", "count": 1}]


# ---------------------------------------------------------------------
# 真实世界：Hollowbrook 过一天
# ---------------------------------------------------------------------

@pytest.fixture
def hollowbrook(world):
    from director import build_from_blueprint, load_world

    build_from_blueprint(world, load_world("homestead"), clear=True)
    return world


def test_world_props_reach_the_graph_and_eating_fills_you_up(hollowbrook):
    from registry import get_registry

    w = hollowbrook
    wren = w.run_query("MATCH (p:Person {name: 'Wren'}) RETURN p.work AS work, p.skills AS skills, p.hunger AS hunger")[0]
    assert wren["work"] == "farmer" and json.loads(wren["skills"])["farming"] == 3 and wren["hunger"] == 20
    w.run_query("MATCH (p:Person {name: 'Wren'}), (i:Item {name: 'Turnip1'}) OPTIONAL MATCH (i)-[r:LOCATED_AT]->() "
                "DELETE r MERGE (p)-[:HAS_ITEM]->(i)")
    assert get_registry().execute(w, "use_item", c="Wren", i="Turnip1")[0]
    assert w.run_query("MATCH (p:Person {name: 'Wren'}) RETURN p.hunger AS h")[0]["h"] == 2, "20 − 6 × 3"


def test_a_day_at_hollowbrook(hollowbrook):
    from agent_profile import AgentProfile
    from game_loop import GameLoop
    from routine import DAY_LENGTH_SECONDS

    w = hollowbrook
    loop = GameLoop(w, verbose=False)
    loop.force_mock = True
    for n in ("Wren", "Tobin", "Mabel", "Pell"):
        loop.add_agent(AgentProfile.from_world(w, n), force_mock=True)
    slept = set()
    while loop.sim_time < 20 * DAY_LENGTH_SECONDS / 24.0:   # 6:00 → 2:00
        loop.tick(0.5, auto_plan=True, max_decisions_per_tick=4)
        for r in w.run_query("MATCH (p:Person {state: 'asleep'}) RETURN p.name AS n"):
            slept.add(r["n"])
    assert w.run_query("MATCH (b:Item {owner: 'Pell'}) RETURN b.name AS b")[0]["b"] == "SpareBed"
    made = {r["k"] for r in w.run_query("MATCH (i:Item) WHERE i.archetype IN ['firewood', 'stew', 'turnip'] "
                                       "AND NOT ()-[:HAS_ITEM]->(i) RETURN DISTINCT i.archetype AS k")}
    assert {"firewood", "stew"} <= made, "the woodcutter and the cook got work done"
    assert slept == {"Wren", "Tobin", "Mabel", "Pell"}
    hungry = w.run_query("MATCH (p:Person) WHERE p.hunger >= 85 RETURN p.name AS n")
    assert hungry == [], "nobody went to bed starving"
    rooms = w.run_query("MATCH (l:Location) WHERE l.name IN ['Farmhouse', 'Bunkhouse'] RETURN l.controlled_by AS c")
    assert all(r["c"] in (None, "Hollowbrook") for r in rooms), "nobody moves into rooms inside the homestead"
