"""战斗、敌对、物品归属（Neo4j：会清空并重建营地）。"""

from __future__ import annotations

import pytest


@pytest.fixture
def camp(world):
    from director import build_from_blueprint, load_world

    build_from_blueprint(world, load_world("town_daily_life"), clear=True)
    return world


def _do(world, action, **params):
    """像玩家一样做一个动作，再结算后果（server._player_consequences 做的事）。"""
    from registry import get_registry
    from relations import consequences

    ok, why = get_registry().execute(world, action, **params)
    assert ok, why
    return consequences(world, next(iter(params.values())), action, params, at=0.0)


def _stand(world, name, region, near=None, *, far_from=None):
    """把人放到某地点的一格上（near：尽量靠近这一格；far_from：尽量离这一格远）。"""
    from spatial import _octile, terrain_of, walkable_cells

    t = terrain_of(world)
    cells = sorted(t.cells_of(region) & walkable_cells(world))
    if near is not None:
        cell = min((c for c in cells if c != near), key=lambda c: (_octile(c, near), c))
    elif far_from is not None:
        cell = max(cells, key=lambda c: (_octile(c, far_from), c))
    else:
        mid = t.anchor(region)
        cell = min(cells, key=lambda c: (_octile(c, mid), c))  # 正中间可能摆着东西（篝火）
    world.run_query(
        """
        MATCH (p:Person {name: $n}) SET p.x = $x, p.y = $y
        WITH p OPTIONAL MATCH (p)-[r:LOCATED_AT]->() DELETE r
        WITH DISTINCT p MATCH (l:Location {name: $l}) MERGE (p)-[:LOCATED_AT]->(l)
        """,
        n=name, x=cell[0], y=cell[1], l=region,
    )
    return cell


def _feel(world, a, b):
    from relations import feeling

    return feeling(world, a, b)


# ---------------------------------------------------------------------
# 归属
# ---------------------------------------------------------------------


def test_what_you_pick_up_or_are_given_is_yours(camp):
    from ownership import owner_of

    _stand(camp, "Observer", "Forest")
    assert owner_of(camp, "Wood1") == ""
    _do(camp, "pick_up", c="Observer", i="Wood1", l="Forest")
    assert owner_of(camp, "Wood1") == "Observer", "finders keepers"
    _stand(camp, "Rowan", "Forest", near=_stand(camp, "Observer", "Forest"))
    _do(camp, "give", giver="Observer", i="Wood1", receiver="Rowan", l="Forest")
    assert owner_of(camp, "Wood1") == "Rowan", "a gift changes hands"


def test_taking_someone_elses_things_is_theft_and_people_remember(camp):
    from ownership import crimes_seen, owner_of, set_owner

    set_owner(camp, "Berry1", "Sage")
    me = _stand(camp, "Observer", "BerryGrove")
    _stand(camp, "Sage", "BerryGrove", near=me)
    before = _feel(camp, "Sage", "Observer")
    out = _do(camp, "pick_up", c="Observer", i="Berry1", l="BerryGrove")
    assert out["theft"] and out["victim"] == "Sage" and "Sage" in out["witnesses"]
    assert owner_of(camp, "Berry1") == "Sage", "stolen goods still belong to the owner"
    assert _feel(camp, "Sage", "Observer") < before
    assert crimes_seen(camp, "Sage", "Observer")[0]["crime"] == "theft"
    # 还回去：好感回来一些
    low = _feel(camp, "Sage", "Observer")
    _do(camp, "give", giver="Observer", i="Berry1", receiver="Sage", l="BerryGrove")
    assert _feel(camp, "Sage", "Observer") > low and owner_of(camp, "Berry1") == "Sage"


def test_nobody_sees_a_theft_through_walls_or_from_far_away(camp):
    from ownership import crimes_seen, set_owner

    set_owner(camp, "Wood2", "Rowan")
    me = _stand(camp, "Observer", "Forest")
    _stand(camp, "Rowan", "Shelter")  # 屋里，隔着墙
    _stand(camp, "Ash", "BerryGrove", far_from=me)
    out = _do(camp, "pick_up", c="Observer", i="Wood2", l="Forest")
    assert out["theft"] and out["witnesses"] == []
    assert not crimes_seen(camp, "Rowan", "Observer")


# ---------------------------------------------------------------------
# 战斗
# ---------------------------------------------------------------------


def test_a_blow_is_power_plus_weapon_minus_armour(camp):
    from combat import stats, strike

    camp.run_query("MATCH (i:Item {name: 'Wood3'}) SET i.damage = 5")
    camp.run_query(
        "MATCH (p:Person {name: 'Ash'}), (i:Item {name: 'Wood3'}) "
        "OPTIONAL MATCH (i)-[r:LOCATED_AT]->() DELETE r MERGE (p)-[:HAS_ITEM]->(i)"
    )
    camp.run_query(
        "MATCH (p:Person {name: 'Sage'}) CREATE (a:Item {name: 'Jerkin', armor: 2, portable: true}) "
        "MERGE (p)-[:HAS_ITEM]->(a)"
    )
    hp = stats(camp, "Sage")["hp"]
    out = strike(camp, "Ash", "Sage", at=1.0)
    assert out["damage"] == 8 + 5 - 2
    assert stats(camp, "Sage")["hp"] == hp - 11


def test_death_drops_everything_and_frees_it(camp):
    from combat import stats, strike
    from ownership import owner_of

    _stand(camp, "Ash", "BerryGrove", near=_stand(camp, "Sage", "BerryGrove"))
    camp.run_query(
        "MATCH (p:Person {name: 'Sage'}), (i:Item {name: 'Berry2'}) "
        "OPTIONAL MATCH (i)-[r:LOCATED_AT]->() DELETE r MERGE (p)-[:HAS_ITEM]->(i) MERGE (p)-[:OWNS]->(i)"
    )
    camp.run_query("MATCH (p:Person {name: 'Sage'}) SET p.hp = 3")
    out = strike(camp, "Ash", "Sage", at=1.0)
    assert out["killed"] and "Berry2" in out["dropped"]
    assert not stats(camp, "Sage")["alive"]
    row = camp.run_query("MATCH (i:Item {name: 'Berry2'})-[:LOCATED_AT]->(l) RETURN l.name AS l, i.x AS x")[0]
    assert row["l"] == "BerryGrove" and row["x"] is not None, "lies on the ground where they fell"
    assert owner_of(camp, "Berry2") == "", "the dead own nothing: lootable"


def test_an_unprovoked_attack_is_a_crime_and_the_victim_fights_back(camp):
    from combat import hostility, react
    from ownership import crimes_seen

    me = _stand(camp, "Observer", "CampfireClearing")
    _stand(camp, "Ash", "CampfireClearing", near=me)
    out = _do(camp, "attack", attacker="Observer", target="Ash", l="CampfireClearing")
    assert out["crime"]
    assert crimes_seen(camp, "Ash", "Observer")[0]["crime"] == "assault"
    assert hostility(camp, "Ash", "Observer", at=0.0) == "attacked me"
    choice = react(camp, "Ash", at=0.0)
    assert choice["kind"] == "attack" and choice["target"] == "Observer"
    assert choice["plan"][-1][0] == "attack"
    # 被打的人还手不算犯罪
    from relations import consequences
    from registry import get_registry

    ok, _ = get_registry().execute(camp, "attack", attacker="Ash", target="Observer", l="CampfireClearing")
    assert ok
    assert not consequences(camp, "Ash", "attack",
                            {"attacker": "Ash", "target": "Observer", "l": "CampfireClearing"}, at=0.0)["crime"]


def test_the_losing_side_runs_away(camp):
    from combat import react, strike

    here = _stand(camp, "Reed", "CampfireClearing")
    _stand(camp, "Rowan", "CampfireClearing", near=here)
    camp.run_query("MATCH (p:Person {name: 'Reed'}) SET p.hp = 20")
    strike(camp, "Rowan", "Reed", at=1.0)
    choice = react(camp, "Reed", at=1.0)
    assert choice["kind"] == "flee" and choice["from"] == "Rowan"
    assert choice["to"] != "CampfireClearing" and choice["plan"][0][0] == "move"


def test_guards_go_after_criminals_and_monsters_attack_outsiders(camp):
    from combat import hostility, react
    from ownership import set_owner

    camp.run_query("MATCH (p:Person {name: 'Rowan'}) SET p.guard = true")
    set_owner(camp, "Berry3", "Sage")
    me = _stand(camp, "Observer", "BerryGrove")
    _stand(camp, "Rowan", "BerryGrove", near=me)
    _do(camp, "pick_up", c="Observer", i="Berry3", l="BerryGrove")
    assert hostility(camp, "Rowan", "Observer") == "criminal"
    assert react(camp, "Rowan", at=0.0)["target"] == "Observer"

    camp.run_query("MATCH (p:Person {name: 'Reed'}) SET p.faction = 'monster'")
    assert hostility(camp, "Reed", "Sage") == "faction" and hostility(camp, "Sage", "Reed") == "faction"


def test_npcs_drop_what_they_are_doing_when_attacked(camp):
    from agent_profile import AgentProfile
    from game_loop import GameLoop

    loop = GameLoop(camp, verbose=False)
    loop.force_mock = True
    for n in ("Ash",):
        loop.add_agent(AgentProfile.from_world(camp, n), force_mock=True)
    ash = loop.get_agent_state("Ash")
    me = _stand(camp, "Observer", "CampfireClearing")
    _stand(camp, "Ash", "CampfireClearing", near=me)
    ash.current_plan, ash.plan_index = [("wait", ["Ash"])], 0
    loop._start_next_step(ash)
    assert ash.pending_action is not None and ash.pending_action.name == "wait"
    loop.sim_time += 0.1
    from registry import get_registry

    ok, _ = get_registry().execute(camp, "attack", attacker="Observer", target="Ash", l="CampfireClearing")
    assert ok
    camp.run_query("MATCH (p:Person {name: 'Ash'}) SET p.attacked_at = $t", t=loop.sim_time)
    loop.tick(0.05, auto_plan=True)
    assert (ash.last_goal or {}).get("source") == "combat"
    assert ash.pending_action is not None and ash.pending_action.name == "attack"
    loop.close()


def test_hp_comes_back_when_nobody_is_fighting(camp):
    from combat import COMBAT_MEMORY, REGEN, regen, stats

    camp.run_query("MATCH (p:Person {name: 'Reed'}) SET p.hp = 50, p.max_hp = 100, p.attacked_at = 0.0")
    regen(camp, at=1.0)
    assert stats(camp, "Reed")["hp"] == 50, "still in a fight: no healing"
    regen(camp, at=COMBAT_MEMORY + 1.0)
    assert stats(camp, "Reed")["hp"] == 50 + REGEN


def test_someone_killed_on_the_road_lies_where_they_fell(camp):
    """路走到一半被打死：倒在走到的地方，东西掉在身边；之后也不会被挪回出发点。"""
    from agent_profile import AgentProfile
    from combat import stats, strike
    from game_loop import GameLoop
    from spatial import entity_world_cells, person_cell, region_at

    loop = GameLoop(camp, verbose=False)
    loop.force_mock = True
    loop.add_agent(AgentProfile.from_world(camp, "Reed"), force_mock=True)
    reed = loop.get_agent_state("Reed")
    start = _stand(camp, "Reed", "RiverBank")
    camp.run_query(
        "MATCH (p:Person {name: 'Reed'}), (i:Item {name: 'Wood1'}) "
        "OPTIONAL MATCH (i)-[r:LOCATED_AT]->() DELETE r MERGE (p)-[:HAS_ITEM]->(i)"
    )
    reed.current_plan, reed.plan_index = [("move", ["Reed", "RiverBank", "CampfireClearing"])], 0
    loop._start_next_step(reed)
    pending = reed.pending_action
    assert pending.path and len(pending.path) > 4
    half = pending.started_at + pending.walk_seconds / 2
    loop.sim_time = half
    camp.run_query("MATCH (p:Person {name: 'Reed'}) SET p.hp = 1")
    out = strike(camp, "Rowan", "Reed", at=half)
    assert out["killed"]
    fell = person_cell(camp, "Reed")
    assert fell != start and fell in [tuple(c) for c in pending.path[1:-1]], "halfway along the road"
    assert region_at(camp, fell) == camp.run_query(
        "MATCH (:Person {name: 'Reed'})-[:LOCATED_AT]->(l) RETURN l.name AS l")[0]["l"]
    wood = entity_world_cells(camp, "Wood1")[0]
    assert max(abs(wood[0] - fell[0]), abs(wood[1] - fell[1])) <= 3, "dropped beside the body"
    loop.tick(0.25, auto_plan=True)  # 下一拍：死人的动作被取消
    assert reed.pending_action is None and not stats(camp, "Reed")["alive"]
    assert person_cell(camp, "Reed") == fell, "the body stays put"
    loop.close()
