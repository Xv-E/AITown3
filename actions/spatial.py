"""
区域格子空间 — 每个 Location 是一张 grid_w × grid_h 的格子地图，大地图上的位置是 map_x / map_y。

只管几何：哪些格子被固定物占着、某个占地能不能放、离某点最近的空位在哪。
物品的格子坐标存在 Item.x / Item.y（左上角），占地存在 footprint_w / footprint_h。
可携带物品有坐标但不占格子。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

DEFAULT_GRID = (20, 12)
# 四周留一圈给出口
EDGE_MARGIN = 1

Cell = Tuple[int, int]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def pair(raw: Any) -> Optional[Tuple[int, int]]:
    """[a, b] / (a, b) / {"w":a,"h":b} / {"x":a,"y":b} → (a, b)。"""
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return _int(raw[0]), _int(raw[1])
    if isinstance(raw, dict):
        if "w" in raw or "h" in raw:
            return _int(raw.get("w"), 1), _int(raw.get("h"), 1)
        if "x" in raw or "y" in raw:
            return _int(raw.get("x")), _int(raw.get("y"))
    return None


def zone_grid(world: "Neo4jWorld", location: str) -> Tuple[int, int]:
    rows = world.run_query(
        "MATCH (l:Location {name: $n}) RETURN l.grid_w AS w, l.grid_h AS h LIMIT 1",
        n=location,
    ) or []
    if not rows:
        return DEFAULT_GRID
    w = _int(rows[0].get("w"), DEFAULT_GRID[0]) or DEFAULT_GRID[0]
    h = _int(rows[0].get("h"), DEFAULT_GRID[1]) or DEFAULT_GRID[1]
    return max(4, w), max(4, h)


def footprint_of(props: Dict[str, Any]) -> Tuple[int, int]:
    fp = pair(props.get("footprint"))
    if fp:
        return max(1, fp[0]), max(1, fp[1])
    return max(1, _int(props.get("footprint_w"), 1)), max(1, _int(props.get("footprint_h"), 1))


def cells(x: int, y: int, w: int, h: int) -> Iterable[Cell]:
    for dx in range(w):
        for dy in range(h):
            yield x + dx, y + dy


def occupied(
    world: "Neo4jWorld",
    location: str,
    *,
    exclude: Optional[str] = None,
    include_portable: bool = False,
) -> Set[Cell]:
    rows = world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(:Location {name: $loc})
        WHERE ($portable OR coalesce(i.portable, true) = false)
          AND i.x IS NOT NULL AND i.y IS NOT NULL
          AND NOT (:Person)-[:HAS_ITEM]->(i)
        RETURN i.name AS name, i.x AS x, i.y AS y,
               coalesce(i.footprint_w, 1) AS w, coalesce(i.footprint_h, 1) AS h
        """,
        loc=location,
        portable=include_portable,
    ) or []
    out: Set[Cell] = set()
    for r in rows:
        if exclude and r.get("name") == exclude:
            continue
        out.update(cells(_int(r["x"]), _int(r["y"]), _int(r["w"], 1), _int(r["h"], 1)))
    return out


def can_place(
    world: "Neo4jWorld",
    location: str,
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    exclude: Optional[str] = None,
) -> Tuple[bool, str]:
    gw, gh = zone_grid(world, location)
    m = EDGE_MARGIN
    if x < m or y < m or x + w > gw - m or y + h > gh - m:
        return False, f"Out of bounds ({gw}x{gh} grid)"
    taken = occupied(world, location, exclude=exclude)
    if any(c in taken for c in cells(x, y, w, h)):
        return False, "Something is already built there"
    return True, "ok"


def find_spot(
    world: "Neo4jWorld",
    location: str,
    w: int,
    h: int,
    *,
    near: Optional[Cell] = None,
    blocking: bool = True,
    walkway: bool = True,
) -> Optional[Cell]:
    """离 near（默认区域中心）最近、能放下 w×h 的左上角格子。

    blocking=True（固定物）：避开其它固定物，尽量四周留一格走道；
    blocking=False（可携带物）：格子上不能已有任何东西（也不叠在别的掉落物上）。
    """
    gw, gh = zone_grid(world, location)
    taken = occupied(world, location, include_portable=not blocking)
    m = EDGE_MARGIN
    cx, cy = near if near else (gw // 2 - w // 2, gh // 2 - h // 2)
    best: Optional[Tuple[float, Cell]] = None
    for x in range(m, gw - m - w + 1):
        for y in range(m, gh - m - h + 1):
            if any(c in taken for c in cells(x, y, w, h)):
                continue
            if blocking and walkway and any(
                (x + dx, y + dy) in taken for dx in range(-1, w + 1) for dy in range(-1, h + 1)
            ):
                continue
            d = math.hypot(x - cx, y - cy)
            if best is None or d < best[0]:
                best = (d, (x, y))
    if best is None and blocking and walkway:
        return find_spot(world, location, w, h, near=near, blocking=True, walkway=False)
    return best[1] if best else None


def item_props(world: "Neo4jWorld", name: str) -> Dict[str, Any]:
    rows = world.run_query(
        """
        MATCH (i:Item {name: $n})
        OPTIONAL MATCH (i)-[:LOCATED_AT]->(l:Location)
        RETURN properties(i) AS props, l.name AS loc LIMIT 1
        """,
        n=name,
    ) or []
    if not rows:
        return {}
    props = dict(rows[0].get("props") or {})
    props["_loc"] = rows[0].get("loc")
    return props


def entity_cell(world: "Neo4jWorld", name: str) -> Optional[Cell]:
    props = item_props(world, name)
    if props.get("x") is None or props.get("y") is None:
        return None
    return _int(props["x"]), _int(props["y"])


def place_item(
    world: "Neo4jWorld",
    name: str,
    *,
    near: Optional[Cell] = None,
    force: bool = False,
) -> Optional[Cell]:
    """给地上的物品分配格子坐标（已有坐标且 force=False 时保持不变）。"""
    props = item_props(world, name)
    loc = props.get("_loc")
    if not loc:
        return None
    if not force and props.get("x") is not None and props.get("y") is not None:
        return _int(props["x"]), _int(props["y"])
    portable = props.get("portable", True) is not False
    w, h = (1, 1) if portable else footprint_of(props)
    spot = find_spot(world, loc, w, h, near=near, blocking=not portable)
    if spot is None:
        return None
    world.run_query(
        "MATCH (i:Item {name: $n}) SET i.x = $x, i.y = $y",
        n=name,
        x=spot[0],
        y=spot[1],
    )
    return spot


def place_unplaced(world: "Neo4jWorld", location: Optional[str] = None) -> List[str]:
    """给所有还没有坐标的地上物品分配位置（固定物优先、大的优先）。"""
    rows = world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(l:Location)
        WHERE (i.x IS NULL OR i.y IS NULL) AND ($loc IS NULL OR l.name = $loc)
        RETURN i.name AS name, coalesce(i.portable, true) AS portable,
               coalesce(i.footprint_w, 1) * coalesce(i.footprint_h, 1) AS area
        """,
        loc=location,
    ) or []
    rows.sort(key=lambda r: (bool(r.get("portable")), -_int(r.get("area"), 1), r.get("name") or ""))
    placed = []
    for r in rows:
        if place_item(world, r["name"]):
            placed.append(r["name"])
    return placed


def map_distance(world: "Neo4jWorld", a: str, b: str) -> Optional[float]:
    rows = world.run_query(
        """
        MATCH (a:Location {name: $a}), (b:Location {name: $b})
        RETURN a.map_x AS ax, a.map_y AS ay, b.map_x AS bx, b.map_y AS by
        """,
        a=a,
        b=b,
    ) or []
    if not rows:
        return None
    r = rows[0]
    if None in (r.get("ax"), r.get("ay"), r.get("bx"), r.get("by")):
        return None
    return math.hypot(float(r["ax"]) - float(r["bx"]), float(r["ay"]) - float(r["by"]))
