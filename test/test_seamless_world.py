"""无缝世界：所有区域在同一张世界格子上；人有坐标，走路要时间，跨区域就是走过去。"""

from __future__ import annotations

import pytest


def _build(world, world_id):
    from director import build_from_blueprint, load_world

    build_from_blueprint(world, load_world(world_id), clear=True)
    return world


@pytest.fixture
def camp(world):
    return _build(world, "town_daily_life")


def _center(terrain, name):
    cells = terrain.cells_of(name)
    return (sum(c[0] for c in cells) / len(cells), sum(c[1] for c in cells) / len(cells))


def test_camp_keeps_the_hand_laid_arrangement(camp):
    from spatial import terrain_of

    t = terrain_of(camp)
    clearing, forest, grove, river = (_center(t, n) for n in ("CampfireClearing", "Forest", "BerryGrove", "RiverBank"))
    assert forest[0] < clearing[0] < grove[0], "forest to the west, berries to the east"
    assert river[1] > clearing[1], "the river bank to the south"
    # 窝棚是空地上的一间屋子：有墙，挨着空地开门
    assert "Shelter" in t.indoor and t.parents["Shelter"] == "CampfireClearing"
    assert t.doorways_between("Shelter", "CampfireClearing")


def test_everyone_and_everything_has_a_place(camp):
    from spatial import entity_world_cells, person_region_name, region_at

    for p in camp.list_entities("Person"):
        (cell,) = entity_world_cells(camp, p)
        assert region_at(camp, cell) == person_region_name(camp, p), p
    for row in camp.run_query("MATCH (i:Item)-[:LOCATED_AT]->(l:Location) RETURN i.name AS i, l.name AS l"):
        for cell in entity_world_cells(camp, row["i"]):
            assert region_at(camp, cell) == row["l"], row


@pytest.mark.parametrize("world_id", ["key_and_door", "sealed_archive", "sick_girlfriend", "adventurer_town"])
def test_worlds_without_a_map_are_laid_out_automatically(world, world_id):
    from spatial import find_path, region_areas, terrain_of, walkable_cells

    w = _build(world, world_id)
    t = terrain_of(w)
    areas = region_areas(w)
    assert set(areas) == set(w.list_entities("Location"))
    walkable = walkable_cells(w, areas)
    for row in w.run_query("MATCH (a:Location)-[:ADJACENT_TO]->(b:Location) RETURN a.name AS a, b.name AS b"):
        start = t.anchor(row["a"])
        goals = t.cells_of(row["b"]) & walkable
        assert find_path(walkable, start, goals), (row, "adjacent places should be walkable to each other")


def test_paths_cross_the_clearing_and_avoid_the_fire(camp):
    from spatial import entity_world_cells, find_path, region_at, terrain_of, walkable_cells

    walkable = walkable_cells(camp)
    fire = set(entity_world_cells(camp, "Campfire"))
    assert fire and not fire & walkable
    start = terrain_of(camp).anchor("Forest")
    goals = {c for c in walkable if region_at(camp, c) == "BerryGrove"}
    path = find_path(walkable, start, goals)
    regions = [region_at(camp, c) for c in path]
    assert "CampfireClearing" in regions and not fire & set(path)


def _loop(world, *names):
    from agent_profile import AgentProfile
    from game_loop import GameLoop

    loop = GameLoop(world, verbose=False)
    loop.force_mock = True
    for n in names:
        loop.add_agent(AgentProfile.from_world(world, n), force_mock=True)
    return loop


def _finish(loop, state):
    while state.pending_action is not None:
        loop.tick(0.25, auto_plan=False)


def test_walking_takes_time_and_ends_in_the_new_region(camp):
    from spatial import WALK_CELLS_PER_SEC, path_length, person_cell, person_region_name, region_at

    loop = _loop(camp, "Reed")
    reed = loop.get_agent_state("Reed")
    start_region = person_region_name(camp, "Reed")
    reed.current_plan, reed.plan_index = [("move", ["Reed", start_region, "CampfireClearing"])], 0
    started = loop._start_next_step(reed)
    pending = reed.pending_action
    assert pending.path and started["duration"] == pytest.approx(
        max(0.3, path_length(pending.path) / WALK_CELLS_PER_SEC)
    )
    assert started["duration"] > 1.0, "crossing most of a region is not instant"
    _finish(loop, reed)
    loop.close()
    cell = person_cell(camp, "Reed")
    assert region_at(camp, cell) == "CampfireClearing"
    assert person_region_name(camp, "Reed") == "CampfireClearing"


def test_actions_walk_to_their_target_first(camp):
    from action_timing import get_action_duration
    from spatial import REACH, _octile, entity_world_cells, person_cell, terrain_of, walkable_cells

    loop = _loop(camp, "Sage")
    sage = loop.get_agent_state("Sage")
    berry = next(r["i"] for r in camp.run_query(
        "MATCH (i:Item)-[:LOCATED_AT]->(:Location {name: 'BerryGrove'}) WHERE i.item_kind = 'berry' RETURN i.name AS i ORDER BY i.name"
    ))
    target = entity_world_cells(camp, berry)[0]
    t = terrain_of(camp)
    far = max(t.cells_of("BerryGrove") & walkable_cells(camp), key=lambda c: (_octile(c, target), c))
    camp.run_query("MATCH (p:Person {name: 'Sage'}) SET p.x = $x, p.y = $y", x=far[0], y=far[1])  # far side of the grove
    sage.current_plan, sage.plan_index = [("pick_up", ["Sage", berry, "BerryGrove"])], 0
    started = loop._start_next_step(sage)
    assert started["duration"] > get_action_duration("pick_up")
    path_end = sage.pending_action.path[-1]
    assert _octile(path_end, target) <= REACH
    _finish(loop, sage)
    loop.close()
    assert sage.last_action_result.success
    assert person_cell(camp, "Sage") == tuple(path_end)


def test_the_player_walks_between_regions_freely(camp):
    from spatial import entity_world_cells, move_person_freely, person_region_name, terrain_of, walkable_cells

    t = terrain_of(camp)
    assert person_region_name(camp, "Observer") == "CampfireClearing"
    spot = sorted(t.cells_of("Forest") & walkable_cells(camp))[0]
    out = move_person_freely(camp, "Observer", spot)
    assert out == {"ok": True, "region": "Forest", "changed_region": True}
    assert person_region_name(camp, "Observer") == "Forest"
    assert not move_person_freely(camp, "Observer", (0, 0))["ok"], "outside every region"
    wall = next((x, y) for y in range(t.height) for x in range(t.width) if t.kind_at((x, y)) == "#")
    assert not move_person_freely(camp, "Observer", wall)["ok"], "can't stand in a wall"
    fire = entity_world_cells(camp, "Campfire")[0]
    assert not move_person_freely(camp, "Observer", fire)["ok"], "can't stand inside the fire"


def test_play_state_carries_the_whole_world(camp):
    from character_actions import get_play_state

    from spatial import terrain_of

    view = get_play_state(camp)["world"]
    t = terrain_of(camp)
    assert (view["width"], view["height"]) == (t.width, t.height)
    assert len(view["tiles"]) == t.height and "#" in "".join(view["tiles"]), "walls go to the client"
    assert {r["name"] for r in view["regions"]} == set(camp.list_entities("Location"))
    shelter = next(r for r in view["regions"] if r["name"] == "Shelter")
    assert shelter["indoor"] and shelter["parent"] == "CampfireClearing" and shelter["cx"] is not None
    oak = next(i for i in view["items"] if i["name"] == "OakTree")
    assert oak["region"] == "Forest" and "actions" in oak
    assert all(p["x"] is not None for p in view["persons"])


def test_who_is_around_is_about_distance_and_walls(camp):
    from routine import people_around
    from spatial import _octile, move_person_freely, terrain_of, walkable_cells

    t = terrain_of(camp)
    walkable = walkable_cells(camp)
    clearing, forest = t.cells_of("CampfireClearing") & walkable, t.cells_of("Forest") & walkable
    shelter = t.cells_of("Shelter") & walkable
    here, near = min((c, f) for c in clearing for f in forest if 2 <= _octile(c, f) <= 4)
    far = max(clearing, key=lambda c: (_octile(c, here), c))
    for name in camp.list_entities("Person"):  # everyone far away first
        camp.run_query("MATCH (p:Person {name: $n}) SET p.x = $x, p.y = $y", n=name, x=far[0], y=far[1])
    assert move_person_freely(camp, "Observer", here)["ok"]   # clearing, at the forest border
    assert move_person_freely(camp, "Rowan", near)["ok"]      # forest, a few cells away
    assert move_person_freely(camp, "Ash", far)["ok"]         # clearing, far side
    around = people_around(camp, "Observer", "CampfireClearing")
    assert "Rowan" in around and "Ash" not in around

    # 隔着墙：窝棚里离得近的人看不见
    door = t.doorways_between("Shelter", "CampfireClearing")[0].cell
    outside = min(clearing, key=lambda c: (_octile(c, door), c))
    inside = min((c for c in shelter if c != door), key=lambda c: (_octile(c, outside), c))
    assert move_person_freely(camp, "Observer", outside)["ok"]
    camp.run_query("MATCH (p:Person {name: 'Rowan'}) SET p.x = $x, p.y = $y", x=inside[0], y=inside[1])
    camp.run_query("""MATCH (p:Person {name: 'Rowan'})-[r:LOCATED_AT]->() DELETE r WITH p
                      MATCH (l:Location {name: 'Shelter'}) MERGE (p)-[:LOCATED_AT]->(l)""")
    assert "Rowan" not in people_around(camp, "Observer", "CampfireClearing"), "walls hide people indoors"


# ---------------------------------------------------------------------
# 连通关系以地图为准
# ---------------------------------------------------------------------


def _edges(world, rel="ADJACENT_TO"):
    rows = world.run_query(
        f"MATCH (a:Location)-[:{rel}]-(b:Location) WHERE a.name < b.name RETURN a.name AS a, b.name AS b"
    )
    return {frozenset((r["a"], r["b"])) for r in rows}


@pytest.mark.parametrize("world_id", ["key_and_door", "sealed_archive", "town_daily_life", "adventurer_town",
                                      "market_town", "frontier_village", "riverford"])
def test_the_graph_says_what_the_map_says(world, world_id):
    from spatial import locked_door_cells, terrain_of
    from terrain_gen import links_present

    w = _build(world, world_id)
    t = terrain_of(w)
    closed = locked_door_cells(w, t)
    assert _edges(w) == links_present(t, closed)
    assert _edges(w, "LOCKED_ADJACENT_TO") == links_present(t) - links_present(t, closed)


def test_unlocking_a_door_opens_the_way(world):
    from graph_ops import apply_all, make_ctx

    w = _build(world, "key_and_door")
    pair = frozenset(("Hall", "Vault"))
    assert pair in _edges(w, "LOCKED_ADJACENT_TO") and pair not in _edges(w)
    apply_all(w, [{"op": "set_prop", "entity": "VaultDoor", "label": "Item", "prop": "locked", "value": False}],
              make_ctx(actor="", tool="", target=""))
    assert pair in _edges(w) and pair not in _edges(w, "LOCKED_ADJACENT_TO")


def test_a_wall_cuts_places_apart_and_a_doorway_joins_them(camp):
    from spatial import edit_terrain, find_path, terrain_of, walkable_cells

    t = terrain_of(camp)
    pair = frozenset(("CampfireClearing", "Forest"))
    forest = t.cells_of("Forest")
    border = sorted(c for c in t.cells_of("CampfireClearing")
                    if any((c[0] + dx, c[1] + dy) in forest for dx in (-1, 0, 1) for dy in (-1, 0, 1)))
    out = edit_terrain(camp, {c: ("#", None) for c in border})
    assert pair in {frozenset(p) for p in out["removed"]} and pair not in _edges(camp)
    walk = walkable_cells(camp)
    assert not find_path(walk, t.anchor("Forest"), terrain_of(camp).cells_of("CampfireClearing") & walk) or \
        any(p for p in _edges(camp) if "Forest" in p), "only a detour through other places, if any"
    gate = next(c for c in border for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                if (c[0] + dx, c[1] + dy) in forest
                and terrain_of(camp).owner_at((c[0] - dx, c[1] - dy)) == "CampfireClearing")
    edit_terrain(camp, {gate: ("+", "CampfireClearing")})
    assert pair in _edges(camp)


def test_scripts_that_link_or_unlink_places_change_the_map(camp):
    from graph_ops import apply_all, make_ctx
    from spatial import find_path, terrain_of, walkable_cells

    ctx = make_ctx(actor="", tool="", target="")
    pair = frozenset(("Forest", "BerryGrove"))
    assert pair not in _edges(camp)
    apply_all(camp, [{"op": "link", "a": "Forest", "b": "BerryGrove", "rel": "ADJACENT_TO", "bidirectional": True}], ctx)
    t, walk = terrain_of(camp), walkable_cells(camp)
    assert pair in _edges(camp), "a passage was cut on the map"
    direct = walk & (t.cells_of("Forest") | t.cells_of("BerryGrove"))
    assert find_path(direct, t.anchor("Forest"), t.cells_of("BerryGrove") & walk), "a way through without the clearing"
    apply_all(camp, [{"op": "unlink", "a": "Forest", "b": "BerryGrove", "rel": "ADJACENT_TO", "bidirectional": True}], ctx)
    assert pair not in _edges(camp)
    # 图上随手加的边，地图上不通就会被同步掉
    camp.run_query("MATCH (a:Location {name: 'Shelter'}), (b:Location {name: 'RiverBank'}) MERGE (a)-[:ADJACENT_TO]->(b)")
    from spatial import sync_adjacency

    sync_adjacency(camp)
    assert frozenset(("Shelter", "RiverBank")) not in _edges(camp)


def test_people_spread_out_instead_of_piling_up_in_the_middle(camp):
    """所有人都去空地：各站各的，不叠在一起，也不都挤在正中间。"""
    from spatial import _octile, person_cell, plan_walk, start_walk, terrain_of

    t = terrain_of(camp)
    spots = []
    for name in camp.list_entities("Person"):
        here = camp.run_query("MATCH (:Person {name: $n})-[:LOCATED_AT]->(l) RETURN l.name AS l", n=name)[0]["l"]
        walk = plan_walk(camp, name, "move", [name, here, "CampfireClearing"])
        if walk is None:
            continue
        start_walk(camp, name, walk.path, 0.0, walk.seconds)  # 别人看得见他要去哪
        spots.append(walk.dest)
    assert len(spots) >= 3
    assert len(set(spots)) == len(spots), "nobody on the same cell"
    assert all(_octile(a, b) >= 2 for i, a in enumerate(spots) for b in spots[i + 1:]), "not even side by side"
    mid = t.anchor("CampfireClearing")
    assert max(_octile(s, mid) for s in spots) >= 2, "spread out, not all at the centre"


def test_npcs_say_a_short_line_when_they_start_something():
    from mutter import action_line

    assert action_line("Ash", "move", ["Ash", "Forest", "BerryGrove"]) in {
        "Off to BerryGrove.", "Right, BerryGrove next.", "Better get to BerryGrove.", "Heading for BerryGrove."}
    for _ in range(10):
        line = action_line("Ash", "pick_up", ["Ash", "Berry2", "BerryGrove"])
        assert "berry" in line or line == "Mine now.", line          # "Berry2" is said as "berry"
        line = action_line("Ash", "attack", ["Ash", "Hale", "TownSquare"])
        assert "Hale" in line or line == "Enough!", line
    assert action_line("Ash", "move", ["Ash", "Forest", "Meadow"], goal="Flee") in {
        "I'm getting out of here!", "Not today!", "Run!"}
    assert action_line("Ash", "talk", ["Ash", "Hale", "TownSquare"]) == "", "they'll talk anyway"
    assert action_line("Ash", "wait", ["Ash"]) == ""


def test_gathering_and_crafting_lines_read_like_speech():
    from mutter import _doing_label

    assert _doing_label("gather_gather_sticks") == "gather dry sticks"
    assert _doing_label("gather_fell_log") == "fell a log", "no tool hint in brackets"
    assert _doing_label("craft_stone_axe") == "make a stone axe"
    assert _doing_label("craft_cook_fish") == "cook fish"


def test_npcs_remember_what_they_already_told_you():
    """之前对话里说过的话进对话 prompt：别每次都从头说一遍。"""
    import dialogue_service
    from game_loop import GameLoop

    class _NoWorld:
        def run_query(self, *a, **k):
            return []

    loop = GameLoop.__new__(GameLoop)
    loop.conversations = [
        {"a": "Rookie", "b": "Pip", "lines": [{"speaker": "Rookie", "text": "Hi"},
                                              {"speaker": "Pip", "text": "I need wood for a fishing rod."}]},
        {"a": "Pip", "b": "Hale", "lines": [{"speaker": "Pip", "text": "Morning, Hale."}]},
    ]
    assert loop.said_to("Pip", "Rookie") == ["I need wood for a fishing rod."]
    old = dialogue_service.EARLIER_LINES
    try:
        dialogue_service.EARLIER_LINES = loop.said_to
        block = dialogue_service.earlier_block("Pip", "Rookie")
        assert "I need wood for a fishing rod." in block and "Don't repeat" in block
        assert "Morning, Hale" not in block, "only what was said to this listener"
    finally:
        dialogue_service.EARLIER_LINES = old


def test_working_tools_are_not_given_away_as_gifts(world):
    from director import build_from_blueprint, load_world
    from goal_planner import _maybe_append_give
    from registry import act

    build_from_blueprint(world, load_world("riverford"), clear=True)
    world.run_query("MATCH (p:Person {name:'Pip'}) CREATE (r:Item {name:'TestRod', item_kind:'rod', portable:true}) "
                    "MERGE (p)-[:HAS_ITEM]->(r)")
    plan = _maybe_append_give(world, "Pip", "Hale", [(act("converse"), ["Pip", "Hale", "RiverDocks"])])
    assert not any(step[0] == act("give") and "TestRod" in step[1] for step in plan), plan


def test_having_it_already_is_not_being_stuck(camp):
    """日程要「采一个浆果」、手上已经有了：不算卡住，不挂委托。"""
    from agent_profile import AgentProfile
    from game_loop import REQUEST_AFTER, GameLoop

    camp.run_query(
        "MATCH (p:Person {name: 'Sage'}), (i:Item {name: 'Berry1'}) "
        "OPTIONAL MATCH (i)-[r:LOCATED_AT]->() DELETE r MERGE (p)-[:HAS_ITEM]->(i)"
    )
    loop = GameLoop(camp, verbose=False)
    loop.force_mock = True
    loop.add_agent(AgentProfile.from_world(camp, "Sage"), force_mock=True)
    sage = loop.get_agent_state("Sage")
    wanted = {"goal": "Collect item", "target_entity": "kind:berry", "reason": "Routine: pick berries"}
    for _ in range(REQUEST_AFTER + 2):
        loop._note_progress(sage, wanted, None)
    assert sage.stuck_count == 0
    assert not camp.run_query("MATCH (r:Request) RETURN r LIMIT 1") and not any(
        "Posted a request" in (e.get("text") or "") for e in sage.history)
    loop.close()
