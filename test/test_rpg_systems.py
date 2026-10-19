"""RPG 系统：配方 / 采集展开成动作；玩家只说「做什么」，服务端挑材料、工具和工作台。"""

from __future__ import annotations


class FakeWorld:
    """只认 rpg_content 用到的几条查询。"""

    def __init__(self, held, ground, here="Forest", kinds=None):
        self.held, self.ground, self.here, self.kinds = held, ground, here, kinds or {}

    def run_query(self, q, **k):
        if "HAS_ITEM" in q and "toLower" in q:
            return [{"name": n, "kind": kd} for n, kd in self.held]
        if "LOCATED_AT]->(:Location" in q:
            return [{"name": n, "kind": kd} for n, kd in self.ground]
        if "-[:LOCATED_AT]->(l:Location) RETURN l.name" in q:
            return [{"l": self.here}]
        if "MATCH (i:Item {name: $n}) RETURN toLower" in q:
            return [{"k": self.kinds.get(k["n"], "")}]
        return []


def test_recipes_and_gathering_expand_into_plain_actions():
    from rpg_content import expand_gather, expand_recipe, recipe_inputs

    assert recipe_inputs({"inputs": [{"kind": "wood", "count": 2}, "stone"]}) == ["wood", "wood", "stone"]
    action, err = expand_recipe({"id": "rod", "inputs": [{"kind": "wood", "count": 2}],
                                 "output": {"archetype": "fishing_rod"}})
    assert err == "" and action["name"] == "craft_rod"
    assert [p["name"] for p in action["params"]] == ["c", "in1", "in2", "l"]
    assert {"op": "neq", "a": "@in1", "b": "@in2", "message": "You need 2 wood"} in action["requires"]
    assert action["effects"][-1] == {"op": "spawn_item", "held_by": "@c", "archetype": "fishing_rod"}
    gather, _ = expand_gather({"id": "fish", "node": "fishing_spot", "tool": "rod", "stock": "stock",
                               "yield": {"archetype": "fish"}})
    assert [p["name"] for p in gather["params"]] == ["c", "node", "tool", "l"]
    assert gather["effects"][0] == {"op": "add_prop", "entity": "@node", "prop": "stock", "delta": -1, "min": 0}
    assert expand_recipe({"id": "x"})[0] is None


def test_the_server_picks_materials_tools_and_stations():
    from content import get_content
    from rpg_content import bind_params

    actions = get_content().actions
    world = FakeWorld(held=[("Stone1", "stone"), ("Stick1", "wood"), ("Stick2", "wood")],
                      ground=[("Oak", "tree"), ("Rock", "rock")])
    params, why = bind_params(world, "Kai", actions["craft_fishing_rod"])
    assert why == "" and {params["in1"], params["in2"]} == {"Stick1", "Stick2"}
    params, why = bind_params(world, "Kai", actions["gather_fell_log"], target="Oak")
    assert params is None and why == "You need an axe"
    params, why = bind_params(world, "Kai", actions["craft_cook_fish"])
    assert params is None and "fish" in why and "campfire" in why
    params, _ = bind_params(world, "Kai", actions["gather_collect_stone"])
    assert params == {"c": "Kai", "l": "Forest", "node": "Rock"}


def test_the_craft_panel_says_what_is_missing():
    from rpg_content import crafting_view

    world = FakeWorld(held=[("Stone1", "stone")], ground=[])
    recipes = {r["id"]: r for r in crafting_view(world, "Kai")}
    axe = recipes["stone_axe"]
    assert not axe["ready"] and {i["kind"]: (i["have"], i["need"]) for i in axe["inputs"]} == {"stone": (1, 1), "wood": (0, 1)}
    assert recipes["spear"]["station"] == "workbench" and not recipes["spear"]["at_station"]


class Recorder:
    def __init__(self):
        self.queries = []

    def run_query(self, q, **k):
        self.queries.append((q, k))
        if "is_player" in q:
            return [{"pl": k.get("n") == "Ash"}]
        return []


def test_who_joins_your_party_and_who_allies_with_you(monkeypatch):
    import relations

    feel = {("Sage", "Ash"): 3, ("Reed", "Ash"): 0}
    monkeypatch.setattr(relations, "feeling", lambda w, a, b: feel.get((a, b), 0))
    monkeypatch.setattr(relations, "are_allies", lambda w, a, b: False)
    monkeypatch.setattr(relations, "leader_of", lambda w, n: "")
    world = Recorder()
    ok, msg = relations.invite(world, "Ash", "Sage")
    assert ok and any("SET p.follows" in q for q, _ in world.queries)
    ok, msg = relations.invite(world, "Ash", "Reed")
    assert not ok and "rather not" in msg
    ok, msg = relations.propose_alliance(world, "Ash", "Sage")
    assert not ok and "doesn't trust you enough" in msg, "needs a feeling of ALLY_MIN"
    feel[("Sage", "Ash")] = relations.ALLY_MIN
    ok, msg = relations.propose_alliance(world, "Ash", "Sage")
    assert ok and any("MERGE (x)-[r1:ALLY]->(y)" in q for q, _ in world.queries)


def test_gifts_and_deals_change_how_people_feel(monkeypatch):
    import relations

    changes = []
    monkeypatch.setattr(relations, "adjust", lambda w, a, b, d, **k: changes.append((a, b, d)))
    relations.after_action(None, "Ash", "give", {"giver": "Ash", "i": "Berry1", "receiver": "Sage"})
    relations.after_action(None, "Ash", "buy", {"c": "Ash", "i": "Axe", "seller": "Reed"})
    assert changes == [("Sage", "Ash", relations.GIFT), ("Reed", "Ash", relations.TRADE), ("Ash", "Reed", relations.TRADE)]
    assert relations.you_feel(8) == "really like" and relations.you_feel(-4) == "dislike"


def test_party_members_follow_instead_of_deciding():
    from unittest.mock import MagicMock

    from game_loop import AgentState, GameLoop

    class Who:
        def __init__(self, n):
            self.name = n

    loop = GameLoop(MagicMock(), verbose=False)
    loop._person_flags = lambda s: {"is_player": False, "role": "npc", "dead": False, "hp": 100,
                                    "brain": "llm", "follows": "Ash" if s.agent.name == "Sage" else ""}
    sage, reed = AgentState(agent=Who("Sage")), AgentState(agent=Who("Reed"))
    for s in (sage, reed):
        s.pending_triggers = [MagicMock(priority=0.9)]
    loop.agents = [sage, reed]
    loop._may_auto_plan = lambda *a, **k: True
    picked = loop._pick_deciders([sage, reed], include_player=True, limit=0)
    assert picked == [reed], "the follower doesn't make its own decisions"


def test_prices_follow_scarcity_and_demand():
    from economy import MAX_FACTOR, MIN_FACTOR, factor

    assert factor(base=4, supply=4, demand=0) == 1.0
    assert factor(base=4, supply=0, demand=0) > 2.5, "nothing left: expensive"
    assert factor(base=4, supply=40, demand=0) == MIN_FACTOR, "a glut: cheap"
    assert factor(base=4, supply=4, demand=2) > 1.0, "people keep buying: dearer"
    assert factor(base=0, supply=0, demand=50) == MAX_FACTOR


def test_an_empty_shop_becomes_the_traders_own_goal(monkeypatch):
    import economy

    monkeypatch.setattr(economy, "shop_shortages", lambda w, t: ["axe"])
    (goal,) = economy.restock_candidates(None, "Reed")
    assert goal["goal"] == "Collect item" and goal["target_entity"] == "kind:axe" and goal["source"] == "shop"


def test_alliance_groups_and_their_stances(monkeypatch):
    import relations

    assert relations.stance_word(6) == "friendly" and relations.stance_word(-6) == "hostile"
    assert relations.stance_word(-3) == "rivals" and relations.stance_word(0) == "neutral"
    groups = {"Ash": "Ash's Band", "Rook": "Rook's Band"}
    monkeypatch.setattr(relations, "alliance_of", lambda w, n: groups.get(n, ""))
    monkeypatch.setattr(relations, "stance", lambda w, a, b: -7)
    assert relations.hostile(None, "Ash", "Rook")
    assert not relations.hostile(None, "Ash", "Sage"), "not in an alliance: never hostile"


def test_npcs_can_set_out_to_form_an_alliance():
    from content import get_content
    from goal_terms import get_all_goal_terms

    assert "Form alliance" in get_all_goal_terms()
    action = get_content().actions["propose_alliance"]
    assert {"op": "feels_gte", "a": "@other", "b": "@c", "value": 4,
            "message": "@other doesn't trust you enough to be allies yet"} in action["requires"]
    buy = get_content().actions["buy"]
    assert any(r.get("op") == "not_hostile" for r in buy["requires"]), "no deals across hostile alliances"


def test_a_stuck_background_npc_posts_a_request_then_thinks_with_the_llm(monkeypatch):
    from unittest.mock import MagicMock

    import game_loop
    from game_loop import REQUEST_AFTER, STUCK_LIMIT, AgentState, GameLoop

    class Who:
        name = "Reed"

    loop = GameLoop(MagicMock(), verbose=False)
    loop._person_flags = lambda s: {"is_player": False, "role": "npc", "dead": False, "hp": 100,
                                    "brain": "routine", "follows": ""}
    monkeypatch.setattr(game_loop, "_llm_configured", lambda: True)
    import goal_feasibility

    # 假世界答不了「他手上有没有石头」：当作还没有（不然就不算卡住）
    monkeypatch.setattr(goal_feasibility, "assess_goal", lambda *a, **k: MagicMock(already_satisfied=False))
    posted = []
    loop._post_request = lambda state, kind, why: posted.append(kind)
    reed = AgentState(agent=Who())
    loop.agents = [reed]
    want = {"goal": "Collect item", "target_entity": "kind:stone", "reason": "The shop has run out of stone"}
    for _ in range(REQUEST_AFTER):
        loop._note_progress(reed, want, None)
    assert posted == ["stone"] and loop._routine_only(reed), "asks for help first"
    for _ in range(STUCK_LIMIT - REQUEST_AFTER):
        loop._note_progress(reed, want, None)
    assert not loop._routine_only(reed), "still stuck: thinks it over with the LLM for a while"
    loop.sim_time = reed.escalated_until + 1
    assert loop._routine_only(reed), "and goes back to the routine afterwards"

    picked = MagicMock(goal_data=dict(want))
    loop._note_progress(reed, want, picked)
    assert reed.stuck_count == 0, "doing it resets the count"


def test_an_emergency_makes_a_background_npc_think():
    from unittest.mock import MagicMock

    import game_loop
    from game_loop import AgentState, GameLoop
    from triggers import Trigger

    class Who:
        name = "Rowan"

    loop = GameLoop(MagicMock(), verbose=False)
    loop._person_flags = lambda s: {"brain": "routine"}
    game_loop._llm_configured, old = (lambda: True), game_loop._llm_configured
    try:
        rowan = AgentState(agent=Who())
        rowan.pending_triggers = [Trigger("event", "alert", "The campfire is out!", 1.0, 0.0)]
        assert not loop._routine_only(rowan)
        assert rowan.escalated_until > 0 and rowan.history[-1]["kind"] == "state"
    finally:
        game_loop._llm_configured = old


def test_requests_are_planned_by_fetching_the_thing_and_handing_it_over():
    from compiler import PlanningConfig, compile_pddl
    from solver import solve_pddl
    from test_pddl_compiler import _camp

    snap = _camp()
    c = compile_pddl(snap, "Kai", [{"op": "has_item", "person": "Reed", "item": "kind:stone"}], config=PlanningConfig())
    result = solve_pddl(c.domain, c.problem, engines=["unified_planning"])
    assert result["success"], result.get("error")
    plan = c.decode(result["plan"])
    names = [a for a, _ in plan]
    assert "gather_collect_stone" in names and names[-1] == "give", plan


def test_landmarks_in_a_known_place_need_not_have_been_seen():
    from compiler import Snapshot
    from knowledge import Knowledge, filter_snapshot

    snap = Snapshot()
    snap.objects.update(
        Docks=("Location", {}), Forest=("Location", {}),
        Pip=("Person", {}),
        Deadfall=("Item", {"item_kind": "deadwood", "portable": False}),
        LostCoin=("Item", {"item_kind": "coin", "portable": True}),
    )
    snap.location.update(Pip="Docks", Deadfall="Forest", LostCoin="Forest")
    k = Knowledge(places={"Docks", "Forest"})
    out = filter_snapshot(snap, "Pip", k)
    assert "Deadfall" in out.objects and out.location["Deadfall"] == "Forest", "a fixed landmark of a known place"
    assert "LostCoin" not in out.objects, "loose things still have to be seen"


def test_gathering_spots_regrow_even_in_tiny_steps():
    from item_scripts import _run_tick_op

    class World:
        props = {"stock": 0}

    import action

    old_get, old_set = action.get_property, action.set_property
    action.get_property = lambda w, item, label, prop: World.props.get(prop)
    action.set_property = lambda w, item, label, prop, v: World.props.__setitem__(prop, v)
    try:
        rule = {"op": "add_prop", "prop": "stock", "delta_per_sec": 0.015, "max": 4}
        for _ in range(400):  # 400 ticks of 0.25 s = 100 sim seconds
            _run_tick_op(World(), "Bush", rule, 0.25)
        assert 1.4 < float(World.props["stock"]) < 1.6, World.props
    finally:
        action.get_property, action.set_property = old_get, old_set
