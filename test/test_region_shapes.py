"""区域形状：不再是方方正正的矩形，但相接的区域之间、地上的东西都走得到。"""

from __future__ import annotations

from spatial import find_path, region_links, region_shape, shape_rows

CAMP = {"Shelter": (20, 0, 20, 13), "Forest": (0, 10, 20, 20), "CampfireClearing": (20, 13, 20, 14),
        "BerryGrove": (40, 10, 20, 20), "RiverBank": (20, 27, 20, 13)}


def _center(area):
    return (area[0] + area[2] // 2, area[1] + area[3] // 2)


def test_regions_are_rounded_not_rectangles():
    for name, (x, y, w, h) in CAMP.items():
        shape = region_shape(name, (x, y, w, h))
        assert 0.6 * w * h < len(shape) < w * h, name
        assert (x, y) not in shape and (x + w - 1, y + h - 1) not in shape, f"{name}: corners are cut"
        assert _center((x, y, w, h)) in shape
        assert all(x <= cx < x + w and y <= cy < y + h for cx, cy in shape), "stays inside its area"


def test_shapes_are_stable_and_differ_by_name():
    area = (0, 0, 20, 20)
    assert region_shape("Forest", area) == region_shape("Forest", area)
    assert region_shape("Forest", area) != region_shape("BerryGrove", area)


def test_every_region_reaches_every_other():
    links = region_links(CAMP)
    walk = set().union(*(region_shape(n, a, links[n]) for n, a in CAMP.items()))
    for a in CAMP:
        for b in CAMP:
            assert find_path(walk, _center(CAMP[a]), {_center(CAMP[b])}), (a, b)


def test_something_in_a_cut_corner_is_still_reachable():
    area = (0, 0, 20, 20)
    shape = region_shape("Forest", area, anchors=[(0, 0)])
    assert (0, 0) in shape and find_path(shape, _center(area), {(0, 0)})


def test_rows_for_the_client():
    area = (5, 5, 4, 3)
    rows = shape_rows({(5, 5), (8, 7)}, area)
    assert rows == ["#...", "....", "...#"]


class DoorWorld:
    """Hall | Vault behind a door (locked until the key links them); Store touches Hall but isn't adjacent."""

    def __init__(self):
        self.locked = True
        self.areas = {"Hall": (0, 0, 12, 10), "Vault": (12, 0, 10, 10), "Store": (0, 10, 12, 8)}
        self.door_xy = None

    def run_query(self, q, **k):
        if "l.area_x IS NOT NULL" in q:
            return [{"name": n, "x": a[0], "y": a[1], "w": a[2], "h": a[3]} for n, a in self.areas.items()]
        if "toLower(coalesce(d.item_kind" in q:
            return [{"door": "VaultDoor", "here": "Hall", "to": "", "locked": self.locked, "locked_to": []}]
        if "i.effects_on IS NOT NULL" in q:
            return [{"e": '[{"target": "VaultDoor", "ops": [{"op": "link", "rel": "ADJACENT_TO", "a": "Hall", "b": "Vault"}]}]'}]
        if "[:ADJACENT_TO]-(b:Location) WHERE a.name < b.name" in q:
            return [] if self.locked else [{"a": "Hall", "b": "Vault"}]
        if "[:ADJACENT_TO]-(:Location {name: $b})" in q:
            return [{"ok": 1}] if (not self.locked and {k["a"], k["b"]} == {"Hall", "Vault"}) else []
        if "MATCH (i:Item)-[:LOCATED_AT]->(l:Location)" in q and "i.x IS NOT NULL" in q:
            if self.door_xy is None:
                return []
            return [{"loc": "Hall", "x": self.door_xy[0], "y": self.door_xy[1], "portable": False, "w": 1, "h": 1,
                     "kind": "door", "locked": self.locked}]
        return []


def test_a_locked_door_is_the_only_way_through_a_wall():
    from spatial import boundary_status, find_path, open_between, walkable_cells

    world = DoorWorld()
    status = boundary_status(world, world.areas)
    assert status[frozenset(("Hall", "Vault"))] == "door"
    assert status[frozenset(("Hall", "Store"))] == "wall", "touching but not adjacent: a wall"
    hall, vault, store = (6, 5), (17, 5), (6, 14)
    walk = walkable_cells(world)
    assert hall in walk and vault in walk
    assert find_path(walk, hall, {vault}) is None, "locked: no way in"
    assert find_path(walk, hall, {store}) is None, "a wall, no door"
    assert not open_between(world, "Hall", "Vault")

    world.locked = False  # the key was used: the door opens, the regions become adjacent
    walk = walkable_cells(world)
    path = find_path(walk, hall, {vault})
    assert path is not None and (11, 5) in path, "through the one-cell doorway"
    assert open_between(world, "Hall", "Vault")
