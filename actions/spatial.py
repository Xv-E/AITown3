"""
格子空间 — 无缝世界：所有区域（Location）拼在同一张世界格子上（area_x / area_y + grid_w × grid_h），
区域之间没有墙；人有世界坐标，走路沿格子寻路、耗时按距离（见文件后半「无缝世界」）。

前半是区域内的摆放：每个 Location 自己的 grid_w × grid_h 局部格子。

只管几何：哪些格子被固定物占着、某个占地能不能放、离某点最近的空位在哪。
物品的格子坐标存在 Item.x / Item.y（左上角），占地存在 footprint_w / footprint_h。
可携带物品有坐标但不占格子。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
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


# =====================================================================
# 无缝世界：所有区域拼在同一张世界格子上
#
#   Location.area_x / area_y   区域左上角的世界格子坐标（grid_w × grid_h 是它的大小）
#   Item.x / Item.y            仍是区域内的局部格子（上面的摆放函数不变）
#   Person.x / Person.y        世界格子坐标（人会跨区域走动）
#
# AI 仍按区域推理（PDDL / 日程 / 认知），世界负责「走过去要多久、站在哪」。
# =====================================================================

# 走路速度：世界格子 / 模拟秒（一天 = 300 模拟秒；横穿一个 20 格的区域约 2 秒 ≈ 10 游戏分钟）
WALK_CELLS_PER_SEC = 10.0
# 与目标距离不超过这么多格就算「在旁边」，不用再走
REACH = 1.5

Area = Tuple[int, int, int, int]  # x, y, w, h（世界格子）


def region_areas(world: "Neo4jWorld") -> Dict[str, Area]:
    rows = world.run_query(
        """
        MATCH (l:Location)
        WHERE l.area_x IS NOT NULL AND l.area_y IS NOT NULL
        RETURN l.name AS name, l.area_x AS x, l.area_y AS y,
               coalesce(l.grid_w, $dw) AS w, coalesce(l.grid_h, $dh) AS h
        """,
        dw=DEFAULT_GRID[0],
        dh=DEFAULT_GRID[1],
    ) or []
    return {r["name"]: (_int(r["x"]), _int(r["y"]), _int(r["w"]), _int(r["h"])) for r in rows}


def layout_regions(world: "Neo4jWorld") -> Dict[str, Area]:
    """没有手写 area 的区域自动排到世界格子上：从连接最多的区域起，邻居贴着它的四边放。"""
    rows = world.run_query(
        """
        MATCH (l:Location)
        OPTIONAL MATCH (l)-[:ADJACENT_TO|LOCKED_ADJACENT_TO]-(m:Location)
        RETURN l.name AS name, l.area_x AS x, l.area_y AS y,
               coalesce(l.grid_w, $dw) AS w, coalesce(l.grid_h, $dh) AS h,
               collect(DISTINCT m.name) AS adj
        """,
        dw=DEFAULT_GRID[0],
        dh=DEFAULT_GRID[1],
    ) or []
    size = {r["name"]: (max(4, _int(r["w"])), max(4, _int(r["h"]))) for r in rows}
    adj = {r["name"]: sorted(a for a in r["adj"] or [] if a) for r in rows}
    placed: Dict[str, Area] = {
        r["name"]: (_int(r["x"]), _int(r["y"]), *size[r["name"]])
        for r in rows
        if r.get("x") is not None and r.get("y") is not None
    }
    todo = [n for n in sorted(size, key=lambda n: (-len(adj[n]), n)) if n not in placed]
    if todo and not placed:
        first = todo.pop(0)
        placed[first] = (0, 0, *size[first])

    def free(a: Area) -> bool:
        x, y, w, h = a
        return all(
            x + w <= ox or ox + ow <= x or y + h <= oy or oy + oh <= y
            for ox, oy, ow, oh in placed.values()
        )

    def around(anchor: Area, w: int, h: int) -> List[Area]:
        ax, ay, aw, ah = anchor
        cx, cy = ax + (aw - w) // 2, ay + (ah - h) // 2
        return [
            (ax + aw, cy, w, h), (ax - w, cy, w, h), (cx, ay + ah, w, h), (cx, ay - h, w, h),
            (ax + aw, ay + ah, w, h), (ax - w, ay - h, w, h), (ax + aw, ay - h, w, h), (ax - w, ay + ah, w, h),
        ]

    while todo:
        # 先放已有邻居落位的；整张图不连通时随便贴一个已放的
        todo.sort(key=lambda n: (not any(a in placed for a in adj[n]), n))
        name = todo.pop(0)
        w, h = size[name]
        anchors = [placed[a] for a in adj[name] if a in placed] or list(placed.values())
        spot = next((c for anchor in anchors for c in around(anchor, w, h) if free(c)), None)
        if spot is None:  # 实在挤不下：放到最右边
            right = max(x + aw for x, _, aw, _ in placed.values())
            spot = (right + 1, 0, w, h)
        placed[name] = spot

    # 平移到非负坐标，写回
    min_x = min((a[0] for a in placed.values()), default=0)
    min_y = min((a[1] for a in placed.values()), default=0)
    out: Dict[str, Area] = {}
    for name, (x, y, w, h) in placed.items():
        out[name] = (x - min_x, y - min_y, w, h)
        world.run_query(
            "MATCH (l:Location {name: $n}) SET l.area_x = $x, l.area_y = $y, l.grid_w = $w, l.grid_h = $h",
            n=name, x=x - min_x, y=y - min_y, w=w, h=h,
        )
    return out


def region_at(areas: Dict[str, Area], cell: Cell) -> Optional[str]:
    x, y = cell
    for name, (ax, ay, aw, ah) in sorted(areas.items()):
        if ax <= x < ax + aw and ay <= y < ay + ah:
            return name
    return None


def walkable_cells(world: "Neo4jWorld", areas: Optional[Dict[str, Area]] = None) -> Set[Cell]:
    """所有区域形状里的格子，去掉固定物占的格子。区域之间没有墙（相接处有通道）。"""
    areas = areas if areas is not None else region_areas(world)
    item_cells, blocked = _item_cells(world, areas)
    out: Set[Cell] = set()
    for shape in region_shapes(world, areas, item_cells).values():
        out |= shape
    return out - blocked


# ---------------------------------------------------------------------
# 区域形状：在矩形范围里长出一块圆润、边缘起伏的地（按名字固定，不随机变）。
# 矩形之间相接的地方留通道，地上的东西也一定连得到中心，所以哪儿都走得到。
# ---------------------------------------------------------------------

SHAPE_POWER = 2.2        # 越大越接近方形，2 = 椭圆
SHAPE_EDGE = 0.95        # 形状占矩形的比例
SHAPE_WOBBLE = ((2, 0.08), (3, 0.07), (4, 0.05))  # (起伏频率, 幅度)
SHAPE_SMOOTH = 2         # 平滑几遍：去掉孤零零的尖角、补上小坑
LINK_HALF = 1            # 区域之间的通道：中线两侧各几格


def _carve_line(out: Set[Cell], a: Cell, b: Cell, area: Area, radius: int = 1) -> None:
    """a → b 的直线连同两侧 radius 格并入 out（只在矩形范围里）。"""
    x0, y0, w, h = area
    steps = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1)
    for i in range(steps + 1):
        cx = round(a[0] + (b[0] - a[0]) * i / steps)
        cy = round(a[1] + (b[1] - a[1]) * i / steps)
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                x, y = cx + dx, cy + dy
                if x0 <= x < x0 + w and y0 <= y < y0 + h:
                    out.add((x, y))


def _smooth(shape: Set[Cell], area: Area) -> Set[Cell]:
    """多数表决：8 邻里 5 个以上是地就算地，2 个以下就不算。"""
    x0, y0, w, h = area
    out: Set[Cell] = set()
    for x in range(x0, x0 + w):
        for y in range(y0, y0 + h):
            n = sum((x + dx, y + dy) in shape for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy)
            if n >= 5 or ((x, y) in shape and n >= 3):
                out.add((x, y))
    return out


def region_shape(name: str, area: Area, anchors: Iterable[Cell] = ()) -> Set[Cell]:
    """这块区域实际的地（世界格子）。anchors：必须连到中心的格子（通道口、物品）。"""
    x0, y0, w, h = area
    if w <= 0 or h <= 0:
        return set()
    seed = _stable_hash(name)
    phases = [((seed >> (8 * i)) & 0xFF) / 255.0 * 2 * math.pi for i in range(len(SHAPE_WOBBLE))]
    cx, cy = x0 + w / 2.0, y0 + h / 2.0
    out: Set[Cell] = set()
    for x in range(x0, x0 + w):
        for y in range(y0, y0 + h):
            u = (x + 0.5 - cx) / (w / 2.0)
            v = (y + 0.5 - cy) / (h / 2.0)
            r = (abs(u) ** SHAPE_POWER + abs(v) ** SHAPE_POWER) ** (1.0 / SHAPE_POWER)
            angle = math.atan2(v, u)
            limit = SHAPE_EDGE + sum(amp * math.sin(k * angle + ph) for (k, amp), ph in zip(SHAPE_WOBBLE, phases))
            if r <= limit:
                out.add((x, y))
    for _ in range(SHAPE_SMOOTH):
        out = _smooth(out, area)
    center = (x0 + w // 2, y0 + h // 2)
    for a in anchors:
        _carve_line(out, a, center, area)
    return out


def region_links(areas: Dict[str, Area]) -> Dict[str, List[Cell]]:
    """相接的两块矩形：各自在交界线中点留一个通道口（格子在自己那一侧）。"""
    out: Dict[str, List[Cell]] = {n: [] for n in areas}
    names = sorted(areas)
    for i, a in enumerate(names):
        ax, ay, aw, ah = areas[a]
        for b in names[i + 1:]:
            bx, by, bw, bh = areas[b]
            if ax + aw == bx or bx + bw == ax:  # 左右相接
                lo, hi = max(ay, by), min(ay + ah, by + bh)
                if hi - lo >= 1:
                    mid = (lo + hi) // 2
                    a_x, b_x = (ax + aw - 1, bx) if ax + aw == bx else (ax, bx + bw - 1)
                    for d in range(-LINK_HALF, LINK_HALF + 1):
                        if lo <= mid + d < hi:
                            out[a].append((a_x, mid + d))
                            out[b].append((b_x, mid + d))
            if ay + ah == by or by + bh == ay:  # 上下相接
                lo, hi = max(ax, bx), min(ax + aw, bx + bw)
                if hi - lo >= 1:
                    mid = (lo + hi) // 2
                    a_y, b_y = (ay + ah - 1, by) if ay + ah == by else (ay, by + bh - 1)
                    for d in range(-LINK_HALF, LINK_HALF + 1):
                        if lo <= mid + d < hi:
                            out[a].append((mid + d, a_y))
                            out[b].append((mid + d, b_y))
    return out


def _item_cells(world: "Neo4jWorld", areas: Dict[str, Area]) -> Tuple[Dict[str, List[Cell]], Set[Cell]]:
    """每个区域里物品占的世界格子；以及固定物（挡路）占的格子。"""
    rows = world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(l:Location)
        WHERE i.x IS NOT NULL AND i.y IS NOT NULL
        RETURN l.name AS loc, i.x AS x, i.y AS y, coalesce(i.portable, true) AS portable,
               coalesce(i.footprint_w, 1) AS w, coalesce(i.footprint_h, 1) AS h
        """
    ) or []
    per: Dict[str, List[Cell]] = {n: [] for n in areas}
    blocked: Set[Cell] = set()
    for r in rows:
        area = areas.get(r["loc"])
        if not area:
            continue
        footprint = list(cells(area[0] + _int(r["x"]), area[1] + _int(r["y"]), _int(r["w"], 1), _int(r["h"], 1)))
        per[r["loc"]].extend(footprint)
        if not r.get("portable", True):
            blocked.update(footprint)
    return per, blocked


_SHAPE_CACHE: Dict[Any, Dict[str, Set[Cell]]] = {}


def region_shapes(world: "Neo4jWorld", areas: Optional[Dict[str, Area]] = None,
                  item_cells: Optional[Dict[str, List[Cell]]] = None) -> Dict[str, Set[Cell]]:
    """所有区域的形状（同样的布局 + 物品位置只算一次）。"""
    areas = areas if areas is not None else region_areas(world)
    if item_cells is None:
        item_cells, _ = _item_cells(world, areas)
    key = (
        tuple(sorted(areas.items())),
        tuple(sorted((n, tuple(sorted(c))) for n, c in item_cells.items())),
    )
    hit = _SHAPE_CACHE.get(key)
    if hit is not None:
        return hit
    links = region_links(areas)
    shapes = {
        name: region_shape(name, area, list(links.get(name, [])) + list(item_cells.get(name, [])))
        for name, area in areas.items()
    }
    if len(_SHAPE_CACHE) > 64:
        _SHAPE_CACHE.clear()
    _SHAPE_CACHE[key] = shapes
    return shapes


def shape_rows(shape: Set[Cell], area: Area) -> List[str]:
    """给客户端：每行一串 '#'（地）/ '.'（空）。"""
    x0, y0, w, h = area
    return ["".join("#" if (x, y) in shape else "." for x in range(x0, x0 + w)) for y in range(y0, y0 + h)]


def nearest_walkable(walkable: Set[Cell], cell: Cell) -> Optional[Cell]:
    return min(walkable, key=lambda c: (_octile(c, cell), c)) if walkable else None


def find_path(walkable: Set[Cell], start: Cell, goals: Set[Cell]) -> Optional[List[Cell]]:
    """A*（8 方向，斜走不切角）；返回含起点的格子路径。"""
    import heapq

    goals = {g for g in goals if g in walkable}
    if not goals:
        return None
    if start in goals:
        return [start]

    def h(c: Cell) -> float:
        return min(_octile(c, g) for g in goals)

    frontier: List[Tuple[float, float, Cell]] = [(h(start), 0.0, start)]
    came: Dict[Cell, Optional[Cell]] = {start: None}
    cost: Dict[Cell, float] = {start: 0.0}
    while frontier:
        _, g, cur = heapq.heappop(frontier)
        if cur in goals:
            path = [cur]
            while came[path[-1]] is not None:
                path.append(came[path[-1]])  # type: ignore[arg-type]
            return path[::-1]
        if g > cost.get(cur, math.inf):
            continue
        x, y = cur
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nxt = (x + dx, y + dy)
            if nxt not in walkable:
                continue
            if dx and dy and ((x + dx, y) not in walkable or (x, y + dy) not in walkable):
                continue
            ng = g + (1.4142 if dx and dy else 1.0)
            if ng < cost.get(nxt, math.inf):
                cost[nxt] = ng
                came[nxt] = cur
                heapq.heappush(frontier, (ng + h(nxt), ng, nxt))
    return None


def path_length(path: List[Cell]) -> float:
    return sum(_octile(a, b) for a, b in zip(path, path[1:]))


def _octile(a: Cell, b: Cell) -> float:
    dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
    return max(dx, dy) + 0.4142 * min(dx, dy)


def _stable_hash(text: str) -> int:
    h = 5381
    for ch in text.encode("utf-8"):
        h = (h * 33 + ch) & 0xFFFFFFFF
    return h


def resting_spot(walkable: Set[Cell], area: Area, who: str) -> Optional[Cell]:
    """区域里给某人的落脚点：中心附近，按名字错开，免得大家叠在一个格子上。"""
    ax, ay, aw, ah = area
    jitter = _stable_hash(who)
    cx = ax + aw // 2 + (jitter % 7) - 3
    cy = ay + ah // 2 + (jitter // 7 % 5) - 2
    inside = [c for c in walkable if ax <= c[0] < ax + aw and ay <= c[1] < ay + ah]
    return min(inside, key=lambda c: (_octile(c, (cx, cy)), c)) if inside else None


def person_cell(world: "Neo4jWorld", name: str) -> Optional[Cell]:
    """人的世界坐标；没有、或不在自己所在区域里（被脚本挪走过）时就地补一个并存下。"""
    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})
        OPTIONAL MATCH (p)-[:LOCATED_AT]->(l:Location)
        RETURN p.x AS x, p.y AS y, l.name AS loc LIMIT 1
        """,
        n=name,
    ) or []
    if not rows:
        return None
    r = rows[0]
    areas = region_areas(world)
    area = areas.get(r.get("loc") or "")
    if area is None:
        return None
    walkable = walkable_cells(world, areas)
    if r.get("x") is not None and r.get("y") is not None:
        cell = (_int(r["x"]), _int(r["y"]))
        if region_at({r["loc"]: area}, cell) and cell in walkable:
            return cell
    spot = resting_spot(walkable, area, name)
    if spot:
        set_person_cell(world, name, spot)
    return spot


def set_person_cell(world: "Neo4jWorld", name: str, cell: Cell) -> None:
    world.run_query("MATCH (p:Person {name: $n}) SET p.x = $x, p.y = $y", n=name, x=cell[0], y=cell[1])


def place_people(world: "Neo4jWorld") -> None:
    for r in world.run_query("MATCH (p:Person) RETURN p.name AS n") or []:
        person_cell(world, r["n"])


def entity_world_cells(world: "Neo4jWorld", name: str) -> Optional[List[Cell]]:
    """物品 / 人占的世界格子；被人拿着的物品算在拿的人身上。"""
    if world.run_query("MATCH (p:Person {name: $n}) RETURN 1 AS ok LIMIT 1", n=name):
        cell = person_cell(world, name)
        return [cell] if cell else None
    rows = world.run_query(
        """
        MATCH (i:Item {name: $n})
        OPTIONAL MATCH (i)-[:LOCATED_AT]->(l:Location)
        OPTIONAL MATCH (h:Person)-[:HAS_ITEM]->(i)
        RETURN l.name AS loc, h.name AS holder, i.x AS x, i.y AS y,
               coalesce(i.portable, true) AS portable,
               coalesce(i.footprint_w, 1) AS w, coalesce(i.footprint_h, 1) AS h
        LIMIT 1
        """,
        n=name,
    ) or []
    if not rows:
        return None
    r = rows[0]
    if r.get("holder"):
        cell = person_cell(world, r["holder"])
        return [cell] if cell else None
    area = region_areas(world).get(r.get("loc") or "")
    if area is None or r.get("x") is None or r.get("y") is None:
        return None
    w, h = (1, 1) if r.get("portable") is not False else (_int(r["w"], 1), _int(r["h"], 1))
    return list(cells(area[0] + _int(r["x"]), area[1] + _int(r["y"]), w, h))


@dataclass
class Walk:
    path: List[Cell]
    seconds: float

    @property
    def dest(self) -> Cell:
        return self.path[-1]


# 动作里「要走到谁 / 什么旁边」的参数位置（按能力；内容包换了动作名也成立）
_APPROACH_ARG = {"take": 1, "give": 2, "converse": 1, "attack": 1}
_APPROACH_BY_NAME = {"use_on": 2, "interact": 1}


def plan_walk(world: "Neo4jWorld", actor: str, action_name: str, args: List[Any]) -> Optional[Walk]:
    """执行这个动作之前要走的路；不用走（或这个世界没有布局）返回 None。"""
    from content import get_content

    areas = region_areas(world)
    if not areas:
        return None
    start = person_cell(world, actor)
    if start is None:
        return None
    walkable = walkable_cells(world, areas) | {start}
    capability = get_content().capability_of(action_name)

    if capability == "travel":
        area = areas.get(str(args[2])) if len(args) > 2 else None
        spot = resting_spot(walkable, area, actor) if area else None
        goals = {spot} if spot else set()
    else:
        idx = _APPROACH_BY_NAME.get(action_name, _APPROACH_ARG.get(capability or ""))
        if idx is None or idx >= len(args) or str(args[idx]) == actor:
            return None
        target = entity_world_cells(world, str(args[idx]))
        if not target:
            return None
        if any(_octile(start, t) <= REACH for t in target):
            return None
        goals = {
            (tx + dx, ty + dy)
            for tx, ty in target
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if (tx + dx, ty + dy) not in target
        }
    path = find_path(walkable, start, goals)
    if not path or len(path) < 2:
        return None
    return Walk(path=path, seconds=path_length(path) / WALK_CELLS_PER_SEC)


def move_person_freely(world: "Neo4jWorld", name: str, cell: Cell) -> Dict[str, Any]:
    """玩家在无缝世界里自由走动：写下坐标；走进别的区域就换所在区域（不要求走 move 动作）。"""
    areas = region_areas(world)
    if cell not in walkable_cells(world, areas):
        return {"ok": False, "error": "Can't stand there"}
    region = region_at(areas, cell)
    before = person_region_name(world, name)
    set_person_cell(world, name, cell)
    if region and region != before:
        world.run_query(
            """
            MATCH (p:Person {name: $n})
            OPTIONAL MATCH (p)-[r:LOCATED_AT]->()
            DELETE r
            WITH DISTINCT p
            MATCH (l:Location {name: $loc})
            MERGE (p)-[:LOCATED_AT]->(l)
            MERGE (p)-[:VISITED]->(l)
            """,
            n=name, loc=region,
        )
    return {"ok": True, "region": region, "changed_region": bool(region and region != before)}


def person_region_name(world: "Neo4jWorld", name: str) -> Optional[str]:
    rows = world.run_query(
        "MATCH (:Person {name: $n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1", n=name
    ) or []
    return rows[0]["loc"] if rows else None


# 看得见 / 够得着说话的距离（世界格子）
VIEW_RADIUS = 8.0


def people_near(
    world: "Neo4jWorld", name: str, *, radius: float = VIEW_RADIUS, hidden: Iterable[str] = ()
) -> Optional[List[str]]:
    """离 name 不超过 radius 格的其他活人（不分区域）；这个世界没有布局时返回 None。"""
    me = person_cell(world, name)
    if me is None:
        return None
    rows = world.run_query(
        """
        MATCH (p:Person)
        WHERE p.name <> $n AND coalesce(p.dead, false) = false AND coalesce(p.hp, 0) > 0
          AND NOT coalesce(p.state, '') IN $hidden
        RETURN p.name AS name
        """,
        n=name,
        hidden=list(hidden),
    ) or []
    out = []
    for r in rows:
        cell = person_cell(world, r["name"])
        if cell is not None and _octile(me, cell) <= radius:
            out.append(r["name"])
    return sorted(out)
