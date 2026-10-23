"""地形：地点像真实地图那样彼此咬合、房子嵌在地里，墙是障碍物；相邻的地点之间才走得通。

纯几何（terrain_gen）不碰数据库；最后一个测试用假世界检查 spatial 的门 / 墙逻辑。
"""

from __future__ import annotations

import json
import random
from collections import deque
from pathlib import Path

import pytest

from terrain_gen import (
    DOOR, FLOOR, GROUND, VOID, WALL, WALKABLE, Place, Terrain, extend, from_ascii, generate, links_present,
)

WORLDS = Path(__file__).resolve().parents[1] / "worlds"


def _world_spec(world_id):
    """和 spatial.layout_regions 一样，从 world JSON 得到地点和连接关系。"""
    data = json.loads((WORLDS / f"{world_id}.json").read_text(encoding="utf-8"))
    places = []
    for loc in data["locations"]:
        area = loc.get("area")
        size = tuple(loc.get("grid") or (area[2:] if area else (20, 12)))
        places.append(Place(
            loc["name"], size, bool(loc.get("indoor")), loc.get("parent", ""),
            seed=(area[0] + area[2] / 2, area[1] + area[3] / 2) if area else None,
            map=tuple(loc["map"]) if loc.get("map") else None,
        ))
    links, side = {}, {}
    for e in data.get("adjacencies", []):
        links[frozenset((e["a"], e["b"]))] = "door" if e.get("locked") else "open"
    for it in data.get("items", []):
        if (it.get("item_kind") or "").lower() == "door":
            other = it.get("connects_to") or ""
            if not other:  # 钥匙效果里连的那两处
                for item in data.get("items", []):
                    for eff in item.get("effects_on") or []:
                        if eff.get("target") == it["name"]:
                            for op in eff.get("ops") or []:
                                if op.get("op") == "link" and op.get("rel") == "ADJACENT_TO":
                                    a, b = op.get("a"), op.get("b")
                                    other = b if a == it["located_at"] else a
            if other:
                p = frozenset((it["located_at"], other))
                links[p] = "door"
                side[p] = it["located_at"]
    return places, links, side


def _reachable(names, edges):
    seen, queue = {names[0]}, deque([names[0]])
    while queue:
        cur = queue.popleft()
        for e in edges:
            if cur in e:
                for other in e - {cur}:
                    if other not in seen:
                        seen.add(other)
                        queue.append(other)
    return seen


@pytest.mark.parametrize("world_id", sorted(p.stem for p in WORLDS.glob("*.json")))
def test_the_map_connects_exactly_what_the_graph_connects(world_id):
    places, links, side = _world_spec(world_id)
    data = json.loads((WORLDS / f"{world_id}.json").read_text(encoding="utf-8"))
    if data.get("terrain"):
        # 手画地图的世界不走自动布局：检查画出来的地图本身
        raw = data["terrain"]
        parents = {p.name: p.parent for p in places if p.indoor}
        t = from_ascii(raw["rows"], raw["legend"], parents.keys(), parents, decor=raw.get("decor"))
    else:
        t = generate(places, links, side)
    for p in places:
        assert t.cells_of(p.name), f"{p.name} has ground"
    assert links_present(t) == set(links), "every link is a real passage; no other place touches without a wall"


def test_outdoor_places_interlock_instead_of_being_rectangles():
    places, links, side = _world_spec("frontier_village")
    t = generate(places, links, side)
    for p in places:
        x, y, w, h = t.bbox(p.name)
        assert len(t.cells_of(p.name)) < 0.85 * w * h, f"{p.name} is not a rectangle"
    # 相邻的两块地共享一段弯弯曲曲的边界，不是只在一个缺口处相接
    village = t.cells_of("Village")
    for other in ("Forest", "Quarry", "Field"):
        border = {c for c in t.cells_of(other)
                  if any((c[0] + dx, c[1] + dy) in village for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))}
        assert len(border) >= 3, other
        assert len({c[0] for c in border}) > 1 and len({c[1] for c in border}) > 1, f"{other}: border isn't a straight line"


def test_rooms_have_walls_and_sit_inside_their_parent():
    places, links, side = _world_spec("adventurer_town")
    t = generate(places, links, side)
    town = t.cells_of("TownSquare")
    for room in ("GuildHall", "Crypt", "Tavern", "Smithy"):
        assert room in t.indoor and t.parents[room] == "TownSquare"
        cells = t.cells_of(room)
        assert all(t.kind_at(c) in (FLOOR, DOOR) for c in cells)
        for x, y in cells:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (x + dx, y + dy)
                if n in cells:
                    continue
                # 房间的边上不是墙就是门洞（门洞那一格可能算在门外那块地上）
                assert t.kind_at(n) in (WALL, DOOR) or t.kind_at((x, y)) == DOOR, (room, (x, y), n)
    # 地下墓穴在公会大厅里面：只能从大厅那扇门进
    assert frozenset(("Crypt", "TownSquare")) not in links_present(t)
    assert [d.other for d in t.doorways if d.owner == "GuildHall" and d.other == "Crypt"]
    # 房子嵌在镇上：房子的墙外头大多是镇上的地
    walls = {(x, y) for y in range(t.height) for x in range(t.width) if t.kind_at((x, y)) == WALL}
    outside = {(x + dx, y + dy) for x, y in walls for dx in (-1, 0, 1) for dy in (-1, 0, 1)
               if t.kind_at((x + dx, y + dy)) == GROUND}
    assert sum(1 for c in outside if c in town) > 0.6 * len(outside)


def test_the_same_world_always_gets_the_same_map():
    places, links, side = _world_spec("market_town")
    a, b = generate(places, links, side), generate(places, links, side)
    assert a.kind_rows() == b.kind_rows() and a.to_props() == b.to_props()


def test_terrain_survives_a_round_trip_through_the_database():
    places, links, side = _world_spec("sealed_archive")
    t = generate(places, links, side)
    back = Terrain.from_props(t.to_props())
    assert back.kind_rows() == t.kind_rows()
    assert back.cells_by_region() == t.cells_by_region()
    assert back.indoor == t.indoor and back.parents == t.parents
    assert [(d.cell, d.owner, d.other) for d in back.doorways] == [(d.cell, d.owner, d.other) for d in t.doorways]


def test_new_places_grow_next_to_the_map_without_moving_anything():
    places, links, side = _world_spec("town_daily_life")
    t = generate(places, links, side)
    links2 = dict(links)
    links2[frozenset(("OldMine", "Forest"))] = "open"
    links2[frozenset(("MineHut", "OldMine"))] = "open"
    more = places + [Place("OldMine", (18, 12)), Place("MineHut", (8, 6), indoor=True, parent="OldMine")]
    t2, (dx, dy) = extend(t, more, links2, side)
    for name in t.names:
        assert {(x + dx, y + dy) for x, y in t.cells_of(name)} == t2.cells_of(name), f"{name} unchanged"
    assert t2.cells_of("OldMine") and t2.cells_of("MineHut")
    assert {frozenset(("OldMine", "Forest")), frozenset(("MineHut", "OldMine"))} <= links_present(t2)
    assert links_present(t2) - links_present(t) <= set(links2) | {frozenset(p) for p in [("OldMine", "Forest")]}


def test_hand_drawn_maps():
    rows = [
        "VVVVVVVVVV",
        "VV#####VVV",
        "VV#ttt#VVV",
        "VV#ttt+VVV",
        "VV#####VVV",
        "VVVV%%%%%%",
        "VVVV%FFFFF",
    ]
    t = from_ascii(rows, {"V": "Village", "t": "Tavern", "F": "Field"}, indoor=["Tavern"], parents={"Tavern": "Village"})
    assert t.kind_at((4, 2)) == FLOOR and t.kind_at((2, 2)) == WALL and t.kind_at((0, 0)) == GROUND
    (door,) = t.doorways
    assert door.cell == (6, 3) and door.owner == "Tavern" and door.other == "Village"
    assert links_present(t) == {frozenset(("Tavern", "Village"))}, "the hedge keeps the field apart"


def _random_world(seed):
    rnd = random.Random(seed)
    outs = [f"O{i}" for i in range(rnd.randint(1, 6))]
    ins = [f"I{i}" for i in range(rnd.randint(0, 4))]
    links = {}
    for i in range(1, len(outs)):
        links[frozenset((outs[i], outs[rnd.randrange(i)]))] = "open"
    for j, r in enumerate(ins):
        if j and rnd.random() < 0.4:
            links[frozenset((r, ins[rnd.randrange(j)]))] = rnd.choice(["open", "door"])
        else:
            links[frozenset((r, rnd.choice(outs)))] = rnd.choice(["open", "door"])
    places = [Place(o, (rnd.randint(12, 26), rnd.randint(9, 16))) for o in outs]
    places += [Place(r, (rnd.randint(8, 20), rnd.randint(6, 12)), indoor=True) for r in ins]
    return places, links


@pytest.mark.parametrize("seed", range(40))
def test_random_worlds_are_fully_walkable(seed):
    places, links = _random_world(seed)
    t = generate(places, links)
    names = [p.name for p in places]
    assert all(t.cells_of(n) for n in names)
    assert _reachable(names, links_present(t)) == _reachable(names, set(links)), "everyone can get everywhere"
    for c in t.walkable():
        assert t.kind_at(c) in WALKABLE and t.owner_at(c) is not None
    assert all(t.kind_at((x, 0)) == VOID for x in range(t.width)), "a margin of wilderness around the map"


class DoorWorld:
    """Hall → Vault behind a locked door (the key links them); Storage is a room next to Hall."""

    def __init__(self):
        places = [Place("Hall", indoor=True), Place("Vault", indoor=True), Place("Storage", indoor=True)]
        links = {frozenset(("Hall", "Storage")): "open", frozenset(("Hall", "Vault")): "door"}
        self.terrain = generate(places, links, {frozenset(("Hall", "Vault")): "Hall"})
        self.areas = {n: self.terrain.bbox(n) for n in self.terrain.names}
        self.locked = True
        (way,) = self.terrain.doorways_between("Hall", "Vault")
        hall = self.areas["Hall"]
        self.door_xy = (way.cell[0] - hall[0], way.cell[1] - hall[1])

    def run_query(self, q, **k):
        if "MATCH (t:Terrain) RETURN t.version" in q:
            return [{"v": "door-world"}]
        if "MATCH (t:Terrain) RETURN properties(t)" in q:
            return [{"p": self.terrain.to_props()}]
        if "l.area_x IS NOT NULL" in q:
            return [{"name": n, "x": a[0], "y": a[1], "w": a[2], "h": a[3]} for n, a in self.areas.items()]
        if "toLower(coalesce(d.item_kind" in q:
            return [{"door": "VaultDoor", "here": "Hall", "to": "", "locked": self.locked, "locked_to": []}]
        if "i.effects_on IS NOT NULL" in q:
            return [{"e": '[{"target": "VaultDoor", "ops": [{"op": "link", "rel": "ADJACENT_TO", "a": "Hall", "b": "Vault"}]}]'}]
        if "[:ADJACENT_TO]-(:Location {name: $b})" in q:
            pairs = [{"Hall", "Storage"}] + ([] if self.locked else [{"Hall", "Vault"}])
            return [{"ok": 1}] if {k["a"], k["b"]} in pairs else []
        if "MATCH (i:Item)-[:LOCATED_AT]->(l:Location)" in q and "i.x IS NOT NULL" in q:
            return [{"loc": "Hall", "x": self.door_xy[0], "y": self.door_xy[1], "portable": False, "w": 1, "h": 1,
                     "kind": "door", "locked": self.locked}]
        return []


def test_a_locked_door_is_the_only_way_through_the_wall():
    import spatial
    from spatial import find_path, open_between, walkable_cells

    spatial._TERRAIN_CACHE.clear()
    world = DoorWorld()
    t = world.terrain
    hall, vault, store = (t.anchor(n) for n in ("Hall", "Vault", "Storage"))
    walk = walkable_cells(world)
    assert hall in walk and vault in walk
    assert find_path(walk, hall, {vault}) is None, "locked: no way in"
    assert find_path(walk, hall, {store}), "the storeroom door is open"
    assert not open_between(world, "Hall", "Vault")

    world.locked = False  # the key was used: the door opens, the rooms become adjacent
    walk = walkable_cells(world)
    path = find_path(walk, hall, {vault})
    (way,) = t.doorways_between("Hall", "Vault")
    assert path is not None and way.cell in path, "through the one doorway"
    assert open_between(world, "Hall", "Vault")


# ---------------------------------------------------------------------
# 改地形：连通关系跟着地图走
# ---------------------------------------------------------------------


def test_a_locked_doorway_counts_as_closed():
    places, links, side = _world_spec("key_and_door")
    t = generate(places, links, side)
    (way,) = t.doorways_between("Hall", "Vault")
    assert frozenset(("Hall", "Vault")) in links_present(t)
    assert frozenset(("Hall", "Vault")) not in links_present(t, blocked=[way.cell])


def test_walling_up_a_border_disconnects_and_a_doorway_reconnects():
    from terrain_gen import edit_cells

    places, links, side = _world_spec("frontier_village")
    t = generate(places, links, side)
    pair = frozenset(("Village", "Forest"))
    forest = t.cells_of("Forest")
    border = {c for c in t.cells_of("Village")
              if any((c[0] + dx, c[1] + dy) in forest for dx in (-1, 0, 1) for dy in (-1, 0, 1))}
    walled = edit_cells(t, {c: ("#", None) for c in border})
    assert pair not in links_present(walled)
    assert t.kind_at(next(iter(border))) == GROUND, "the original map is untouched"
    # 墙上开一个门洞：门洞两侧一边是村子、一边是森林
    gate = next(c for c in sorted(border) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                if (c[0] + dx, c[1] + dy) in forest and walled.owner_at((c[0] - dx, c[1] - dy)) == "Village")
    opened = edit_cells(walled, {gate: ("+", "Village")})
    assert pair in links_present(opened)
    assert [(d.owner, d.other) for d in opened.doorways if d.cell == gate] == [("Village", "Forest")]


def test_connect_cuts_a_passage_and_separate_closes_it():
    from terrain_gen import connect, separate

    places, links, side = _world_spec("frontier_village")
    t = generate(places, links, side)
    pair = frozenset(("Forest", "Field"))
    assert pair not in links_present(t)
    joined = connect(t, "Forest", "Field")
    assert pair in links_present(joined)
    others = links_present(t) - {pair}
    assert others <= links_present(joined), "nothing else got cut off"
    assert connect(joined, "Forest", "Field") is joined, "already connected: no change"
    apart = separate(joined, "Forest", "Field")
    assert pair not in links_present(apart)
    # 室内：把公会大厅和地下墓穴之间的门洞砌上
    places, links, side = _world_spec("adventurer_town")
    town = generate(places, links, side)
    sealed = separate(town, "GuildHall", "Crypt")
    assert frozenset(("GuildHall", "Crypt")) not in links_present(sealed)
    assert not sealed.doorways_between("GuildHall", "Crypt")


def test_riverford_is_drawn_by_hand_and_renders():
    """手画的 Riverford：城里的房子都朝街开门，过河只能走桥，墓穴只能从公会大厅进；美术图画得出来。"""
    from terrain_art import render

    data = json.loads((WORLDS / "riverford.json").read_text(encoding="utf-8"))
    raw = data["terrain"]
    indoor = {l["name"]: l["parent"] for l in data["locations"] if l.get("indoor")}
    t = from_ascii(raw["rows"], raw["legend"], indoor.keys(), indoor, decor=raw["decor"])
    links = links_present(t)
    assert links == {frozenset((e["a"], e["b"])) for e in data["adjacencies"]}
    for room in ("Tavern", "Smithy", "Chapel", "Cottages"):
        assert links & {frozenset((room, other)) for other in t.names} == {frozenset((room, "TownSquare"))}
    assert {p for p in links if "Crypt" in p} == {frozenset(("Crypt", "GuildHall"))}
    assert {p for p in links if "Meadow" in p} == {frozenset(("Meadow", "RiverDocks")), frozenset(("Meadow", "Quarry"))}
    assert len(t.decor) == t.height and any("=" in row for row in t.decor)
    image = render(t, labels=False, scale=1)
    assert image.size == (t.width * 16, t.height * 16)
