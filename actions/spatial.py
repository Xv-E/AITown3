"""
格子空间 — 无缝世界：所有地点（Location）都在同一张世界格子地图（地形，Terrain）上。

地形（见 terrain_gen.py）像真实的地图那样划分地点：
  · 室外地点是彼此咬合、边界弯曲的一片片地；
  · 室内地点是有墙的房子里的房间，嵌在所属的室外地点里（Location.indoor / Location.parent）；
  · 墙、篱笆、水是挡路的格子，不属于任何地点；相邻的地点之间才有路（门洞 / 敞开的边界）。
人有世界坐标，走路沿格子寻路、耗时按距离（见文件后半「无缝世界」）。

前半是地点内的摆放：每个 Location 的局部格子以它在地图上的外接矩形（area_x / area_y +
grid_w × grid_h）为准，物品的格子坐标存在 Item.x / Item.y（左上角，相对外接矩形），
占地存在 footprint_w / footprint_h；能放东西的只有这个地点自己的地（不是墙、不是嵌在里面的房子）。
可携带物品有坐标但不占格子。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple

from terrain_gen import (
    DOOR, Place, Terrain, connect, edit_cells, extend, from_ascii, generate, links_present, separate,
)

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

DEFAULT_GRID = (20, 12)
# 没有地形时（老格子）四周留一圈
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


def local_ground(world: "Neo4jWorld", location: str) -> Optional[Tuple[Set[Cell], Set[Cell]]]:
    """这个地点自己的地（局部坐标）：(能放东西的格子, 所有能走的格子)。

    能放东西的格子避开门洞和门洞旁边一圈，免得把门堵死。没有地形的老世界返回 None。
    """
    t = terrain_of(world)
    area = region_areas(world).get(location)
    if t is None or area is None or location not in t.names:
        return None
    ax, ay = area[0], area[1]
    keep_clear: Set[Cell] = set()
    for d in t.doorways:
        keep_clear.update((d.cell[0] + dx, d.cell[1] + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1))
    ground = t.cells_of(location)
    walk = {(x - ax, y - ay) for x, y in ground}
    place = {(x - ax, y - ay) for x, y in ground if (x, y) not in keep_clear and t.kind_at((x, y)) != DOOR}
    return place, walk


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
    ground = local_ground(world, location)
    if ground is None:
        gw, gh = zone_grid(world, location)
        m = EDGE_MARGIN
        if x < m or y < m or x + w > gw - m or y + h > gh - m:
            return False, f"Out of bounds ({gw}x{gh} grid)"
    elif any(c not in ground[0] for c in cells(x, y, w, h)):
        return False, f"Not open ground in {location} (walls, doorways and other places are off limits)"
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
    """离 near（默认地点中间）最近、能放下 w×h 的左上角格子。

    blocking=True（固定物）：避开其它固定物，尽量四周留一格能走的地当走道；
    blocking=False（可携带物）：格子上不能已有任何东西（也不叠在别的掉落物上）。
    """
    taken = occupied(world, location, include_portable=not blocking)
    ground = local_ground(world, location)
    if ground is None:
        gw, gh = zone_grid(world, location)
        m = EDGE_MARGIN
        place = {(x, y) for x in range(m, gw - m) for y in range(m, gh - m)}
        walk = place
        default = (gw // 2 - w // 2, gh // 2 - h // 2)
    else:
        place, walk = ground
        t = terrain_of(world)
        area = region_areas(world)[location]
        mid = t.anchor(location) if t else None
        default = (mid[0] - area[0] - w // 2, mid[1] - area[1] - h // 2) if mid else (0, 0)
    cx, cy = near if near else default
    best: Optional[Tuple[float, Cell]] = None
    for x, y in place:
        foot = list(cells(x, y, w, h))
        if any(c not in place or c in taken for c in foot):
            continue
        if blocking and walkway and any(
            (x + dx, y + dy) not in walk or (x + dx, y + dy) in taken
            for dx in range(-1, w + 1) for dy in range(-1, h + 1)
            if not (0 <= dx < w and 0 <= dy < h)
        ):
            continue
        d = math.hypot(x - cx, y - cy)
        if best is None or (d, (x, y)) < best:
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
    if force:  # 先腾出自己原来的位置
        world.run_query("MATCH (i:Item {name: $n}) REMOVE i.x, i.y", n=name)
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
# 无缝世界：所有地点在同一张地形上
#
#   (:Terrain)                 整张地形（几串字符串，见 terrain_gen.Terrain.to_props）
#   Location.area_x / area_y   地点在地形上的外接矩形左上角（grid_w × grid_h 是它的大小）
#   Location.indoor / parent   室内地点、它所在的室外地点
#   Item.x / Item.y            相对外接矩形的局部格子（上面的摆放函数）
#   Person.x / Person.y        世界格子坐标（人会跨地点走动）
#
# AI 仍按地点推理（PDDL / 日程 / 认知），地形负责「走过去要多久、站在哪、隔不隔墙」。
# =====================================================================

# 走路速度：世界格子 / 模拟秒（一天 = 300 模拟秒；横穿一个 20 格的地点约 2 秒 ≈ 10 游戏分钟）
WALK_CELLS_PER_SEC = 10.0
# 与目标距离不超过这么多格就算「在旁边」，不用再走
REACH = 1.5

Area = Tuple[int, int, int, int]  # x, y, w, h（世界格子）

INDOOR_TAGS = {"indoor", "interior", "inside"}

_TERRAIN_CACHE: Dict[str, Terrain] = {}


def terrain_of(world: "Neo4jWorld", *, build: bool = True) -> Optional[Terrain]:
    """这个世界的地形；还没有（老存档、手搭的世界）就现场生成一次。"""
    rows = world.run_query("MATCH (t:Terrain) RETURN t.version AS v LIMIT 1") or []
    if not rows or not rows[0].get("v"):
        if build and world.run_query("MATCH (l:Location) RETURN l.name AS n LIMIT 1"):
            layout_regions(world)
            return terrain_of(world, build=False)
        return None
    version = str(rows[0]["v"])
    hit = _TERRAIN_CACHE.get(version)
    if hit is not None:
        return hit
    got = world.run_query("MATCH (t:Terrain) RETURN properties(t) AS p LIMIT 1") or []
    if not got:
        return None
    terrain = Terrain.from_props(dict(got[0].get("p") or {}))
    if len(_TERRAIN_CACHE) > 8:
        _TERRAIN_CACHE.clear()
    _TERRAIN_CACHE[version] = terrain
    return terrain


def save_terrain(world: "Neo4jWorld", terrain: Terrain) -> None:
    """存地形，并把每个地点的外接矩形 / 室内 / 所属地写回 Location。"""
    import uuid

    props = terrain.to_props()
    props["name"] = "world"
    props["version"] = uuid.uuid4().hex
    world.run_query("MERGE (t:Terrain {name: 'world'}) SET t = $p", p=props)
    for name in terrain.names:
        box = terrain.bbox(name)
        if box is None:
            continue
        x, y, w, h = box
        world.run_query(
            """
            MATCH (l:Location {name: $n})
            SET l.area_x = $x, l.area_y = $y, l.grid_w = $w, l.grid_h = $h,
                l.indoor = $indoor, l.parent = $parent
            """,
            n=name, x=x, y=y, w=w, h=h,
            indoor=name in terrain.indoor, parent=terrain.parents.get(name, ""),
        )


def save_authored_terrain(world: "Neo4jWorld", raw: Dict[str, Any]) -> Optional[Terrain]:
    """world JSON 里手画的地形（"terrain": {"rows": [...], "legend": {...}, "decor": [...]}）。"""
    rows = [str(r) for r in raw.get("rows") or []]
    legend = {str(k): str(v) for k, v in (raw.get("legend") or {}).items() if len(str(k)) == 1}
    if not rows or not legend:
        return None
    info = world.run_query(
        "MATCH (l:Location) RETURN l.name AS n, coalesce(l.indoor, false) AS indoor, "
        "coalesce(l.parent, '') AS parent, coalesce(l.tags, '') AS tags"
    ) or []
    indoor = {r["n"] for r in info if r.get("indoor") or _tag_set(r.get("tags")) & INDOOR_TAGS}
    parents = {r["n"]: r["parent"] for r in info if r.get("parent")}
    # 门（物品）所在的那边算门洞的主人，门才摆得进门洞
    sides = {frozenset((d["here"], d["other"])): d["here"] for d in door_pairs(world).values()}
    decor = [str(r) for r in raw.get("decor") or []]
    terrain = from_ascii(rows, legend, indoor, parents, decor=decor, door_side=sides)
    save_terrain(world, terrain)
    return terrain


def _tag_set(tags: Any) -> Set[str]:
    return {t.strip().lower() for t in str(tags or "").replace(";", ",").split(",") if t.strip()}


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


def graph_links(world: "Neo4jWorld") -> Tuple[Dict[Any, str], Dict[Any, str]]:
    """图上谁和谁相通：{一对地点: "open" / "door"}，以及门（门洞）算在哪一边。"""
    links: Dict[Any, str] = {}
    side: Dict[Any, str] = {}
    for r in world.run_query(
        "MATCH (a:Location)-[:ADJACENT_TO]-(b:Location) WHERE a.name < b.name RETURN a.name AS a, b.name AS b"
    ) or []:
        links[frozenset((r["a"], r["b"]))] = "open"
    for r in world.run_query(
        "MATCH (a:Location)-[:LOCKED_ADJACENT_TO]-(b:Location) WHERE a.name < b.name RETURN a.name AS a, b.name AS b"
    ) or []:
        links[frozenset((r["a"], r["b"]))] = "door"
    for d in door_pairs(world).values():
        p = frozenset((d["here"], d["other"]))
        links[p] = "door"
        side[p] = d["here"]
    return {k: v for k, v in links.items() if len(k) == 2}, side


def _places(world: "Neo4jWorld") -> List[Place]:
    rows = world.run_query(
        """
        MATCH (l:Location)
        RETURN l.name AS name, l.area_x AS ax, l.area_y AS ay,
               coalesce(l.size_w, l.grid_w) AS w, coalesce(l.size_h, l.grid_h) AS h,
               l.map_x AS mx, l.map_y AS my, coalesce(l.indoor, false) AS indoor,
               coalesce(l.parent, '') AS parent, coalesce(l.tags, '') AS tags
        ORDER BY name
        """
    ) or []
    out = []
    for r in rows:
        w = max(4, _int(r.get("w"), DEFAULT_GRID[0]) or DEFAULT_GRID[0])
        h = max(4, _int(r.get("h"), DEFAULT_GRID[1]) or DEFAULT_GRID[1])
        seed = None
        if r.get("ax") is not None and r.get("ay") is not None:
            seed = (_int(r["ax"]) + w / 2.0, _int(r["ay"]) + h / 2.0)
        mapped = None
        if r.get("mx") is not None and r.get("my") is not None:
            mapped = (float(r["mx"]), float(r["my"]))
        out.append(Place(
            name=r["name"], size=(w, h),
            indoor=bool(r.get("indoor")) or bool(_tag_set(r.get("tags")) & INDOOR_TAGS),
            parent=str(r.get("parent") or ""), seed=seed, map=mapped,
        ))
    return out


def layout_regions(world: "Neo4jWorld") -> Dict[str, Area]:
    """把所有地点落到地形上。

    还没有地形：按地点和连接关系生成整张图（手摆过 area / map 的按原位置）；
    已有地形、多了新地点（任务线新开的区域）：新地点长在荒野里，已有的一格不动。
    """
    import logging

    log = logging.getLogger(__name__)
    # 第一次落图前记下作者给的大小（之后 grid_w / grid_h 会改成地图上的外接矩形）
    world.run_query(
        """
        MATCH (l:Location) WHERE l.size_w IS NULL
        SET l.size_w = coalesce(l.grid_w, $dw), l.size_h = coalesce(l.grid_h, $dh)
        """,
        dw=DEFAULT_GRID[0], dh=DEFAULT_GRID[1],
    )
    places = _places(world)
    if not places:
        return {}
    links, side = graph_links(world)
    existing = terrain_of(world, build=False)
    names = {p.name for p in places}
    # 作者给的格子大小：重新生成地图时，原来的摆放按比例搬到新的外接矩形里
    authored_sizes = {p.name: (0, 0, p.size[0], p.size[1]) for p in places}
    if existing is not None and names <= set(existing.names):
        terrain, rebuilt = existing, False
        save_terrain(world, terrain)  # 外接矩形、室内标记写回（地点可能被手改过）
    elif existing is not None and any(existing.cells_of(n) for n in existing.names):
        terrain, (dx, dy) = extend(existing, places, links, side)
        rebuilt = False
        save_terrain(world, terrain)
        if dx or dy:
            world.run_query(
                "MATCH (p:Person) WHERE p.x IS NOT NULL AND p.y IS NOT NULL SET p.x = p.x + $dx, p.y = p.y + $dy",
                dx=dx, dy=dy,
            )
    else:
        terrain = generate(places, links, side, salt=",".join(sorted(names)))
        rebuilt = True
        save_terrain(world, terrain)
        world.run_query("MATCH (p:Person) REMOVE p.x, p.y")
    missing = [tuple(sorted(p)) for p in links if p <= set(terrain.names) and p not in links_present(terrain)]
    if missing:
        log.info("地形上没有直接相通的相邻地点（经由别处仍走得到；图里会以地图为准）：%s", missing)
    areas = region_areas(world)
    place_doors(world, areas)
    _settle_items(world, terrain, areas, authored_sizes if rebuilt else {})
    sync_adjacency(world)
    return areas


def _settle_items(world: "Neo4jWorld", terrain: Terrain, areas: Dict[str, Area],
                  authored: Dict[str, Area]) -> None:
    """地上的东西要落在自己那个地点的地上：不在的挪到附近的空地。

    authored：重新生成地图前作者给的格子大小；原来的摆放按比例搬到新的外接矩形里（布局大体不变）。
    """
    rows = world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(l:Location)
        WHERE i.x IS NOT NULL AND i.y IS NOT NULL AND NOT (:Person)-[:HAS_ITEM]->(i)
          AND toLower(coalesce(i.item_kind, '')) <> 'door'
        RETURN i.name AS name, l.name AS loc, i.x AS x, i.y AS y, coalesce(i.portable, true) AS portable,
               coalesce(i.footprint_w, 1) AS w, coalesce(i.footprint_h, 1) AS h
        """
    ) or []
    rows.sort(key=lambda r: (bool(r.get("portable")), -_int(r["w"], 1) * _int(r["h"], 1), r["name"]))
    if authored:
        # 先都清掉坐标，再按（缩放后的）原位置一个个放回去，免得互相挡
        for r in rows:
            world.run_query("MATCH (i:Item {name: $n}) REMOVE i.x, i.y", n=r["name"])
    for r in rows:
        loc = r["loc"]
        x, y = _int(r["x"]), _int(r["y"])
        old, new = authored.get(loc), areas.get(loc)
        if old and new:
            x = round(x * new[2] / max(1, old[2]))
            y = round(y * new[3] / max(1, old[3]))
        w, h = (1, 1) if r.get("portable") is not False else (_int(r["w"], 1), _int(r["h"], 1))
        ground = local_ground(world, loc)
        fits = ground is not None and all(c in ground[0] for c in cells(x, y, w, h))
        if fits and authored:
            taken = occupied(world, loc, include_portable=bool(r.get("portable")))
            fits = not any(c in taken for c in cells(x, y, w, h))
        if fits:
            if authored:
                world.run_query("MATCH (i:Item {name: $n}) SET i.x = $x, i.y = $y", n=r["name"], x=x, y=y)
            continue
        place_item(world, r["name"], near=(x, y), force=True)


def door_pairs(world: "Neo4jWorld") -> Dict[str, Dict[str, Any]]:
    """每扇门连着哪两块地。

    门已经摆在地图的门洞上：就是那个门洞两边（地图说了算）。还没摆：看门的 connects_to；
    或者开门的钥匙效果里连的那两处；或者门所在地的「锁着的相邻」。
    """
    import json

    rows = world.run_query(
        """
        MATCH (d:Item)-[:LOCATED_AT]->(l:Location) WHERE toLower(coalesce(d.item_kind, '')) = 'door'
        OPTIONAL MATCH (l)-[:LOCKED_ADJACENT_TO]-(m:Location)
        RETURN d.name AS door, l.name AS here, coalesce(d.connects_to, '') AS to,
               coalesce(d.locked, false) AS locked, collect(DISTINCT m.name) AS locked_to,
               d.x AS x, d.y AS y
        """
    ) or []
    if not rows:
        return {}
    t = terrain_of(world, build=False)
    areas = region_areas(world) if t is not None else {}
    links: Dict[str, Tuple[str, str]] = {}
    for r in world.run_query("MATCH (i:Item) WHERE i.effects_on IS NOT NULL RETURN i.effects_on AS e") or []:
        try:
            effects = json.loads(r["e"]) if isinstance(r["e"], str) else (r["e"] or [])
        except ValueError:
            continue
        for e in effects if isinstance(effects, list) else []:
            for op in (e or {}).get("ops") or []:
                if op.get("op") == "link" and op.get("rel") == "ADJACENT_TO" and e.get("target"):
                    links[str(e["target"])] = (str(op.get("a")), str(op.get("b")))
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        here, other = r["here"], ""
        area = areas.get(here)
        on_map = None
        if t is not None and area and r.get("x") is not None and r.get("y") is not None:
            cell = (area[0] + _int(r["x"]), area[1] + _int(r["y"]))
            on_map = next((w for w in t.doorways if w.cell == cell and here in (w.owner, w.other)), None)
        if on_map is not None:
            other = on_map.other if on_map.owner == here else on_map.owner
            out[r["door"]] = {"here": here, "other": other, "locked": bool(r["locked"]), "cell": on_map.cell}
            continue
        if r["to"]:
            other = r["to"]
        elif r["door"] in links:
            a, b = links[r["door"]]
            other = b if a == here else a if b == here else ""
        elif r.get("locked_to"):
            other = sorted(x for x in r["locked_to"] if x)[0] if any(r["locked_to"]) else ""
        if other and other != here:
            out[r["door"]] = {"here": here, "other": other, "locked": bool(r["locked"])}
    return out


def open_between(world: "Neo4jWorld", a: str, b: str) -> bool:
    """a、b 之间现在走得通（相邻；隔着门的话门得开着）。"""
    rows = world.run_query(
        "MATCH (:Location {name: $a})-[:ADJACENT_TO]-(:Location {name: $b}) RETURN 1 AS ok LIMIT 1", a=a, b=b
    ) or []
    if not rows:
        return False
    for d in door_pairs(world).values():
        if {d["here"], d["other"]} == {a, b} and d["locked"]:
            return False
    return True


def place_doors(world: "Neo4jWorld", areas: Optional[Dict[str, Area]] = None) -> List[str]:
    """把每扇门摆到它那两块地之间的门洞上（门所在那一侧）。返回挪过的门。"""
    t = terrain_of(world, build=False)
    if t is None:
        return []
    areas = areas if areas is not None else region_areas(world)
    moved = []
    for door, d in door_pairs(world).items():
        here = d["here"]
        ways = t.doorways_between(here, d["other"])
        if not ways or here not in areas:
            continue
        way = next((w for w in ways if w.owner == here), ways[0])
        x, y = way.cell[0] - areas[here][0], way.cell[1] - areas[here][1]
        rows = world.run_query("MATCH (i:Item {name: $n}) RETURN i.x AS x, i.y AS y", n=door) or []
        if rows and (rows[0].get("x"), rows[0].get("y")) == (x, y):
            continue
        world.run_query(
            "MATCH (i:Item {name: $n}) SET i.x = $x, i.y = $y, i.footprint_w = 1, i.footprint_h = 1", n=door, x=x, y=y
        )
        moved.append(door)
    return moved


def region_at(world: "Neo4jWorld", cell: Cell) -> Optional[str]:
    """这一格属于哪个地点（墙、篱笆、荒野不属于任何地点）。"""
    t = terrain_of(world)
    return t.owner_at(cell) if t else None


def _item_cells(world: "Neo4jWorld", areas: Dict[str, Area]) -> Tuple[Dict[str, List[Cell]], Set[Cell]]:
    """每个地点里物品占的世界格子；以及固定物（挡路）占的格子。"""
    rows = world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(l:Location)
        WHERE i.x IS NOT NULL AND i.y IS NOT NULL
        RETURN l.name AS loc, i.x AS x, i.y AS y, coalesce(i.portable, true) AS portable,
               coalesce(i.footprint_w, 1) AS w, coalesce(i.footprint_h, 1) AS h,
               coalesce(i.item_kind, '') AS kind, coalesce(i.locked, false) AS locked
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
        if str(r.get("kind") or "").lower() == "door":
            if r.get("locked"):
                blocked.update(footprint)  # 锁着的门挡住门洞；开了就能走过去
        elif not r.get("portable", True):
            blocked.update(footprint)
    return per, blocked


def walkable_cells(world: "Neo4jWorld", areas: Optional[Dict[str, Area]] = None) -> Set[Cell]:
    """所有地点的地（含门洞），去掉固定物占的格子、锁着的门洞。墙和篱笆本来就不在里面。"""
    t = terrain_of(world)
    if t is None:
        return set()
    areas = areas if areas is not None else region_areas(world)
    _, blocked = _item_cells(world, areas)
    return t.walkable() - blocked - locked_door_cells(world, t)


def locked_door_cells(world: "Neo4jWorld", t: Terrain) -> Set[Cell]:
    """锁着的门挡住的门洞：门摆在哪个门洞就挡哪个；还没摆上去的，挡住它那两块地之间所有的门洞。"""
    out: Set[Cell] = set()
    for d in door_pairs(world).values():
        if not d["locked"]:
            continue
        if d.get("cell"):
            out.add(d["cell"])
        else:
            out.update(w.cell for w in t.doorways_between(d["here"], d["other"]))
    return out


# ---------------------------------------------------------------------
# 连通关系从地图来：图里的 ADJACENT_TO / LOCKED_ADJACENT_TO 是地图的「摘要」
# ---------------------------------------------------------------------


def sync_adjacency(world: "Neo4jWorld") -> Dict[str, List[Tuple[str, str]]]:
    """按地图重写地点之间的边：

      走得通（敞开的交界，或开着的门洞）        → ADJACENT_TO（双向）
      只隔着锁着的门才连着                      → LOCKED_ADJACENT_TO（双向，key = 开门的钥匙）
      地图上不通                                → 没有边

    只动两头都在地图上的地点之间的边。返回加了 / 删了哪些对。
    """
    import json

    t = terrain_of(world, build=False)
    if t is None:
        return {}
    on_map = {n for n in t.names if t.cells_of(n)}
    every = {p for p in links_present(t) if p <= on_map}
    open_ = {p for p in links_present(t, locked_door_cells(world, t)) if p <= on_map}
    locked = every - open_
    pairs = door_pairs(world)
    keys: Dict[Any, str] = {}
    for r in world.run_query(
        """
        MATCH (d:Item)-[:LOCATED_AT]->(:Location) WHERE toLower(coalesce(d.item_kind, '')) = 'door'
        RETURN d.name AS door, d.accepts_from AS acc, coalesce(d.key_item, '') AS key
        """
    ) or []:
        try:
            acc = json.loads(r["acc"]) if isinstance(r.get("acc"), str) else (r.get("acc") or [])
        except ValueError:
            acc = []
        key = r["key"] or (str(acc[0]) if acc else "")
        d = pairs.get(r["door"])
        if d and key:
            keys[frozenset((d["here"], d["other"]))] = key

    changes: Dict[str, List[Tuple[str, str]]] = {"added": [], "removed": [], "locked": [], "unlocked": []}
    for rel, want, add_key, drop_key in (("ADJACENT_TO", open_, "added", "removed"),
                                         ("LOCKED_ADJACENT_TO", locked, "locked", "unlocked")):
        have = {
            frozenset((r["a"], r["b"]))
            for r in world.run_query(
                f"MATCH (a:Location)-[:{rel}]-(b:Location) WHERE a.name < b.name RETURN a.name AS a, b.name AS b"
            ) or []
        }
        have = {p for p in have if p <= on_map and len(p) == 2}
        for p in sorted(want, key=sorted):
            a, b = sorted(p)
            extra = " SET r1.key = $key, r2.key = $key" if rel == "LOCKED_ADJACENT_TO" else ""
            world.run_query(
                f"MATCH (a:Location {{name: $a}}), (b:Location {{name: $b}}) "
                f"MERGE (a)-[r1:{rel}]->(b) MERGE (b)-[r2:{rel}]->(a)" + extra,
                a=a, b=b, key=keys.get(p, ""),
            )
            if p not in have:
                changes[add_key].append((a, b))
        for p in sorted(have - want, key=sorted):
            a, b = sorted(p)
            world.run_query(
                f"MATCH (a:Location {{name: $a}})-[r:{rel}]-(b:Location {{name: $b}}) DELETE r", a=a, b=b
            )
            changes[drop_key].append((a, b))
    return changes


def _save_keeping_items(world: "Neo4jWorld", terrain: Terrain) -> None:
    """存改过的地形；外接矩形变了的地点，把地上东西的局部坐标跟着挪，世界位置不变。"""
    before = region_areas(world)
    save_terrain(world, terrain)
    after = region_areas(world)
    for name, (x, y, _, _) in after.items():
        old = before.get(name)
        if old and (old[0], old[1]) != (x, y):
            world.run_query(
                """
                MATCH (i:Item)-[:LOCATED_AT]->(:Location {name: $n})
                WHERE i.x IS NOT NULL AND i.y IS NOT NULL
                SET i.x = i.x + $dx, i.y = i.y + $dy
                """,
                n=name, dx=old[0] - x, dy=old[1] - y,
            )


def _after_map_change(world: "Neo4jWorld", terrain: Terrain) -> Dict[str, Any]:
    _save_keeping_items(world, terrain)
    areas = region_areas(world)
    place_doors(world, areas)
    _settle_items(world, terrain, areas, {})
    place_people(world)  # 站在新砌的墙里的人挪到旁边
    return sync_adjacency(world)


def edit_terrain(world: "Neo4jWorld", changes: Dict[Cell, Tuple[str, Optional[str]]]) -> Dict[str, Any]:
    """改地形（砌墙、拆墙、开门洞、铺地……）：{世界格子: (种类, 地点 / None)}，改完按地图同步连通关系。

    种类见 terrain_gen：'.' 室外地 ',' 室内地板 '+' 门洞 '#' 墙 '%' 篱笆 '~' 水 ' ' 荒野。
    """
    t = terrain_of(world)
    if t is None:
        return {}
    return _after_map_change(world, edit_cells(t, changes))


def connect_places(world: "Neo4jWorld", a: str, b: str) -> Dict[str, Any]:
    """在地图上打通 a、b（已经通了就不动）。"""
    t = terrain_of(world)
    if t is None or a not in t.names or b not in t.names:
        return {}
    new = connect(t, a, b)
    return sync_adjacency(world) if new is t else _after_map_change(world, new)


def separate_places(world: "Neo4jWorld", a: str, b: str) -> Dict[str, Any]:
    """在地图上隔断 a、b（门洞砌上，敞开的交界围起来）。"""
    t = terrain_of(world)
    if t is None or a not in t.names or b not in t.names:
        return {}
    return _after_map_change(world, separate(t, a, b))


def apply_map_changes(
    world: "Neo4jWorld",
    *,
    new_places: bool = False,
    linked: Iterable[Tuple[str, str]] = (),
    unlinked: Iterable[Tuple[str, str]] = (),
    doors_changed: bool = False,
) -> Dict[str, Any]:
    """动作 / 脚本改了世界之后，把地图和连通关系对上（graph_ops.apply_all 调用）。

    新地点 → 落到地图上；脚本连 / 断了两个地点 → 在地图上打通 / 隔断；门的锁变了 → 重新同步。
    这个世界还没有地图（老测试的假世界）就什么都不做。
    """
    if terrain_of(world, build=False) is None:
        return {}
    if new_places:
        layout_regions(world)
    doors = {frozenset((d["here"], d["other"])) for d in door_pairs(world).values()}
    for a, b in unlinked:
        if frozenset((a, b)) in doors:
            continue  # 隔着一扇门：关上 / 锁上就够了，不把门洞砌死
        separate_places(world, a, b)
    for a, b in linked:
        connect_places(world, a, b)
    return sync_adjacency(world)


def region_shapes(world: "Neo4jWorld", areas: Optional[Dict[str, Area]] = None,
                  item_cells: Optional[Dict[str, List[Cell]]] = None) -> Dict[str, Set[Cell]]:
    """每个地点在地形上的地（世界格子，含门洞，不含墙）。"""
    t = terrain_of(world)
    return dict(t.cells_by_region()) if t else {}


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


def resting_spot(walkable: Set[Cell], terrain: Terrain, region: str, who: str,
                 avoid: Iterable[Cell] = ()) -> Optional[Cell]:
    """地点里给某人的落脚点：每人在每个地点有自己固定的一块（按名字散开在地点中间那一半里，不是都挤在正中间），
    避开别人站着 / 正要去的格子和它们旁边一格；不站在门洞里，尽量不贴着墙。"""
    inside = sorted(c for c in terrain.cells_of(region) if c in walkable and terrain.kind_at(c) != DOOR)
    if not inside:
        return None
    roomy = [c for c in inside
             if sum((c[0] + dx, c[1] + dy) in walkable for dx in (-1, 0, 1) for dy in (-1, 0, 1)) >= 8]
    pool = roomy or inside
    # 自己的那块：散在地点中间那一半里（不跑到边上，走路不至于太远）
    mid = terrain.anchor(region) or pool[len(pool) // 2]
    reach = max(3.0, 0.5 * math.sqrt(len(inside) / math.pi))
    near_mid = [c for c in pool if _octile(c, mid) <= reach] or pool
    rnd = random.Random(_stable_hash(f"{who}@{region}"))
    want = near_mid[rnd.randrange(len(near_mid))]
    near_others = {(x + dx, y + dy) for x, y in avoid for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    free = [c for c in pool if c not in near_others] or [c for c in inside if c not in set(avoid)] or inside
    return min(free, key=lambda c: (_octile(c, want), c))


def people_cells(world: "Neo4jWorld", *, besides: str = "") -> Set[Cell]:
    """别人站着的格子，和正在走去的终点。"""
    import json

    rows = world.run_query(
        """
        MATCH (p:Person)
        WHERE p.name <> $me AND p.x IS NOT NULL AND p.y IS NOT NULL
        RETURN p.x AS x, p.y AS y, p.walk_path AS path
        """,
        me=besides,
    ) or []
    out: Set[Cell] = set()
    for r in rows:
        out.add((_int(r["x"]), _int(r["y"])))
        if r.get("path"):
            try:
                path = json.loads(r["path"])
                if path:
                    out.add((int(path[-1][0]), int(path[-1][1])))
            except (ValueError, TypeError, IndexError):
                pass
    return out


def person_cell(world: "Neo4jWorld", name: str) -> Optional[Cell]:
    """人的世界坐标；没有、或不在自己所在地点的地上（被脚本挪走过）时就地补一个并存下。"""
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
    t = terrain_of(world)
    loc = r.get("loc") or ""
    if t is None or not t.cells_of(loc):
        return None
    walkable = walkable_cells(world)
    if r.get("x") is not None and r.get("y") is not None:
        cell = (_int(r["x"]), _int(r["y"]))
        if t.owner_at(cell) == loc and cell in walkable:
            return cell
    spot = resting_spot(walkable, t, loc, name, avoid=people_cells(world, besides=name))
    if spot:
        set_person_cell(world, name, spot)
    return spot


def set_person_cell(world: "Neo4jWorld", name: str, cell: Cell) -> None:
    world.run_query("MATCH (p:Person {name: $n}) SET p.x = $x, p.y = $y", n=name, x=cell[0], y=cell[1])


# ---------------------------------------------------------------------
# 走在路上：Person.x / y 还是出发的那一格，走到哪了按时间算（Person.walk_*）
# ---------------------------------------------------------------------


def start_walk(world: "Neo4jWorld", name: str, path: List[Cell], started_at: float, seconds: float) -> None:
    import json

    world.run_query(
        "MATCH (p:Person {name: $n}) SET p.walk_path = $path, p.walk_t0 = $t0, p.walk_secs = $secs",
        n=name, path=json.dumps([list(c) for c in path]), t0=float(started_at), secs=float(seconds),
    )


def end_walk(world: "Neo4jWorld", name: str) -> None:
    world.run_query("MATCH (p:Person {name: $n}) REMOVE p.walk_path, p.walk_t0, p.walk_secs", n=name)


def walked_cell(path: List[Cell], started_at: float, seconds: float, now: float) -> Cell:
    """按时间算走到了路上哪一格（匀速）。"""
    if not path:
        raise ValueError("empty path")
    frac = 1.0 if seconds <= 0 else min(1.0, max(0.0, (now - started_at) / seconds))
    return tuple(path[min(len(path) - 1, int(frac * (len(path) - 1)))])  # type: ignore[return-value]


def current_cell(world: "Neo4jWorld", name: str, now: float) -> Optional[Cell]:
    """这个人此刻站在哪：正在走路就是路上走到的那一格，否则是 person_cell。"""
    import json

    rows = world.run_query(
        "MATCH (p:Person {name: $n}) RETURN p.walk_path AS path, p.walk_t0 AS t0, p.walk_secs AS secs", n=name
    ) or []
    if rows and rows[0].get("path"):
        path = [tuple(c) for c in json.loads(rows[0]["path"])]
        if path:
            return walked_cell(path, float(rows[0]["t0"] or 0), float(rows[0]["secs"] or 0), now)
    return person_cell(world, name)


def stop_walk_here(world: "Neo4jWorld", name: str, now: float) -> Optional[Cell]:
    """路走到一半停下（死了、被打断）：就停在走到的地方，人算在那块地上。"""
    rows = world.run_query("MATCH (p:Person {name: $n}) RETURN p.walk_path IS NOT NULL AS w", n=name) or []
    if not rows or not rows[0].get("w"):
        return person_cell(world, name)
    cell = current_cell(world, name, now)
    end_walk(world, name)
    if cell is None:
        return None
    set_person_cell(world, name, cell)
    region = region_at(world, cell)
    if region and region != person_region_name(world, name):
        world.run_query(
            """
            MATCH (p:Person {name: $n})
            OPTIONAL MATCH (p)-[r:LOCATED_AT]->()
            DELETE r
            WITH DISTINCT p
            MATCH (l:Location {name: $loc})
            MERGE (p)-[:LOCATED_AT]->(l)
            """,
            n=name, loc=region,
        )
    return cell


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
    """执行这个动作之前要走的路；不用走（或这个世界没有地形）返回 None。"""
    from content import get_content

    t = terrain_of(world)
    if t is None:
        return None
    start = person_cell(world, actor)
    if start is None:
        return None
    walkable = walkable_cells(world) | {start}
    capability = get_content().capability_of(action_name)

    others = people_cells(world, besides=actor)
    if capability == "travel":
        target = str(args[2]) if len(args) > 2 else ""
        spot = resting_spot(walkable, t, target, actor, avoid=others) if target in t.names else None
        goals = {spot} if spot else set()
    else:
        idx = _APPROACH_BY_NAME.get(action_name, _APPROACH_ARG.get(capability or ""))
        if idx is None or idx >= len(args) or str(args[idx]) == actor:
            return None
        target_cells = entity_world_cells(world, str(args[idx]))
        if not target_cells:
            return None
        if any(_octile(start, c) <= REACH for c in target_cells):
            return None
        goals = {
            (tx + dx, ty + dy)
            for tx, ty in target_cells
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if (tx + dx, ty + dy) not in target_cells
        }
        goals = (goals - others) or goals  # 别站到已经有人的那一格上
    path = find_path(walkable, start, goals)
    if not path or len(path) < 2:
        return None
    return Walk(path=path, seconds=path_length(path) / WALK_CELLS_PER_SEC)


def move_person_freely(world: "Neo4jWorld", name: str, cell: Cell) -> Dict[str, Any]:
    """玩家在无缝世界里自由走动：写下坐标；走进别的地点就换所在地点（不要求走 move 动作）。"""
    if cell not in walkable_cells(world):
        return {"ok": False, "error": "Can't stand there"}
    region = region_at(world, cell)
    before = person_region_name(world, name)
    if region and before and region != before and not open_between(world, before, region):
        return {"ok": False, "error": f"There is no open way from {before} into {region}"}
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
    """离 name 不超过 radius 格、又没隔着墙的其他活人；这个世界没有地形时返回 None。

    隔着墙 = 两人在不同的地点，其中一处在屋里（屋里的人看不见屋外，反过来也一样）。
    """
    me = person_cell(world, name)
    t = terrain_of(world)
    if me is None or t is None:
        return None
    mine = t.owner_at(me)
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
        if cell is None or _octile(me, cell) > radius:
            continue
        theirs = t.owner_at(cell)
        if theirs != mine and (mine in t.indoor or theirs in t.indoor):
            continue
        out.append(r["name"])
    return sorted(out)
