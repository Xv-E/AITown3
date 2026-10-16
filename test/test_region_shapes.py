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
