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


def _overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def _touch(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    side = (ax + aw == bx or bx + bw == ax) and ay < by + bh and by < ay + ah
    top = (ay + ah == by or by + bh == ay) and ax < bx + bw and bx < ax + aw
    return side or top


def test_camp_uses_the_hand_laid_map(camp):
    from spatial import region_areas

    areas = region_areas(camp)
    assert areas["CampfireClearing"] == (20, 13, 20, 14)
    assert areas["Forest"] == (0, 10, 20, 20)
    names = sorted(areas)
    assert not any(_overlap(areas[a], areas[b]) for i, a in enumerate(names) for b in names[i + 1:])


def test_everyone_and_everything_has_a_place(camp):
    from spatial import entity_world_cells, person_region_name, region_areas, region_at

    areas = region_areas(camp)
    for p in camp.list_entities("Person"):
        (cell,) = entity_world_cells(camp, p)
        assert region_at(areas, cell) == person_region_name(camp, p), p
    for row in camp.run_query("MATCH (i:Item)-[:LOCATED_AT]->(l:Location) RETURN i.name AS i, l.name AS l"):
        for cell in entity_world_cells(camp, row["i"]):
            assert region_at(areas, cell) == row["l"], row


@pytest.mark.parametrize("world_id", ["key_and_door", "sealed_archive", "sick_girlfriend"])
def test_worlds_without_a_map_are_laid_out_automatically(world, world_id):
    from spatial import find_path, region_areas, walkable_cells

    w = _build(world, world_id)
    areas = region_areas(w)
    names = sorted(areas)
    assert len(names) == len(w.list_entities("Location"))
    assert not any(_overlap(areas[a], areas[b]) for i, a in enumerate(names) for b in names[i + 1:])
    walkable = walkable_cells(w, areas)
    for row in w.run_query("MATCH (a:Location)-[:ADJACENT_TO|LOCKED_ADJACENT_TO]->(b:Location) RETURN a.name AS a, b.name AS b"):
        a, b = areas[row["a"]], areas[row["b"]]
        start = next(c for c in sorted(walkable) if _inside(c, a))
        goals = {c for c in walkable if _inside(c, b)}
        assert find_path(walkable, start, goals), (row, "regions should be walkable to each other")


def _inside(cell, area):
    x, y, w, h = area
    return x <= cell[0] < x + w and y <= cell[1] < y + h


def test_paths_cross_the_clearing_and_avoid_the_fire(camp):
    from spatial import entity_world_cells, find_path, region_areas, region_at, walkable_cells

    areas = region_areas(camp)
    walkable = walkable_cells(camp, areas)
    fire = set(entity_world_cells(camp, "Campfire"))
    assert fire and not fire & walkable
    start = (5, 20)
    goals = {c for c in walkable if region_at(areas, c) == "BerryGrove"}
    path = find_path(walkable, start, goals)
    regions = [region_at(areas, c) for c in path]
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
    from spatial import WALK_CELLS_PER_SEC, path_length, person_cell, person_region_name, region_areas, region_at

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
    assert region_at(region_areas(camp), cell) == "CampfireClearing"
    assert person_region_name(camp, "Reed") == "CampfireClearing"


def test_actions_walk_to_their_target_first(camp):
    from action_timing import get_action_duration
    from spatial import REACH, _octile, entity_world_cells, person_cell

    loop = _loop(camp, "Sage")
    sage = loop.get_agent_state("Sage")
    berry = next(r["i"] for r in camp.run_query(
        "MATCH (i:Item)-[:LOCATED_AT]->(:Location {name: 'BerryGrove'}) WHERE i.item_kind = 'berry' RETURN i.name AS i ORDER BY i.name"
    ))
    target = entity_world_cells(camp, berry)[0]
    camp.run_query("MATCH (p:Person {name: 'Sage'}) SET p.x = 41, p.y = 11")  # far corner of the grove
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
    from spatial import entity_world_cells, move_person_freely, person_region_name, region_areas, walkable_cells

    assert person_region_name(camp, "Observer") == "CampfireClearing"
    forest = region_areas(camp)["Forest"]
    spot = next(c for c in sorted(walkable_cells(camp)) if _inside(c, forest))
    out = move_person_freely(camp, "Observer", spot)
    assert out == {"ok": True, "region": "Forest", "changed_region": True}
    assert person_region_name(camp, "Observer") == "Forest"
    assert not move_person_freely(camp, "Observer", (5, 2))["ok"], "outside every region"
    fire = entity_world_cells(camp, "Campfire")[0]
    assert not move_person_freely(camp, "Observer", fire)["ok"], "can't stand inside the fire"


def test_play_state_carries_the_whole_world(camp):
    from character_actions import get_play_state

    view = get_play_state(camp)["world"]
    assert (view["width"], view["height"]) == (60, 40)
    assert {r["name"] for r in view["regions"]} == set(camp.list_entities("Location"))
    oak = next(i for i in view["items"] if i["name"] == "OakTree")
    assert oak["region"] == "Forest" and "actions" in oak
    assert all(p["x"] is not None for p in view["persons"])


def test_who_is_around_is_about_distance_not_regions(camp):
    from routine import people_around
    from spatial import move_person_freely, walkable_cells

    walkable = walkable_cells(camp)
    for name in camp.list_entities("Person"):  # everyone far away first
        camp.run_query("MATCH (p:Person {name: $n}) SET p.x = 59, p.y = 29", n=name)
    assert move_person_freely(camp, "Observer", (21, 20))["ok"]  # clearing, at the forest border
    assert move_person_freely(camp, "Rowan", (18, 20))["ok"]     # forest, 3 cells away
    assert (38, 20) in walkable
    assert move_person_freely(camp, "Ash", (38, 20))["ok"]       # clearing, far side
    around = people_around(camp, "Observer", "CampfireClearing")
    assert "Rowan" in around and "Ash" not in around
