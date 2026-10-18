"""任务线写进真实世界（Neo4j）+ 每拍推进。会清空并重建营地。"""

from __future__ import annotations

import copy

import pytest

from quest import QuestlinePatch, apply_questline, check_progress, read_digest, validate, verify
from test_quest import GOOD


@pytest.fixture
def camp(world):
    from director import build_from_blueprint, load_world

    build_from_blueprint(world, load_world("town_daily_life"), clear=True)
    return world


def _camp_patch() -> QuestlinePatch:
    data = copy.deepcopy(GOOD)
    data["clues"][0]["known_by"] = ["Rowan"]
    for stage, giver in zip(data["stages"], ["Rowan", "Hollis", "Ash"]):
        stage["giver"] = giver
    data["stages"][2]["complete_when"] = [{"op": "has_item", "person": "Ash", "item": "Crowbar"}]
    return QuestlinePatch.from_dict(data)


def test_a_questline_is_checked_against_the_real_world(camp):
    from compiler import load_snapshot

    digest = read_digest(camp)
    patch = _camp_patch()
    assert validate(patch, digest.known()) == []
    result = verify(load_snapshot(camp), patch, digest.player)
    assert result.ok, result.reason


def test_applying_adds_to_the_world_without_clearing_it(camp):
    from spatial import person_cell, terrain_of
    from terrain_gen import links_present

    before = set(camp.list_entities("Location"))
    old = terrain_of(camp)
    old_cells = {n: len(old.cells_of(n)) for n in before}
    out = apply_questline(camp, _camp_patch())
    assert out["quest"] == "TheOldMine"
    assert set(camp.list_entities("Location")) == before | {"OldMine"}
    t = terrain_of(camp)
    assert t.cells_of("OldMine"), "the mine got its own ground"
    assert {n: len(t.cells_of(n)) for n in before} == old_cells, "existing places keep their ground"
    assert frozenset(("OldMine", "Forest")) in links_present(t), "the mine opens onto the forest"
    assert person_cell(camp, "Hollis") is not None
    q = camp.run_query("MATCH (q:Quest {name: 'TheOldMine'}) RETURN q.state AS s, q.objective AS o")[0]
    assert q["s"] == "active" and q["o"].startswith("Find whoever")


def test_stages_advance_reward_and_announce(camp):
    apply_questline(camp, _camp_patch())
    news = []
    assert check_progress(camp, announce=news.append) == []

    camp.run_query(
        "MATCH (a:Person {name: 'Observer'}), (b:Person {name: 'Hollis'}) MERGE (a)-[:KNOWS]->(b)"
    )
    done = check_progress(camp, announce=news.append)
    assert [d["stage"]["id"] for d in done] == ["s1"]
    camp.run_query("MATCH (i:Item {name: 'Strongbox'}) SET i.opened = true")
    done = check_progress(camp, announce=news.append)
    assert [d["stage"]["id"] for d in done] == ["s2"]
    held = camp.run_query("MATCH (:Person {name: 'Observer'})-[:HAS_ITEM]->(i:Item) RETURN i.name AS n")
    assert any(r["n"].startswith("Lantern") for r in held), "stage 2 reward"
    q = camp.run_query("MATCH (q:Quest {name: 'TheOldMine'}) RETURN q.current AS k, q.objective AS o")[0]
    assert q["k"] == 2 and "crowbar" in q["o"].lower()
    assert len(news) == 2


def _overlap(a, b):
    return not (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0] or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])


def _touch(a, b):
    side = (a[0] + a[2] == b[0] or b[0] + b[2] == a[0]) and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
    top = (a[1] + a[3] == b[1] or b[1] + b[3] == a[1]) and a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
    return side or top
