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


# ---------------------------------------------------------------------------
# 区域内步行 — 人物的格子位置存在 Person.x / Person.y（cell_loc = 这格属于哪个区域），
# 动作要先走到目标旁边，走路耗时 = 距离 / WALK_CELLS_PER_SEC。

# 步行速度（格 / 仿真秒）；必须和客户端 adapter.rs 的 NPC_WALK_CELLS_PER_SEC 一致
WALK_CELLS_PER_SEC = 4.0
# 走到目标旁边就停（格）
APPROACH_GAP = 1.0

# 客户端房间矩形（像素），只用来复现 scene.rs exit_layout 的出口位置
_ROOM_HALF_W = 360.0
_ROOM_HALF_H = 210.0
_ROOM_CENTER_Y = -30.0

Point = Tuple[float, float]


def _stable_slot(name: str, fallback_index: int, modulo: int) -> int:
    """scene.rs stable_slot 的移植（djb2，64 位回绕）。"""
    if modulo == 0:
        return 0
    h = 5381
    for b in name.encode("utf-8"):
        h = (h * 33 + b) & 0xFFFFFFFFFFFFFFFF
    return ((h + fallback_index) & 0xFFFFFFFFFFFFFFFF) % modulo


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _clamp_cell(world: "Neo4jWorld", location: str, p: Point) -> Cell:
    gw, gh = zone_grid(world, location)
    m = EDGE_MARGIN
    return (
        min(max(int(round(p[0])), m), gw - m - 1),
        min(max(int(round(p[1])), m), gh - m - 1),
    )


def adjacent_of(world: "Neo4jWorld", location: str) -> List[str]:
    rows = world.run_query(
        "MATCH (:Location {name: $loc})-[:ADJACENT_TO]->(n:Location) RETURN n.name AS name",
        loc=location,
    ) or []
    return sorted({r["name"] for r in rows if r.get("name")})


def exit_points(world: "Neo4jWorld", location: str) -> Dict[str, Point]:
    """每个出口在区域格子坐标里的位置（与客户端 exit_layout 一致；单位是格，格中心为整数）。"""
    gw, gh = zone_grid(world, location)
    tile = min(_ROOM_HALF_W * 2 / gw, _ROOM_HALF_H * 2 / gh)
    origin_x = -gw * tile / 2
    origin_y = _ROOM_CENTER_Y + gh * tile / 2

    walls: List[List[str]] = [[], [], [], []]
    for name in adjacent_of(world, location):
        preferred = _stable_slot(name, 0, 4)
        wall = min(((preferred + s) % 4 for s in range(4)), key=lambda w: len(walls[w]))
        walls[wall].append(name)

    top = _ROOM_CENTER_Y + _ROOM_HALF_H
    bottom = _ROOM_CENTER_Y - _ROOM_HALF_H
    out: Dict[str, Point] = {}
    for wall, members in enumerate(walls):
        n = len(members)
        for i, name in enumerate(members):
            t = (i + 1) / (n + 1)
            if wall == 0:
                px, py = _lerp(-_ROOM_HALF_W + 120, _ROOM_HALF_W - 120, t), top
            elif wall == 1:
                px, py = _ROOM_HALF_W - 58, _lerp(top - 80, bottom + 80, t)
            elif wall == 2:
                px, py = _lerp(_ROOM_HALF_W - 120, -_ROOM_HALF_W + 120, t), bottom
            else:
                px, py = -_ROOM_HALF_W + 58, _lerp(bottom + 80, top - 80, t)
            out[name] = ((px - origin_x) / tile - 0.5, (origin_y - py) / tile - 0.5)
    return out


def _location_of(world: "Neo4jWorld", name: str) -> Optional[str]:
    rows = world.run_query(
        "MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1",
        n=name,
    ) or []
    return rows[0].get("loc") if rows else None


def _set_person_cell(world: "Neo4jWorld", name: str, location: str, cell: Cell) -> None:
    world.run_query(
        "MATCH (p:Person {name: $n}) SET p.x = $x, p.y = $y, p.cell_loc = $loc",
        n=name,
        x=cell[0],
        y=cell[1],
        loc=location,
    )


def person_cell(world: "Neo4jWorld", name: str) -> Optional[Cell]:
    """人物在所在区域里的格子；没有（或是别的区域的旧格子）就按名字挑一个空格子。"""
    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})-[:LOCATED_AT]->(l:Location)
        RETURN p.x AS x, p.y AS y, p.cell_loc AS cell_loc, l.name AS loc LIMIT 1
        """,
        n=name,
    ) or []
    if not rows or not rows[0].get("loc"):
        return None
    r = rows[0]
    loc = r["loc"]
    if r.get("x") is not None and r.get("y") is not None and r.get("cell_loc") == loc:
        return _int(r["x"]), _int(r["y"])
    gw, gh = zone_grid(world, loc)
    m = EDGE_MARGIN
    preferred = (
        m + _stable_slot(name, 0, max(1, gw - 2 * m - 1)),
        m + _stable_slot(f"{name}#", 0, max(1, gh - 2 * m - 1)),
    )
    cell = find_spot(world, loc, 1, 1, near=preferred, blocking=False) or preferred
    _set_person_cell(world, name, loc, cell)
    return cell


def _thing_point(world: "Neo4jWorld", actor: str, location: str, name: str) -> Optional[Point]:
    """同区域里某个人 / 地上物品的位置；不在这里（或在谁手上）返回 None。"""
    if not name or name == actor:
        return None
    if _location_of(world, name) == location:
        cell = person_cell(world, name)
        return (float(cell[0]), float(cell[1])) if cell else None
    rows = world.run_query(
        """
        MATCH (i:Item {name: $n})-[:LOCATED_AT]->(:Location {name: $loc})
        WHERE i.x IS NOT NULL AND i.y IS NOT NULL AND NOT (:Person)-[:HAS_ITEM]->(i)
        RETURN i.x AS x, i.y AS y,
               coalesce(i.footprint_w, 1) AS w, coalesce(i.footprint_h, 1) AS h,
               coalesce(i.portable, true) AS portable
        LIMIT 1
        """,
        n=name,
        loc=location,
    ) or []
    if not rows:
        return None
    r = rows[0]
    w, h = (1, 1) if r.get("portable") is not False else (_int(r["w"], 1), _int(r["h"], 1))
    return _int(r["x"]) + (w - 1) / 2, _int(r["y"]) + (h - 1) / 2


WalkEnd = Tuple[str, Cell]


def plan_walk(
    world: "Neo4jWorld", actor: str, args: List[Any], *, travel: bool
) -> Tuple[float, Optional[WalkEnd]]:
    """动作开始前要走多久（仿真秒），以及走完停在哪（区域, 格子）。

    travel：走到 args[2] 方向的出口（之后由 enter_zone 放进新区域，停点为 None）；
    其它动作：走到参数里第一个同区域的人 / 地上物品旁边。不用走路 → (0, None)。
    """
    loc = _location_of(world, actor)
    start = person_cell(world, actor) if loc else None
    if not loc or start is None:
        return 0.0, None
    sx, sy = float(start[0]), float(start[1])
    if travel:
        door = exit_points(world, loc).get(str(args[2])) if len(args) >= 3 else None
        if door is None:
            return 0.0, None
        return math.hypot(door[0] - sx, door[1] - sy) / WALK_CELLS_PER_SEC, None
    for a in args[1:]:
        target = _thing_point(world, actor, loc, str(a))
        if target is None:
            continue
        dx, dy = sx - target[0], sy - target[1]
        dist = math.hypot(dx, dy)
        if dist <= APPROACH_GAP:
            return 0.0, None
        stop = (target[0] + dx / dist * APPROACH_GAP, target[1] + dy / dist * APPROACH_GAP)
        return (dist - APPROACH_GAP) / WALK_CELLS_PER_SEC, (loc, _clamp_cell(world, loc, stop))
    return 0.0, None


def finish_walk(world: "Neo4jWorld", actor: str, end: Optional[WalkEnd]) -> None:
    """动作结束：人物停在 plan_walk 算好的位置（人已经不在那个区域就不动）。"""
    if end and _location_of(world, actor) == end[0]:
        _set_person_cell(world, actor, end[0], end[1])


def enter_zone(world: "Neo4jWorld", actor: str, location: str, came_from: str) -> None:
    """从 came_from 方向的出口进入 location：站在门内两格附近的空格子。"""
    door = exit_points(world, location).get(came_from)
    if door is None:
        return
    gw, gh = zone_grid(world, location)
    dx, dy = (gw - 1) / 2 - door[0], (gh - 1) / 2 - door[1]
    norm = math.hypot(dx, dy) or 1.0
    inside = _clamp_cell(world, location, (door[0] + dx / norm * 2, door[1] + dy / norm * 2))
    cell = find_spot(world, location, 1, 1, near=inside, blocking=False) or inside
    _set_person_cell(world, actor, location, cell)
