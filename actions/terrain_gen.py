"""
地形生成 — 把一组地点变成一张像真实地图那样的世界格子。

  · 室外地点（村子、森林、田地……）从各自的中心往外「长」，边界顺着噪声弯曲，
    彼此咬合成一整片地，而不是一排矩形再挖几个洞；
  · 室内地点（酒馆、仓库、地窖……）是有墙的房子，嵌在它所属的室外地点里；
    连着的几间屋子是同一栋房子，共用墙，墙上开门；
  · 墙、篱笆、水都是挡路的格子（障碍物），不属于任何地点；
    只有相邻的地点之间才走得通：敞开的边界、门洞，或篱笆上的一道门；
    挨着却不相邻的两块室外地之间是一排篱笆。

纯几何、不碰数据库：spatial.layout_regions 负责从图库读地点、写回结果。
同样的输入永远生成同样的地图（按名字取种子，没有随机数）。
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

Cell = Tuple[int, int]
Pair = FrozenSet[str]

# 格子种类
VOID = " "      # 地图外 / 荒野，走不了
GROUND = "."    # 室外的地
FLOOR = ","     # 室内地板
DOOR = "+"      # 门洞（墙或篱笆上能走过去的一格；锁着的门挡在这里）
WALL = "#"      # 房子的墙
FENCE = "%"     # 篱笆 / 树篱：室外地点之间的隔断
WATER = "~"     # 水（只在手画地图里出现）

WALKABLE = frozenset((GROUND, FLOOR, DOOR))
OBSTACLES = frozenset((WALL, FENCE, WATER))

DEFAULT_SIZE = (20, 12)
# 室内房间按地点格子尺寸缩小，再夹在这个范围里
ROOM_MIN = (5, 4)
ROOM_MAX = (12, 9)

_DIRS4 = ((1, 0), (-1, 0), (0, 1), (0, -1))
_DIRS8 = _DIRS4 + ((1, 1), (1, -1), (-1, 1), (-1, -1))


@dataclass
class Place:
    name: str
    size: Tuple[int, int] = DEFAULT_SIZE   # 大概多大（格子），决定面积
    indoor: bool = False
    parent: str = ""                       # 室内地点在哪块室外地里
    seed: Optional[Tuple[float, float]] = None   # 中心（世界格子），手摆过的世界用
    map: Optional[Tuple[float, float]] = None    # 大地图坐标（y 向北），按比例换算成中心


@dataclass
class Doorway:
    cell: Cell
    owner: str   # 门洞那一格算在哪个地点里（门这个物品也摆在那边）
    other: str   # 门通向哪里

    def pair(self) -> Pair:
        return frozenset((self.owner, self.other))


@dataclass
class Terrain:
    width: int
    height: int
    names: List[str]
    owner: List[List[int]]   # 每格属于哪个地点（names 的下标），-1 = 不属于任何地点
    kind: List[List[str]]
    indoor: Set[str] = field(default_factory=set)
    parents: Dict[str, str] = field(default_factory=dict)
    doorways: List[Doorway] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._cells: Optional[Dict[str, Set[Cell]]] = None

    # ---- 查询 ----

    def inside(self, cell: Cell) -> bool:
        return 0 <= cell[0] < self.width and 0 <= cell[1] < self.height

    def kind_at(self, cell: Cell) -> str:
        return self.kind[cell[1]][cell[0]] if self.inside(cell) else VOID

    def owner_at(self, cell: Cell) -> Optional[str]:
        if not self.inside(cell):
            return None
        i = self.owner[cell[1]][cell[0]]
        return self.names[i] if i >= 0 else None

    def cells_by_region(self) -> Dict[str, Set[Cell]]:
        if self._cells is None:
            out: Dict[str, Set[Cell]] = {n: set() for n in self.names}
            for y in range(self.height):
                row_o, row_k = self.owner[y], self.kind[y]
                for x in range(self.width):
                    i = row_o[x]
                    if i >= 0 and row_k[x] in WALKABLE:
                        out[self.names[i]].add((x, y))
            self._cells = out
        return self._cells

    def cells_of(self, name: str) -> Set[Cell]:
        return self.cells_by_region().get(name, set())

    def walkable(self) -> Set[Cell]:
        return set().union(*self.cells_by_region().values()) if self.names else set()

    def bbox(self, name: str) -> Optional[Tuple[int, int, int, int]]:
        cells = self.cells_of(name)
        if not cells:
            return None
        xs, ys = [c[0] for c in cells], [c[1] for c in cells]
        return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1

    def anchor(self, name: str) -> Optional[Cell]:
        """地点的「中间」：离重心最近、周围也是这块地的格子（标签、落脚点用）。"""
        cells = self.cells_of(name)
        if not cells:
            return None
        cx = sum(c[0] for c in cells) / len(cells)
        cy = sum(c[1] for c in cells) / len(cells)

        def score(c: Cell) -> Tuple[float, Cell]:
            open_around = sum((c[0] + dx, c[1] + dy) in cells for dx, dy in _DIRS8)
            return (math.hypot(c[0] - cx, c[1] - cy) + (8 - open_around) * 0.75, c)

        return min(cells, key=score)

    def doorways_between(self, a: str, b: str) -> List[Doorway]:
        return [d for d in self.doorways if d.pair() == frozenset((a, b))]

    def kind_rows(self) -> List[str]:
        return ["".join(row) for row in self.kind]

    # ---- 存取（图库里存成几串字符串） ----

    def to_props(self) -> Dict[str, object]:
        import json

        return {
            "width": self.width,
            "height": self.height,
            "names": list(self.names),
            "owner_rows": ["".join(_owner_char(i) for i in row) for row in self.owner],
            "kind_rows": self.kind_rows(),
            "indoor": sorted(self.indoor),
            "parents": json.dumps(self.parents, ensure_ascii=False),
            "doorways": json.dumps([[d.cell[0], d.cell[1], d.owner, d.other] for d in self.doorways],
                                   ensure_ascii=False),
        }

    @classmethod
    def from_props(cls, props: Dict[str, object]) -> "Terrain":
        import json

        names = list(props.get("names") or [])
        owner = [[_owner_index(ch) for ch in row] for row in props.get("owner_rows") or []]
        kind = [list(row) for row in props.get("kind_rows") or []]
        doorways = [
            Doorway((int(x), int(y)), str(o), str(t))
            for x, y, o, t in json.loads(str(props.get("doorways") or "[]"))
        ]
        return cls(
            width=int(props.get("width") or 0),
            height=int(props.get("height") or 0),
            names=names,
            owner=owner,
            kind=kind,
            indoor=set(props.get("indoor") or []),
            parents=dict(json.loads(str(props.get("parents") or "{}"))),
            doorways=doorways,
        )


def _owner_char(i: int) -> str:
    if i < 0:
        return " "
    return chr(0x30 + i) if i < 0x4E else chr(0x100 + i)


def _owner_index(ch: str) -> int:
    o = ord(ch)
    if ch == " ":
        return -1
    return o - 0x30 if o < 0x7E else o - 0x100


# =====================================================================
# 小工具：稳定哈希、噪声
# =====================================================================


def stable_hash(text: str) -> int:
    h = 5381
    for ch in text.encode("utf-8"):
        h = (h * 33 + ch) & 0xFFFFFFFF
    return h


def _mix(*nums: int) -> int:
    h = 0x9E3779B9
    for n in nums:
        h ^= (n & 0xFFFFFFFF) + 0x9E3779B9 + ((h << 6) & 0xFFFFFFFF) + (h >> 2)
        h &= 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 0x45D9F3B) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def _noise(width: int, height: int, salt: int) -> List[List[float]]:
    """分形值噪声，0..1。"""
    octaves = ((9, 0.45), (5, 0.35), (3, 0.2))
    out = [[0.0] * width for _ in range(height)]
    for o, (cell, amp) in enumerate(octaves):
        def lat(ix: int, iy: int) -> float:
            return (_mix(ix, iy, o, salt) % 10007) / 10006.0

        for y in range(height):
            gy, fy = divmod(y, cell)
            ty = fy / cell
            ty = ty * ty * (3 - 2 * ty)
            for x in range(width):
                gx, fx = divmod(x, cell)
                tx = fx / cell
                tx = tx * tx * (3 - 2 * tx)
                a = lat(gx, gy) + (lat(gx + 1, gy) - lat(gx, gy)) * tx
                b = lat(gx, gy + 1) + (lat(gx + 1, gy + 1) - lat(gx, gy + 1)) * tx
                out[y][x] += amp * (a + (b - a) * ty)
    return out


# =====================================================================
# 生成
# =====================================================================


class _Canvas:
    """生成过程中的格子板：owner / kind 两层。"""

    def __init__(self, width: int, height: int, names: List[str]):
        self.w, self.h = width, height
        self.names = names
        self.index = {n: i for i, n in enumerate(names)}
        self.owner = [[-1] * width for _ in range(height)]
        self.kind = [[VOID] * width for _ in range(height)]
        self.doorways: List[Doorway] = []

    def inside(self, c: Cell) -> bool:
        return 0 <= c[0] < self.w and 0 <= c[1] < self.h

    def own(self, c: Cell) -> int:
        return self.owner[c[1]][c[0]] if self.inside(c) else -1

    def kd(self, c: Cell) -> str:
        return self.kind[c[1]][c[0]] if self.inside(c) else VOID

    def set(self, c: Cell, owner: int, kind: str) -> None:
        self.owner[c[1]][c[0]] = owner
        self.kind[c[1]][c[0]] = kind

    def cells(self, i: int, kinds: Iterable[str] = (GROUND,)) -> Set[Cell]:
        kinds = set(kinds)
        return {(x, y) for y in range(self.h) for x in range(self.w)
                if self.owner[y][x] == i and self.kind[y][x] in kinds}

    def terrain(self, indoor: Set[str], parents: Dict[str, str]) -> Terrain:
        return Terrain(self.w, self.h, list(self.names), self.owner, self.kind,
                       indoor=set(indoor), parents=dict(parents), doorways=list(self.doorways))


def _area_of(p: Place) -> int:
    w, h = p.size
    return max(16, int(w) * int(h))


def _room_size(p: Place) -> Tuple[int, int]:
    w, h = p.size
    rw = min(ROOM_MAX[0], max(ROOM_MIN[0], round(w * 0.55)))
    rh = min(ROOM_MAX[1], max(ROOM_MIN[1], round(h * 0.6)))
    return rw, rh


def generate(
    places: List[Place],
    links: Dict[Pair, str],
    door_side: Optional[Dict[Pair, str]] = None,
    *,
    salt: str = "",
) -> Terrain:
    """整张地图。links：相邻的一对地点 → "open"（敞开）/ "door"（隔着一扇门）；没列出的就是不通。

    door_side：隔着门的一对里，门洞（和门）算在哪一边（通常是门这个物品所在的地点）。
    """
    door_side = dict(door_side or {})
    names = sorted({p.name for p in places})
    by_name = {p.name: p for p in places}
    outdoor = [n for n in names if not by_name[n].indoor]
    indoor = [n for n in names if by_name[n].indoor]
    salt_n = stable_hash(salt + "|" + ",".join(names))

    clusters = _indoor_clusters(indoor, links)
    plans = []
    parents: Dict[str, str] = {}
    for members in clusters:
        parent = _cluster_parent(members, by_name, outdoor, links)
        for m in members:
            if parent:
                parents[m] = parent
        plans.append((members, parent, _plan_building(members, by_name, links, door_side)))

    if not outdoor:
        canvas = _canvas_for_buildings(names, [p for _, _, p in plans])
        x = 2
        for members, _, plan in plans:
            _stamp_building(canvas, plan, (x, 2))
            x += plan.w + 2
        return canvas.terrain(set(indoor), parents)

    # 室外地：面积 = 自己的 + 嵌在里面的房子
    area = {n: _area_of(by_name[n]) for n in outdoor}
    # 房子里的屋子还连着别的室外地（后门）：那块地要挨着房子所在的地
    via_house: Dict[Pair, str] = {}
    for members, parent, plan in plans:
        if parent:
            area[parent] += int(plan.w * plan.h * 2.0) + 3 * (plan.w + plan.h)
            for m in members:
                for o in outdoor:
                    if o != parent and frozenset((m, o)) in links and frozenset((parent, o)) not in links:
                        via_house[frozenset((parent, o))] = "house"
    layout_links = {**via_house, **links}
    radius = {n: math.sqrt(area[n] / math.pi) for n in outdoor}
    seeds = _layout_seeds([by_name[n] for n in outdoor], radius, layout_links)

    biggest = max([max(p.w, p.h) for _, _, p in plans] + [0])
    margin = 4 + biggest
    min_x = min(seeds[n][0] - 1.7 * radius[n] for n in outdoor) - margin
    min_y = min(seeds[n][1] - 1.7 * radius[n] for n in outdoor) - margin
    max_x = max(seeds[n][0] + 1.7 * radius[n] for n in outdoor) + margin
    max_y = max(seeds[n][1] + 1.7 * radius[n] for n in outdoor) + margin
    width, height = int(max_x - min_x) + 1, int(max_y - min_y) + 1
    canvas = _Canvas(width, height, names)
    seed_cells = {n: (int(round(seeds[n][0] - min_x)), int(round(seeds[n][1] - min_y))) for n in outdoor}

    _grow(canvas, {canvas.index[n]: seed_cells[n] for n in outdoor},
          {canvas.index[n]: area[n] for n in outdoor},
          {canvas.index[n]: radius[n] for n in outdoor},
          _noise(width, height, salt_n), salt_n)
    _smooth(canvas, [canvas.index[n] for n in outdoor])
    _keep_largest_parts(canvas, [canvas.index[n] for n in outdoor])
    _connect_linked(canvas, outdoor, layout_links)

    open_anyway: Set[Pair] = set()
    fence_side: Dict[Pair, str] = {}
    # 要开后门的房子先挑地方（挨着两块地的位置少）
    def back_doors(entry) -> int:
        members, parent, _ = entry
        return len({o for m in members for o in outdoor if o != parent and frozenset((m, o)) in links})

    for members, parent, plan in sorted(plans, key=lambda e: (-back_doors(e), e[0][0])):
        linked_out = sorted({o for m in members for o in outdoor if frozenset((m, o)) in links})
        needs = {o: [m for m in members if frozenset((m, o)) in links] for o in linked_out}
        spot = _site_building(canvas, plan, parent, [o for o in linked_out if o != parent], needs)
        if spot is None:
            continue
        _stamp_building(canvas, plan, spot)
        missing = _open_entrances(canvas, plan, spot, members, outdoor, links, door_side, parent)
        for o in linked_out:
            if parent and o != parent and o not in missing:
                fence_side[frozenset((parent, o))] = parent  # 后门门口在那块地上：篱笆围在房子这一侧
        # 后门没开成（房子挨不到那块地）：那就让房子所在的地和它直接相通，免得走不到
        for o in missing:
            if parent and o != parent:
                open_anyway.add(frozenset((parent, o)))
    fence_links = {**{p: "open" for p in open_anyway}, **links}
    _fence_borders(canvas, outdoor, fence_links, door_side, area, fence_side)
    _repair_links(canvas, links, door_side, set(indoor))
    return _crop(canvas, set(indoor), parents, 0)[0]


# ---------------------------------------------------------------------
# 室外：摆中心、长地块
# ---------------------------------------------------------------------


def _layout_seeds(outdoor: List[Place], radius: Dict[str, float], links: Dict[Pair, str]) -> Dict[str, Tuple[float, float]]:
    """每块室外地的中心。手摆过的（seed / map）按原位置微调；其余按连接关系弹簧布局。"""
    names = [p.name for p in outdoor]
    pos: Dict[str, List[float]] = {}
    for p in outdoor:
        if p.seed is not None:
            pos[p.name] = [float(p.seed[0]), float(p.seed[1])]
    mapped = [p for p in outdoor if p.seed is None and p.map is not None]
    if len(mapped) >= 2 or (mapped and pos):
        ratios = []
        for i, a in enumerate(mapped):
            for b in mapped[i + 1:]:
                d = math.hypot(a.map[0] - b.map[0], a.map[1] - b.map[1])
                if d > 1e-6 and frozenset((a.name, b.name)) in links:
                    ratios.append((radius[a.name] + radius[b.name]) * 0.9 / d)
        if not ratios:
            ratios = [(radius[a.name] + radius[b.name]) * 0.9 / max(1e-6, math.hypot(a.map[0] - b.map[0], a.map[1] - b.map[1]))
                      for i, a in enumerate(mapped) for b in mapped[i + 1:]] or [10.0]
        scale = sorted(ratios)[len(ratios) // 2]
        for p in mapped:
            pos[p.name] = [p.map[0] * scale, -p.map[1] * scale]
    authored = set(pos)

    # 没有位置的：贴着已经有位置的邻居放，一个都没有就排一圈
    todo = [n for n in sorted(names, key=lambda n: (-sum(1 for k in links if n in k), n)) if n not in pos]
    ring = sum(radius.values()) / math.pi
    for k, n in enumerate(todo):
        placed_nbrs = [m for m in pos if frozenset((n, m)) in links]
        if placed_nbrs:
            ax = sum(pos[m][0] for m in placed_nbrs) / len(placed_nbrs)
            ay = sum(pos[m][1] for m in placed_nbrs) / len(placed_nbrs)
            ang = (stable_hash(n) % 360) * math.pi / 180
            dist = radius[n] + max(radius[m] for m in placed_nbrs)
            pos[n] = [ax + math.cos(ang) * dist, ay + math.sin(ang) * dist]
        elif not pos:
            pos[n] = [0.0, 0.0]
        else:
            ang = 2 * math.pi * k / max(1, len(todo))
            pos[n] = [math.cos(ang) * ring, math.sin(ang) * ring]

    iters = 160 if len(authored) == len(names) else 420
    for it in range(iters):
        step = 0.25 * (1 - it / iters) + 0.03
        disp = {n: [0.0, 0.0] for n in names}
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                dx, dy = pos[b][0] - pos[a][0], pos[b][1] - pos[a][1]
                d = math.hypot(dx, dy)
                if d < 1e-6:
                    ang = (stable_hash(a + b) % 360) * math.pi / 180
                    dx, dy, d = math.cos(ang), math.sin(ang), 1.0
                want = radius[a] + radius[b]
                if frozenset((a, b)) in links:
                    f = d - want * 0.72
                elif d < want * 1.15:
                    f = d - want * 1.15
                else:
                    continue
                ux, uy = dx / d, dy / d
                disp[a][0] += f * ux * 0.5
                disp[a][1] += f * uy * 0.5
                disp[b][0] -= f * ux * 0.5
                disp[b][1] -= f * uy * 0.5
        for n in names:
            k = step * (0.35 if n in authored else 1.0)
            mx, my = disp[n][0] * k, disp[n][1] * k
            cap = radius[n] * 0.5
            mag = math.hypot(mx, my)
            if mag > cap:
                mx, my = mx * cap / mag, my * cap / mag
            pos[n][0] += mx
            pos[n][1] += my
    return {n: (pos[n][0], pos[n][1]) for n in names}


def _grow(canvas: _Canvas, seeds: Dict[int, Cell], quota: Dict[int, int], radius: Dict[int, float],
          noise: List[List[float]], salt: int) -> None:
    """所有地块同时从中心往外长（代价按各自大小归一、乘上噪声），长满面积就停。

    一起长、先到先得，所以地块一定连成一片，边界落在噪声的「山谷」里，彎彎曲曲地咬合。
    """
    count = {i: 0 for i in seeds}
    best: Dict[Tuple[int, Cell], float] = {}
    # 每块地一点各向异性（有的狭长、有的偏圆），方向按名字定
    stretch = {}
    for i in seeds:
        h = _mix(i, salt, 77)
        ang = (h % 360) * math.pi / 180
        amount = 0.15 + (h >> 9) % 100 / 400.0
        stretch[i] = (ang, amount)
    heap: List[Tuple[float, float, int, int, int]] = []
    taken: Set[Cell] = set()
    for i, c in sorted(seeds.items()):
        while c in taken:
            c = (c[0] + 1, c[1])
        taken.add(c)
        best[(i, c)] = 0.0
        heapq.heappush(heap, (0.0, 0.0, i, c[0], c[1]))
    dir_cost = {}
    for i, (ang, amount) in stretch.items():
        for dx, dy in _DIRS8:
            a = math.atan2(dy, dx)
            dir_cost[(i, dx, dy)] = (1.0 + amount * math.cos(2 * (a - ang))) * (1.4142 if dx and dy else 1.0)
    while heap:
        _, g, i, x, y = heapq.heappop(heap)
        if canvas.owner[y][x] != -1 or count[i] >= quota[i]:
            continue
        canvas.set((x, y), i, GROUND)
        count[i] += 1
        for dx, dy in _DIRS8:
            nx, ny = x + dx, y + dy
            if not (1 <= nx < canvas.w - 1 and 1 <= ny < canvas.h - 1) or canvas.owner[ny][nx] != -1:
                continue
            ng = g + dir_cost[(i, dx, dy)] * (0.25 + 1.5 * noise[ny][nx])
            if ng < best.get((i, (nx, ny)), math.inf):
                best[(i, (nx, ny))] = ng
                heapq.heappush(heap, (ng / radius[i], ng, i, nx, ny))


def _smooth(canvas: _Canvas, regions: List[int], passes: int = 2) -> None:
    """去掉一格宽的尖刺、补上小坑。"""
    ours = set(regions)
    for _ in range(passes):
        changes = []
        for y in range(canvas.h):
            for x in range(canvas.w):
                o = canvas.owner[y][x]
                if o != -1 and o not in ours:
                    continue
                nb = [canvas.own((x + dx, y + dy)) for dx, dy in _DIRS4]
                same = nb.count(o)
                if o != -1 and same <= 1:
                    others = [n for n in nb if n != o and (n == -1 or n in ours)]
                    if others:
                        new = max(set(others), key=lambda n: (others.count(n), n))
                        changes.append(((x, y), new))
                elif o == -1:
                    for n in set(nb):
                        if n in ours and nb.count(n) >= 3:
                            changes.append(((x, y), n))
                            break
        for c, n in changes:
            canvas.set(c, n, GROUND if n != -1 else VOID)


def _components(cells: Set[Cell], dirs=_DIRS4) -> List[Set[Cell]]:
    left, out = set(cells), []
    while left:
        start = left.pop()
        comp, queue = {start}, deque([start])
        while queue:
            x, y = queue.popleft()
            for dx, dy in dirs:
                n = (x + dx, y + dy)
                if n in left:
                    left.discard(n)
                    comp.add(n)
                    queue.append(n)
        out.append(comp)
    return out


def _keep_largest_parts(canvas: _Canvas, regions: List[int]) -> None:
    for i in regions:
        parts = sorted(_components(canvas.cells(i)), key=len, reverse=True)
        for part in parts[1:]:
            for c in part:
                canvas.set(c, -1, VOID)


def _touching(canvas: _Canvas, a: int, b: int) -> bool:
    for (x, y) in canvas.cells(a):
        for dx, dy in _DIRS4:
            if canvas.own((x + dx, y + dy)) == b:
                return True
    return False


def _connect_linked(canvas: _Canvas, outdoor: List[str], links: Dict[Pair, str],
                    prefer: Iterable[str] = ()) -> None:
    """相邻（能走）的两块室外地却没挨着：从荒野里开一条三格宽的小路把它们接上。

    prefer：一对里有它（新加的地点）时整条路都算它的，已有的地点一格不多。
    """
    prefer = set(prefer)
    for pair in sorted(links, key=lambda p: tuple(sorted(p))):
        if len(pair) != 2 or not pair <= set(outdoor):
            continue
        a, b = sorted(pair)
        ia, ib = canvas.index[a], canvas.index[b]
        if _touching(canvas, ia, ib):
            continue
        def clear_of_others(c: Cell) -> bool:
            # 小路（连同加宽的两侧）不贴着第三块地，免得之后围篱笆把路截断
            return all(canvas.own((c[0] + dx, c[1] + dy)) in (-1, ia, ib)
                       for dx in range(-2, 3) for dy in range(-2, 3))

        goal = None
        for strict in (True, False):
            prev: Dict[Cell, Optional[Cell]] = {}
            queue = deque()
            for c in sorted(canvas.cells(ia)):
                for dx, dy in _DIRS4:
                    n = (c[0] + dx, c[1] + dy)
                    if canvas.inside(n) and canvas.own(n) == -1 and canvas.kd(n) == VOID and n not in prev                             and (not strict or clear_of_others(n)):
                        prev[n] = None
                        queue.append(n)
            while queue:
                cur = queue.popleft()
                if any(canvas.own((cur[0] + dx, cur[1] + dy)) == ib for dx, dy in _DIRS4):
                    goal = cur
                    break
                for dx, dy in _DIRS4:
                    n = (cur[0] + dx, cur[1] + dy)
                    if 2 <= n[0] < canvas.w - 2 and 2 <= n[1] < canvas.h - 2 and canvas.own(n) == -1                             and canvas.kd(n) == VOID and n not in prev and (not strict or clear_of_others(n)):
                        prev[n] = cur
                        queue.append(n)
            if goal is not None:
                break
        if goal is None:
            continue
        path = [goal]
        while prev[path[-1]] is not None:
            path.append(prev[path[-1]])  # type: ignore[arg-type]
        path.reverse()  # 从 a 这边到 b 那边
        half = (len(path) + 1) // 2
        if b in prefer or a in prefer:
            half = 0 if b in prefer else len(path)
        owners = {c: (ia if k < half else ib) for k, c in enumerate(path)}
        for c, o in list(owners.items()):
            for dx, dy in _DIRS4:
                n = (c[0] + dx, c[1] + dy)
                if canvas.inside(n) and canvas.own(n) == -1 and n not in owners:
                    owners[n] = o
        for c, o in owners.items():
            canvas.set(c, o, GROUND)


def _fence_borders(canvas: _Canvas, outdoor: List[str], links: Dict[Pair, str],
                   door_side: Dict[Pair, str], area: Dict[str, int],
                   fence_side: Optional[Dict[Pair, str]] = None) -> None:
    """挨着的两块室外地：相邻且敞开就不管；隔着门就围一排篱笆留一道门；不相邻就是一整排篱笆。

    先把所有交界的篱笆都算出来再一起围上，最后才挑门的位置，免得后围的篱笆把门堵死。
    """
    gates_wanted = []
    fence: Set[Cell] = set()
    for i, a in enumerate(outdoor):
        for b in outdoor[i + 1:]:
            pair = frozenset((a, b))
            status = links.get(pair)
            if status == "open":
                continue
            ia, ib = canvas.index[a], canvas.index[b]
            if not _touching(canvas, ia, ib):
                continue
            if status == "door":
                side = door_side.get(pair) or a
            else:
                side = (fence_side or {}).get(pair) or (a if (area[a], a) < (area[b], b) else b)
            other = b if side == a else a
            iside, iother = canvas.index[side], canvas.index[other]
            line = {c for c in canvas.cells(iside)
                    if any(canvas.own((c[0] + dx, c[1] + dy)) == iother for dx, dy in _DIRS8)}
            fence |= line
            if status == "door":
                gates_wanted.append((side, other, line))
    for c in fence:
        canvas.set(c, -1, FENCE)
    outdoor_idx = {canvas.index[n] for n in outdoor}
    for side, other, line in gates_wanted:
        iside, iother = canvas.index[side], canvas.index[other]
        gates = []
        for c in sorted(line):
            # 门旁边不能挨着第三块地（不然门就成了通往那里的缺口）
            near = {canvas.own((c[0] + dx, c[1] + dy)) for dx, dy in _DIRS8}
            if (near & outdoor_idx) - {iside, iother}:
                continue
            for dx, dy in _DIRS4:
                out_c, in_c = (c[0] + dx, c[1] + dy), (c[0] - dx, c[1] - dy)
                if canvas.own(out_c) == iother and canvas.kd(out_c) == GROUND                         and canvas.own(in_c) == iside and canvas.kd(in_c) == GROUND:
                    gates.append(c)
                    break
        if gates:
            gate = gates[len(gates) // 2]
            canvas.set(gate, iside, DOOR)
            canvas.doorways.append(Doorway(gate, side, other))


# ---------------------------------------------------------------------
# 室内：房子
# ---------------------------------------------------------------------


@dataclass
class _Building:
    rooms: Dict[str, Tuple[int, int, int, int]]   # 房间地板（局部坐标）x, y, w, h
    cells: Dict[Cell, Tuple[Optional[str], str]]  # 局部格子 → (房间 / None, 种类)
    inner: List[Doorway]
    w: int
    h: int


def _indoor_clusters(indoor: List[str], links: Dict[Pair, str]) -> List[List[str]]:
    """连着的室内地点 = 同一栋房子。"""
    left, out = set(indoor), []
    for start in sorted(indoor):
        if start not in left:
            continue
        left.discard(start)
        comp, queue = [start], deque([start])
        while queue:
            cur = queue.popleft()
            for other in sorted(left):
                if frozenset((cur, other)) in links:
                    left.discard(other)
                    comp.append(other)
                    queue.append(other)
        out.append(comp)
    return out


def _cluster_parent(members: List[str], by_name: Dict[str, Place], outdoor: List[str],
                    links: Dict[Pair, str]) -> str:
    for m in members:
        p = by_name[m].parent
        if p and p in outdoor:
            return p
    scores = {o: sum(1 for m in members if frozenset((m, o)) in links) for o in outdoor}
    best = max(sorted(scores), key=lambda o: scores[o], default="")
    return best if best and scores[best] > 0 else ""


def _plan_building(members: List[str], by_name: Dict[str, Place], links: Dict[Pair, str],
                   door_side: Dict[Pair, str]) -> _Building:
    """屋子一间挨一间排（中间隔一道墙），连着的两间之间开门。"""
    degree = {m: sum(1 for o in members if o != m and frozenset((m, o)) in links) for m in members}
    root = max(sorted(members), key=lambda m: degree[m])
    order, seen, anchor_of = [root], {root}, {}
    queue = deque([root])
    while queue:
        cur = queue.popleft()
        for other in sorted(members):
            if other not in seen and frozenset((cur, other)) in links:
                seen.add(other)
                anchor_of[other] = cur
                order.append(other)
                queue.append(other)
    order += [m for m in sorted(members) if m not in seen]

    rooms: Dict[str, Tuple[int, int, int, int]] = {}

    def clear(r: Tuple[int, int, int, int]) -> bool:
        x, y, w, h = r
        for ox, oy, ow, oh in rooms.values():
            # 加上一圈墙也不能压到别的房间
            if x - 1 < ox + ow and ox < x + w + 1 and y - 1 < oy + oh and oy < y + h + 1:
                return False
        return True

    for m in order:
        rw, rh = _room_size(by_name[m])
        if not rooms:
            rooms[m] = (0, 0, rw, rh)
            continue
        anchors = [anchor_of[m]] if m in anchor_of else []
        anchors += [a for a in rooms if a not in anchors]
        spot = None
        rot = stable_hash(m) % 4
        for a in anchors:
            ax, ay, aw, ah = rooms[a]
            sides = [
                [(ax + aw + 1, ay), (ax + aw + 1, ay + ah - rh), (ax + aw + 1, ay + (ah - rh) // 2)],
                [(ax, ay + ah + 1), (ax + aw - rw, ay + ah + 1), (ax + (aw - rw) // 2, ay + ah + 1)],
                [(ax - rw - 1, ay), (ax - rw - 1, ay + ah - rh), (ax - rw - 1, ay + (ah - rh) // 2)],
                [(ax, ay - rh - 1), (ax + aw - rw, ay - rh - 1), (ax + (aw - rw) // 2, ay - rh - 1)],
            ]
            sides = sides[rot:] + sides[:rot]
            spot = next(((x, y, rw, rh) for side in sides for x, y in side if clear((x, y, rw, rh))), None)
            if spot:
                break
        if spot is None:
            right = max(x + w for x, _, w, _ in rooms.values())
            spot = (right + 1, 0, rw, rh)
        rooms[m] = spot

    min_x = min(x for x, _, _, _ in rooms.values()) - 1
    min_y = min(y for _, y, _, _ in rooms.values()) - 1
    rooms = {m: (x - min_x, y - min_y, w, h) for m, (x, y, w, h) in rooms.items()}
    cells: Dict[Cell, Tuple[Optional[str], str]] = {}
    for m, (x, y, w, h) in rooms.items():
        for cx in range(x, x + w):
            for cy in range(y, y + h):
                cells[(cx, cy)] = (m, FLOOR)
    for (cx, cy) in list(cells):
        for dx, dy in _DIRS8:
            n = (cx + dx, cy + dy)
            if n not in cells:
                cells[n] = (None, WALL)
    inner: List[Doorway] = []
    for i, a in enumerate(sorted(members)):
        for b in sorted(members)[i + 1:]:
            pair = frozenset((a, b))
            if pair not in links:
                continue
            spots = sorted(
                c for c, (o, k) in cells.items() if k == WALL
                for dx, dy in _DIRS4
                if cells.get((c[0] - dx, c[1] - dy), (None, ""))[0] == a
                and cells.get((c[0] + dx, c[1] + dy), (None, ""))[0] == b
            )
            if spots:
                c = spots[len(spots) // 2]
                owner = door_side.get(pair) or a
                cells[c] = (owner, DOOR)
                inner.append(Doorway(c, owner, b if owner == a else a))
    w = max(c[0] for c in cells) + 1
    h = max(c[1] for c in cells) + 1
    return _Building(rooms=rooms, cells=cells, inner=inner, w=w, h=h)


def _canvas_for_buildings(names: List[str], plans: List[_Building]) -> _Canvas:
    width = sum(p.w + 2 for p in plans) + 4
    height = max([p.h for p in plans] + [4]) + 4
    return _Canvas(width, height, names)


def _stamp_building(canvas: _Canvas, plan: _Building, at: Cell) -> None:
    ox, oy = at
    for (x, y), (room, kind) in plan.cells.items():
        canvas.set((ox + x, oy + y), canvas.index[room] if room else -1, kind)
    for d in plan.inner:
        canvas.doorways.append(Doorway((ox + d.cell[0], oy + d.cell[1]), d.owner, d.other))


def _ring(plan: _Building) -> Set[Cell]:
    return {(x + dx, y + dy) for (x, y) in plan.cells for dx, dy in _DIRS8
            if (x + dx, y + dy) not in plan.cells}


def _site_building(canvas: _Canvas, plan: _Building, parent: str, back: List[str],
                   needs: Optional[Dict[str, List[str]]] = None) -> Optional[Cell]:
    """房子放在哪：落在所属的室外地里（要开后门的话可以压到那块相连的地上）、四周留一圈空地、
    放下后那几块地仍然各自连成一片；挨得着要开后门的地优先；离所属地中心不远不近。
    放不下就贴着所属的地放在荒野里。"""
    ring = sorted(_ring(plan))
    body = sorted(plan.cells)
    # 能开门的地方：外墙格子（里侧是哪间屋子的地板）外面那一格
    steps_of: Dict[str, List[Cell]] = {}
    for (x, y), (_, k) in plan.cells.items():
        if k != WALL:
            continue
        for dx, dy in _DIRS4:
            room, kind = plan.cells.get((x - dx, y - dy), (None, ""))
            if kind == FLOOR and room and (x + dx, y + dy) not in plan.cells:
                steps_of.setdefault(room, []).append((x + dx, y + dy))
    every_step = sorted({c for cs in steps_of.values() for c in cs})
    needs = needs or {}

    def steps_for(region: str) -> List[Cell]:
        rooms = needs.get(region)
        if not rooms:
            return every_step
        return sorted({c for r in rooms for c in steps_of.get(r, [])})

    if parent:
        parent_steps = steps_for(parent)
        back_steps = {canvas.index[o]: steps_for(o) for o in back}
        ip = canvas.index[parent]
        allowed = {ip} | {canvas.index[o] for o in back}
        pcells = canvas.cells(ip)
        if pcells:
            pcx = sum(c[0] for c in pcells) / len(pcells)
            pcy = sum(c[1] for c in pcells) / len(pcells)
            r = math.sqrt(len(pcells) / math.pi)
            cands = []
            for oy in range(0, canvas.h - plan.h + 1):
                for ox in range(0, canvas.w - plan.w + 1):
                    if canvas.own((ox + plan.w // 2, oy + plan.h // 2)) not in allowed:
                        continue
                    off_parent = 0
                    good = True
                    for x, y in body:
                        c = (ox + x, oy + y)
                        o = canvas.own(c)
                        if o not in allowed or canvas.kd(c) != GROUND:
                            good = False
                            break
                        off_parent += o != ip
                    if not good:
                        continue
                    for x, y in ring:
                        c = (ox + x, oy + y)
                        if canvas.own(c) not in allowed or canvas.kd(c) != GROUND:
                            good = False
                            break
                    if not good:
                        continue
                    if not any(canvas.own((ox + x, oy + y)) == ip for x, y in parent_steps):
                        continue
                    touch = {io for io, st in back_steps.items()
                             if any(canvas.own((ox + x, oy + y)) == io for x, y in st)}
                    d = math.hypot(ox + plan.w / 2 - pcx, oy + plan.h / 2 - pcy)
                    cands.append((-len(touch), off_parent, round(abs(d - 0.4 * r), 1), oy, ox))
            for *_, oy, ox in sorted(cands):
                taken = {(ox + x, oy + y) for x, y in body}
                if all(len(_components(canvas.cells(i) - taken)) <= 1 for i in allowed):
                    return ox, oy
        # 实在放不下：贴着那块地，放在荒野里
        best = None
        for oy in range(1, canvas.h - plan.h):
            for ox in range(1, canvas.w - plan.w):
                if any(canvas.kd((ox + x, oy + y)) != VOID for x, y in body):
                    continue
                if any(canvas.kd((ox + x, oy + y)) not in (VOID, GROUND) for x, y in ring):
                    continue
                touch = sum(1 for x, y in parent_steps
                            if canvas.own((ox + x, oy + y)) == ip and canvas.kd((ox + x, oy + y)) == GROUND)
                if touch and (best is None or -touch < best[0]):
                    best = (-touch, ox, oy)
        return (best[1], best[2]) if best else None
    # 没有所属的室外地：找块空荒野
    for oy in range(1, canvas.h - plan.h):
        for ox in range(1, canvas.w - plan.w):
            if all(canvas.kd((ox + x, oy + y)) == VOID for x, y in body + ring):
                return ox, oy
    return None


def _open_entrances(canvas: _Canvas, plan: _Building, at: Cell, members: List[str], outdoor: List[str],
                    links: Dict[Pair, str], door_side: Dict[Pair, str], parent: str = "") -> List[str]:
    """外墙上开门：每间和某块室外地相连的屋子，朝那块地开一扇（尽量朝着那块地的中心）。

    门外那一格只能挨着那块地本身（之后别处围篱笆也碰不到门口）；后门门口还可以挨着房子所在的地
    （那条交界的篱笆会围在房子那一侧）。返回没开成门的室外地。
    """
    ox, oy = at
    ip = canvas.index[parent] if parent else -2
    missing = []
    for m in sorted(members):
        im = canvas.index[m]
        for o in outdoor:
            pair = frozenset((m, o))
            if pair not in links:
                continue
            io = canvas.index[o]
            ocells = canvas.cells(io)
            if not ocells:
                missing.append(o)
                continue
            cx = sum(c[0] for c in ocells) / len(ocells)
            cy = sum(c[1] for c in ocells) / len(ocells)
            cands = []
            for (x, y), (room, kind) in plan.cells.items():
                if kind != WALL:
                    continue
                c = (ox + x, oy + y)
                for dx, dy in _DIRS4:
                    inner, outer = (c[0] - dx, c[1] - dy), (c[0] + dx, c[1] + dy)
                    if not (canvas.own(inner) == im and canvas.kd(inner) == FLOOR
                            and canvas.own(outer) == io and canvas.kd(outer) == GROUND):
                        continue
                    ok_next = (io, -1) if io == ip else (io, -1, ip)
                    if any(canvas.own((outer[0] + ex, outer[1] + ey)) not in ok_next for ex, ey in _DIRS8):
                        continue
                    clear = canvas.own((outer[0] + dx, outer[1] + dy)) == io
                    cands.append((not clear, math.hypot(c[0] - cx, c[1] - cy), c))
            if not cands:
                missing.append(o)
                continue
            _, _, c = min(cands)
            owner = door_side.get(pair) or m
            canvas.set(c, canvas.index[owner], DOOR)
            canvas.doorways.append(Doorway(c, owner, o if owner == m else m))
    return missing


def _cheapest_passage(canvas: _Canvas, start: Set[Cell], goal_of: Set[Cell], ia: int, ib: int,
                      strict: bool) -> Tuple[Optional[Cell], Dict[Cell, Optional[Cell]]]:
    """从 start 那块地到 goal_of 那块地，穿过荒野 / 篱笆 / 墙的最便宜的一条路（Dijkstra）。

    strict：路上每一格周围都不能有第三个地点；否则只是不能踩进别的地点（贴着走代价高些）。
    """
    cost = {VOID: 1.0, FENCE: 3.0, WALL: 6.0}

    def step_cost(c: Cell) -> Optional[float]:
        if not (1 <= c[0] < canvas.w - 1 and 1 <= c[1] < canvas.h - 1):
            return None
        k = canvas.kd(c)
        if k not in cost or (not strict and k == WALL):
            return None
        near_other = any(canvas.own((c[0] + dx, c[1] + dy)) not in (-1, ia, ib) for dx, dy in _DIRS8)
        if near_other and strict:
            return None
        return cost[k] + (4.0 if near_other else 0.0)

    dist: Dict[Cell, float] = {}
    prev: Dict[Cell, Optional[Cell]] = {}
    heap: List[Tuple[float, Cell]] = []
    for c in sorted(start):
        for dx, dy in _DIRS4:
            n = (c[0] + dx, c[1] + dy)
            sc = step_cost(n)
            if sc is not None and sc < dist.get(n, math.inf):
                dist[n] = sc
                prev[n] = None
                heapq.heappush(heap, (sc, n))
    while heap:
        d, cur = heapq.heappop(heap)
        if d > dist.get(cur, math.inf):
            continue
        if any((cur[0] + dx, cur[1] + dy) in goal_of for dx, dy in _DIRS4):
            return cur, prev
        for dx, dy in _DIRS4:
            n = (cur[0] + dx, cur[1] + dy)
            sc = step_cost(n)
            if sc is not None and d + sc < dist.get(n, math.inf):
                dist[n] = d + sc
                prev[n] = cur
                heapq.heappush(heap, (d + sc, n))
    return None, prev


def _repair_links(canvas: _Canvas, links: Dict[Pair, str], door_side: Dict[Pair, str], indoor: Set[str],
                  prefer: Iterable[str] = ()) -> None:
    """最后一道保险：图上相邻、地图上却走不通的一对，从荒野 / 篱笆 / 墙里打通一条最短的路。

    路不贴着第三个地点（不会顺带把别处也连通）；穿墙、穿篱笆的那一格是门洞。
    """
    present = links_present(canvas.terrain(indoor, {}))
    for pair in sorted(links, key=lambda p: tuple(sorted(p))):
        if pair in present or len(pair) != 2:
            continue
        a, b = sorted(pair)
        ia, ib = canvas.index[a], canvas.index[b]
        mine = {ia, ib}

        both_out = a not in indoor and b not in indoor
        start = canvas.cells(ia, WALKABLE)
        goal_of = canvas.cells(ib, WALKABLE)
        if not start or not goal_of:
            continue
        end, prev, strict = None, {}, True
        for strict in ((True, False) if both_out else (True,)):
            end, prev = _cheapest_passage(canvas, start, goal_of, ia, ib, strict)
            if end is not None:
                break
        if end is None:
            continue
        path = [end]
        while prev[path[-1]] is not None:
            path.append(prev[path[-1]])  # type: ignore[arg-type]
        path.reverse()
        a_in, b_in = a in indoor, b in indoor
        door_owner = door_side.get(pair) or (a if a_in or not b_in else b)
        need_door = links[pair] == "door" or a_in or b_in
        door_at = next((c for c in path if canvas.kd(c) in (WALL, FENCE)), None)
        if need_door and door_at is None:
            door_at = path[len(path) // 2]
        half = (len(path) + 1) // 2
        if a in set(prefer) or b in set(prefer):
            half = 0 if b in set(prefer) else len(path)
        for k, c in enumerate(path):
            if c == door_at:
                canvas.set(c, canvas.index[door_owner], DOOR)
                continue
            if a_in != b_in and not (set(prefer) & {a, b}):
                owner = b if a_in else a   # 屋外那段路算室外地点的
            else:
                owner = a if k < half else b
            canvas.set(c, canvas.index[owner], FLOOR if owner in indoor else GROUND)
        if door_at is not None:
            canvas.doorways.append(Doorway(door_at, door_owner, b if door_owner == a else a))
        if not strict:
            # 路贴着别的地点走：把那边挨着路的地围上篱笆
            for c in path:
                for dx, dy in _DIRS8:
                    n = (c[0] + dx, c[1] + dy)
                    if canvas.own(n) not in (-1, ia, ib) and canvas.kd(n) == GROUND:
                        canvas.set(n, -1, FENCE)
        present.add(pair)


# =====================================================================
# 往已有地图上加新地点（任务线新开的区域）：已有的格子一格不动
# =====================================================================


def extend(
    terrain: Terrain,
    places: List[Place],
    links: Dict[Pair, str],
    door_side: Optional[Dict[Pair, str]] = None,
) -> Tuple[Terrain, Cell]:
    """新地点长在荒野里、贴着和它相连的已有地点。返回新地图和整体平移量（旧坐标 + 平移 = 新坐标）。"""
    door_side = dict(door_side or {})
    new = [p for p in places if p.name not in terrain.names]
    if not new:
        return terrain, (0, 0)
    by_name = {p.name: p for p in places}
    pad = 8 + max(int(math.sqrt(_area_of(p))) * 2 for p in new)
    names = list(terrain.names) + sorted(p.name for p in new)
    canvas = _Canvas(terrain.width + 2 * pad, terrain.height + 2 * pad, names)
    for y in range(terrain.height):
        for x in range(terrain.width):
            canvas.set((x + pad, y + pad), terrain.owner[y][x], terrain.kind[y][x])
    canvas.doorways = [Doorway((d.cell[0] + pad, d.cell[1] + pad), d.owner, d.other) for d in terrain.doorways]
    indoor = set(terrain.indoor)
    parents = dict(terrain.parents)
    outdoor_all = [n for n in names if n not in indoor and not (n in by_name and by_name[n].indoor)]
    salt_n = stable_hash(",".join(names))
    noise = _noise(canvas.w, canvas.h, salt_n)

    for p in sorted(new, key=lambda p: p.name):
        nbrs = sorted(n for n in names if n != p.name and frozenset((p.name, n)) in links and canvas.cells(canvas.index[n], WALKABLE))
        anchor = nbrs[0] if nbrs else (terrain.names[0] if terrain.names else None)
        if p.indoor:
            indoor.add(p.name)
            plan = _plan_building([p.name], by_name, links, door_side)
            parent = p.parent if p.parent in outdoor_all else (next((n for n in nbrs if n in outdoor_all), ""))
            spot = _site_building(canvas, plan, parent, []) if parent else None
            if spot is None:
                spot = _site_building(canvas, plan, "", [])
            if spot is None:
                continue
            if parent:
                parents[p.name] = parent
            _stamp_building(canvas, plan, spot)
            _open_entrances(canvas, plan, spot, [p.name], outdoor_all, links, door_side)
            continue
        # 室外：在相连地点的边上、离世界中心最远的那格荒野里落种子，只往荒野里长
        if anchor is None:
            seed = (canvas.w // 2, canvas.h // 2)
        else:
            acells = canvas.cells(canvas.index[anchor], WALKABLE)
            wx, wy = canvas.w / 2, canvas.h / 2
            edge = [(c[0] + dx, c[1] + dy) for c in acells for dx, dy in _DIRS4
                    if canvas.inside((c[0] + dx, c[1] + dy)) and canvas.kd((c[0] + dx, c[1] + dy)) == VOID]
            seed = max(edge, key=lambda c: (math.hypot(c[0] - wx, c[1] - wy), c)) if edge else (canvas.w // 2, canvas.h // 2)
        i = canvas.index[p.name]
        area = _area_of(p)
        _grow(canvas, {i: seed}, {i: area}, {i: math.sqrt(area / math.pi)}, noise, salt_n)
        _smooth(canvas, [i])
        _keep_largest_parts(canvas, [i])
    outdoor_now = [n for n in names if n not in indoor and canvas.cells(canvas.index[n])]
    newset = {p.name for p in new}
    _connect_linked(canvas, outdoor_now, {k: v for k, v in links.items() if k & newset}, prefer=newset)
    area = {n: len(canvas.cells(canvas.index[n])) for n in outdoor_now}
    # 只在涉及新地点的交界上围篱笆（已有的地不动）
    for n in sorted(newset & set(outdoor_now)):
        for m in outdoor_now:
            if m == n:
                continue
            pair = frozenset((n, m))
            if links.get(pair) == "open" or not _touching(canvas, canvas.index[n], canvas.index[m]):
                continue
            side_links = {pair: links[pair]} if pair in links else {}
            side = {pair: n}
            _fence_borders(canvas, sorted((n, m)), side_links, side, {n: 0, m: 1})
    _repair_links(canvas, {k: v for k, v in links.items() if k & newset and k <= set(names)}, door_side, indoor,
                  prefer=newset)
    return _crop(canvas, indoor, parents, pad)


def _crop(canvas: _Canvas, indoor: Set[str], parents: Dict[str, str], pad: int) -> Tuple[Terrain, Cell]:
    used = [(x, y) for y in range(canvas.h) for x in range(canvas.w) if canvas.kind[y][x] != VOID]
    if not used:
        return canvas.terrain(indoor, parents), (pad, pad)
    min_x = max(0, min(c[0] for c in used) - 2)
    min_y = max(0, min(c[1] for c in used) - 2)
    max_x = min(canvas.w - 1, max(c[0] for c in used) + 2)
    max_y = min(canvas.h - 1, max(c[1] for c in used) + 2)
    if pad:
        # 已有的部分不往左上缩（平移量不为负，旧坐标只会整体往右下挪）
        min_x, min_y = min(min_x, pad), min(min_y, pad)
    owner = [row[min_x:max_x + 1] for row in canvas.owner[min_y:max_y + 1]]
    kind = [row[min_x:max_x + 1] for row in canvas.kind[min_y:max_y + 1]]
    doorways = [Doorway((d.cell[0] - min_x, d.cell[1] - min_y), d.owner, d.other) for d in canvas.doorways]
    t = Terrain(max_x - min_x + 1, max_y - min_y + 1, list(canvas.names), owner, kind,
                indoor=set(indoor), parents=dict(parents), doorways=doorways)
    return t, (pad - min_x, pad - min_y)


# =====================================================================
# 手画地图：world JSON 里的 "terrain": {"rows": [...], "legend": {"V": "Village", ...}}
#   固定字符：'#' 墙  '%' 篱笆  '~' 水  '+' 门洞  ' ' 荒野
#   门洞算在旁边的室内地点里（两边都是室外就算第一个），通向对面那块。
# =====================================================================


def from_ascii(rows: List[str], legend: Dict[str, str], indoor: Iterable[str] = (),
               parents: Optional[Dict[str, str]] = None) -> Terrain:
    indoor = set(indoor)
    names = sorted(set(legend.values()))
    index = {n: i for i, n in enumerate(names)}
    height = len(rows)
    width = max((len(r) for r in rows), default=0)
    canvas = _Canvas(width, height, names)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row.ljust(width)):
            if ch in legend:
                n = legend[ch]
                canvas.set((x, y), index[n], FLOOR if n in indoor else GROUND)
            elif ch in (WALL, FENCE, WATER):
                canvas.set((x, y), -1, ch)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != DOOR:
                continue
            sides = []
            for dx, dy in ((1, 0), (0, 1)):
                a, b = canvas.own((x - dx, y - dy)), canvas.own((x + dx, y + dy))
                if a >= 0 and b >= 0 and a != b:
                    sides = [a, b]
                    break
            if not sides:
                sides = sorted({canvas.own((x + dx, y + dy)) for dx, dy in _DIRS4} - {-1})
            if not sides:
                continue
            sides.sort(key=lambda i: (names[i] not in indoor, names[i]))
            owner = sides[0]
            canvas.set((x, y), owner, DOOR)
            if len(sides) > 1:
                canvas.doorways.append(Doorway((x, y), names[owner], names[sides[1]]))
    return canvas.terrain(indoor, dict(parents or {}))


def links_present(terrain: Terrain, blocked: Iterable[Cell] = ()) -> Set[Pair]:
    """地图上实际走得通的地点对：直接挨着的地，或门洞两边。

    blocked：额外走不了的格子（比如锁着的门所在的门洞）；不给就当所有的门都开着。
    """
    blocked = set(blocked)

    def open_at(c: Cell) -> bool:
        return terrain.kind_at(c) in WALKABLE and c not in blocked

    out: Set[Pair] = set()
    for y in range(terrain.height):
        for x in range(terrain.width):
            a = terrain.owner[y][x]
            if a < 0 or not open_at((x, y)):
                continue
            for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
                c = (x + dx, y + dy)
                if not terrain.inside(c):
                    continue
                b = terrain.owner[c[1]][c[0]]
                if b < 0 or b == a or not open_at(c):
                    continue
                if dx and dy and (not open_at((x + dx, y)) or not open_at((x, y + dy))):
                    continue
                out.add(frozenset((terrain.names[a], terrain.names[b])))
    return out


# =====================================================================
# 改地形：改几格（砌墙、开门洞、铺地）、在两个地点之间打通 / 隔断
# =====================================================================


def _canvas_from(terrain: Terrain) -> _Canvas:
    canvas = _Canvas(terrain.width, terrain.height, list(terrain.names))
    canvas.owner = [list(row) for row in terrain.owner]
    canvas.kind = [list(row) for row in terrain.kind]
    canvas.doorways = list(terrain.doorways)
    return canvas


def _doorway_at(canvas: _Canvas, c: Cell) -> Optional[Doorway]:
    """门洞那一格两侧（对着的两格）是哪两个地点。"""
    me = canvas.own(c)
    if me < 0:
        return None
    for dx, dy in _DIRS4:
        a, b = canvas.own((c[0] - dx, c[1] - dy)), canvas.own((c[0] + dx, c[1] + dy))
        other = b if a == me else a if b == me else -1
        if other >= 0 and other != me and canvas.kd((c[0] - dx, c[1] - dy)) in WALKABLE                 and canvas.kd((c[0] + dx, c[1] + dy)) in WALKABLE:
            return Doorway(c, canvas.names[me], canvas.names[other])
    return None


def edit_cells(terrain: Terrain, changes: Dict[Cell, Tuple[str, Optional[str]]]) -> Terrain:
    """改几格：{格子: (种类, 属于哪个地点 / None)}。返回新的地形（原来的不动）。

    障碍物（墙、篱笆、水）不属于任何地点；门洞要夹在两个地点之间才算门洞。
    """
    canvas = _canvas_from(terrain)
    for c, (kind, owner) in changes.items():
        if not canvas.inside(c):
            continue
        if kind in OBSTACLES or kind == VOID or owner is None:
            canvas.set(c, -1, kind if kind in OBSTACLES else VOID)
        else:
            if owner not in canvas.index:
                raise ValueError(f"unknown place {owner}")
            canvas.set(c, canvas.index[owner], kind)
    touched = set(changes)
    # 门洞列表：改到的格子和它们旁边的门洞重新认一遍
    near = {(c[0] + dx, c[1] + dy) for c in touched for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    canvas.doorways = [d for d in canvas.doorways if d.cell not in near]
    for c in sorted(near):
        if canvas.inside(c) and canvas.kd(c) == DOOR:
            d = _doorway_at(canvas, c)
            if d:
                canvas.doorways.append(d)
    return canvas.terrain(terrain.indoor, terrain.parents)


def connect(terrain: Terrain, a: str, b: str, *, door: bool = False) -> Terrain:
    """让 a、b 之间走得通：已经通了就不动，否则穿过荒野 / 篱笆 / 墙打通一条最短的路（穿墙处是门洞）。"""
    if frozenset((a, b)) in links_present(terrain):
        return terrain
    canvas = _canvas_from(terrain)
    _repair_links(canvas, {frozenset((a, b)): "door" if door else "open"}, {}, set(terrain.indoor))
    return canvas.terrain(terrain.indoor, terrain.parents)


def separate(terrain: Terrain, a: str, b: str) -> Terrain:
    """隔断 a、b：它们之间的门洞砌上，敞开的交界围上（室内用墙，室外用篱笆）。"""
    canvas = _canvas_from(terrain)
    ia, ib = canvas.index[a], canvas.index[b]
    barrier = WALL if a in terrain.indoor or b in terrain.indoor else FENCE
    for d in [d for d in canvas.doorways if d.pair() == frozenset((a, b))]:
        canvas.set(d.cell, -1, barrier)
    canvas.doorways = [d for d in canvas.doorways if d.pair() != frozenset((a, b))]
    # 小的一边挨着另一边的格子都变成隔断
    na, nb = len(canvas.cells(ia, WALKABLE)), len(canvas.cells(ib, WALKABLE))
    side, other = (ia, ib) if (na, a) <= (nb, b) else (ib, ia)
    line = [c for c in canvas.cells(side, WALKABLE)
            if any(canvas.own((c[0] + dx, c[1] + dy)) == other
                   and canvas.kd((c[0] + dx, c[1] + dy)) in WALKABLE for dx, dy in _DIRS8)]
    for c in line:
        canvas.set(c, -1, barrier)
    canvas.doorways = [d for d in canvas.doorways if canvas.kd(d.cell) == DOOR]
    return canvas.terrain(terrain.indoor, terrain.parents)


def ascii_preview(terrain: Terrain) -> str:
    """调试用：每个地点一个字母，障碍物原样。"""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    lines = []
    for y in range(terrain.height):
        line = []
        for x in range(terrain.width):
            k = terrain.kind[y][x]
            o = terrain.owner[y][x]
            if k in WALKABLE and o >= 0:
                ch = letters[o % len(letters)]
                line.append(DOOR if k == DOOR else (ch.lower() if k == FLOOR else ch))
            else:
                line.append(k)
        lines.append("".join(line).rstrip())
    legend = "  ".join(f"{letters[i % len(letters)]}={n}" for i, n in enumerate(terrain.names))
    return "\n".join(lines) + "\n" + legend
