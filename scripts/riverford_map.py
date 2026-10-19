"""
Riverford：手工布局的城镇地图 → worlds/riverford.json（"terrain" 段）。

布局（x 向右、y 向下，72 × 48 格）：
  · 一条河从北向南流过东边（x≈49，宽 3 格，有弯）；河上一座桥（y 19..21）通往东岸草地。
  · 西岸是有木栅栏围着的城镇：中间石板广场，集市街从广场往东通到码头；
    酒馆、公会大厅（后面是锁着的地下墓穴）、铁匠铺、教堂、民居都是有墙的房子，门朝街开。
  · 城墙西门通森林里的土路，南门通城外的麦田（田里有粮仓）。
  · 东岸草地往北是采石场；森林在西边和北边包着城镇。

跑一下就会重写 worlds/riverford.json 里的地形（人、物品、任务照抄 adventurer_town 并补上新地点）。
"""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "actions"))

from terrain_gen import from_ascii, links_present  # noqa: E402

W, H = 72, 48
grid = [[" "] * W for _ in range(H)]
decor = [[" "] * W for _ in range(H)]
rnd = random.Random(7)


def put(x, y, ch, d=None):
    if 0 <= x < W and 0 <= y < H:
        grid[y][x] = ch
        if d is not None:
            decor[y][x] = d


def rect(x0, y0, x1, y1, ch, d=None):
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            put(x, y, ch, d)


def river_x(y):
    return 49 + round(2 * math.sin(y / 7.0))


# ---- 河两岸的大块地 ----
for y in range(H):
    rx = river_x(y)
    for x in range(W):
        if x < rx - 1:
            put(x, y, "F")                      # 西岸先都是森林，后面再铺城镇和田
        elif x <= rx + 1:
            put(x, y, "~")                      # 河
        else:
            quarry = y <= 11 + round(1.5 * math.sin(x / 3.0)) and x >= 55 + round(math.sin(y / 2.0))
            put(x, y, "Q" if quarry else "E", "g" if quarry else None)

# 城外南边的麦田（西边留给森林）
for y in range(35, H):
    for x in range(13 + (y % 2), river_x(y) - 1):
        put(x, y, "A", "v" if x >= 33 and y >= 38 else "w")   # 东南角一块菜地，其余是麦田

# ---- 城镇（木栅栏围起来；东边靠河，码头沿河）----
TOWN_X0, TOWN_Y0, TOWN_Y1 = 14, 6, 34
for y in range(TOWN_Y0, TOWN_Y1 + 1):
    for x in range(TOWN_X0, river_x(y) - 1):
        put(x, y, "S")
for x in range(TOWN_X0, river_x(TOWN_Y0) - 1):
    put(x, TOWN_Y0, "%")
for x in range(TOWN_X0, river_x(TOWN_Y1) - 1):
    put(x, TOWN_Y1, "%")
for y in range(TOWN_Y0, TOWN_Y1 + 1):
    put(TOWN_X0, y, "%")
# 城门：西门（通森林）、南门（通田）
for y in (20, 21):
    put(TOWN_X0, y, "+")
for x in (28, 29):
    put(x, TOWN_Y1, "+")

# 码头：沿河三格宽的木板地，还有一段伸进河里的栈桥
for y in range(TOWN_Y0 + 1, TOWN_Y1):
    rx = river_x(y)
    for x in range(rx - 4, rx - 1):
        put(x, y, "D", "p")
for y in (27, 28):
    for x in range(river_x(y) - 1, river_x(y) + 1):
        put(x, y, "D", "p")

# 桥：码头 → 东岸
for y in (19, 20, 21):
    for x in range(river_x(y) - 1, river_x(y) + 2):
        put(x, y, "D", "p")

# 集市街：广场东边一直通到码头，两边摆摊
for y in range(18, 23):
    for x in range(33, river_x(y) - 4):
        put(x, y, "M", "s" if y in (18, 22) and x % 3 == 0 else "c")

# 广场（石板）
rect(22, 16, 32, 24, "S", "c")


def building(x0, y0, x1, y1, room, doors):
    """一栋房子：墙在外框上，里面是房间地板；doors 是墙上门洞的位置。"""
    rect(x0, y0, x1, y1, "#")
    rect(x0 + 1, y0 + 1, x1 - 1, y1 - 1, room)
    for x, y in doors:
        put(x, y, "+")


building(22, 8, 30, 14, "t", [(26, 14)])                 # 酒馆，门朝广场
building(33, 7, 41, 16, "g", [(37, 16)])                 # 公会大厅
rect(34, 8, 40, 10, "k")                                 # 大厅后部隔出地下墓穴的入口间
for x in range(34, 41):
    put(x, 11, "#")
put(37, 11, "+")                                         # 墓穴的铁门（CryptGate 摆在这里）
building(16, 17, 21, 23, "m", [(21, 20)])                # 铁匠铺，门朝广场
building(22, 26, 30, 32, "h", [(26, 26)])                # 教堂
building(33, 25, 39, 31, "o", [(36, 25)])                # 民居
building(18, 38, 24, 43, "n", [(21, 38)])                # 城外粮仓，门朝北

# ---- 路 ----
for x in range(0, TOWN_X0):                               # 西门外的林间土路
    for y in (20, 21):
        put(x, y, "F", "=")
for y in range(TOWN_Y1 + 1, H):                          # 南门外穿过麦田的土路
    for x in (28, 29):
        put(x, y, "A", "=")
for x in range(river_x(20) + 2, 62):                      # 桥东边的路，再往北去采石场
    for y in (19, 20, 21):
        if grid[y][x] in "EQ":
            decor[y][x] = "="
for y in range(8, 20):
    for x in (60, 61):
        if grid[y][x] in "EQ":
            decor[y][x] = "=" if grid[y][x] == "E" else "g"

# ---- 树：森林里密，草地上稀；路上不长；森林里走不到的小块也长满树 ----
for y in range(H):
    for x in range(W):
        if grid[y][x] == "F" and decor[y][x] != "=" and rnd.random() < 0.33:
            put(x, y, "^")
        elif grid[y][x] == "E" and decor[y][x] != "=" and rnd.random() < 0.05:
            put(x, y, "^")
        elif grid[y][x] == "E" and decor[y][x] == " " and rnd.random() < 0.08:
            decor[y][x] = "f"
# 城墙外一格留出空地（不贴着墙长树）
for y in range(H):
    for x in range(W):
        if grid[y][x] == "^" and any(
            0 <= x + dx < W and 0 <= y + dy < H and grid[y + dy][x + dx] in "%+"
            for dx in (-1, 0, 1) for dy in (-1, 0, 1)
        ):
            put(x, y, "F")


def fill_unreachable(ch):
    """从路出发走不到的那块地上补满树（不留死角）。"""
    start = [(x, y) for y in range(H) for x in range(W) if grid[y][x] == ch and decor[y][x] == "="]
    seen, todo = set(start), list(start)
    while todo:
        x, y = todo.pop()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            n = (x + dx, y + dy)
            if 0 <= n[0] < W and 0 <= n[1] < H and n not in seen and grid[n[1]][n[0]] == ch:
                seen.add(n)
                todo.append(n)
    for y in range(H):
        for x in range(W):
            if grid[y][x] == ch and (x, y) not in seen:
                put(x, y, "^")


fill_unreachable("F")
fill_unreachable("E")

LEGEND = {
    "S": "TownSquare", "M": "Market", "D": "RiverDocks", "E": "Meadow", "Q": "Quarry",
    "F": "Forest", "A": "Fields", "t": "Tavern", "g": "GuildHall", "k": "Crypt",
    "m": "Smithy", "h": "Chapel", "o": "Cottages", "n": "Granary",
}
INDOOR = {"Tavern": "TownSquare", "GuildHall": "TownSquare", "Crypt": "TownSquare", "Smithy": "TownSquare",
          "Chapel": "TownSquare", "Cottages": "TownSquare", "Granary": "Fields"}
rows = ["".join(r) for r in grid]
decor_rows = ["".join(r) for r in decor]

terrain = from_ascii(rows, LEGEND, INDOOR.keys(), INDOOR, decor=decor_rows,
                     door_side={frozenset(("GuildHall", "Crypt")): "GuildHall"})
pairs = sorted(tuple(sorted(p)) for p in links_present(terrain))


def cell_of(name, x, y):
    """世界格子 → 这个地点外接矩形里的局部坐标（物品的 pos）。"""
    bx, by, _, _ = terrain.bbox(name)
    assert terrain.owner_at((x, y)) == name, (name, x, y, terrain.owner_at((x, y)))
    return [x - bx, y - by]


if __name__ == "__main__":
    base = json.loads((ROOT / "worlds" / "adventurer_town.json").read_text(encoding="utf-8"))
    descriptions = {
        "TownSquare": "A cobbled square inside the palisade, ringed by the tavern, the guild hall, the smithy and the chapel.",
        "Market": "Market Row runs from the square down to the docks, lined with stalls.",
        "RiverDocks": "Plank docks along the river, a fishing pier, and the bridge to the east bank.",
        "Meadow": "Open grassland across the bridge; the road climbs north to the quarry.",
        "Quarry": "A grey scar of broken stone in the northern hills.",
        "Forest": "Old pines wrap the town on the west and north; a dirt road leads out from the west gate.",
        "Fields": "Wheat and vegetable plots outside the south gate, with the granary.",
        "Tavern": "The Drowned Lantern: warm, loud, smelling of stew.",
        "GuildHall": "The adventurers' guild: maps, trophies, and an iron gate at the back.",
        "Crypt": "Below the guild hall: cold stone and something that should have stayed sealed.",
        "Smithy": "Forge, anvil and racks of half-finished blades.",
        "Chapel": "A small stone chapel, quiet except for the bell.",
        "Cottages": "A row of family homes behind the market.",
        "Granary": "Where the harvest is stored, dry and guarded from rats.",
    }
    tags = {
        "TownSquare": "hub,social,build", "Market": "trade,build", "RiverDocks": "work,water,social",
        "Meadow": "wild", "Quarry": "work,stone", "Forest": "work,wood,food,wild", "Fields": "work,food",
        "Tavern": "food,social,rest,meal", "GuildHall": "guild,social", "Crypt": "dungeon,danger",
        "Smithy": "work,craft", "Chapel": "rest,social", "Cottages": "home,rest", "Granary": "food,storage",
    }
    locations = []
    for name in sorted(set(LEGEND.values()), key=lambda n: (n in INDOOR, n)):
        loc = {"name": name, "description": descriptions[name], "tags": tags[name]}
        if name in INDOOR:
            loc.update(indoor=True, parent=INDOOR[name])
        locations.append(loc)
    items = [dict(i) for i in base["items"]]
    spots = {
        "FishingPier": ("RiverDocks", river_x(27) - 1, 27),
        "Stalls": ("Market", 38, 20),
        "Forge": ("Smithy", 18, 19),
        "Hearth": ("Tavern", 24, 10),
        "QuarryFace": ("Quarry", 63, 5),
    }
    for it in items:
        if it["name"] in spots:
            loc, x, y = spots[it["name"]]
            it["located_at"], it["pos"] = loc, cell_of(loc, x, y)
        else:
            it.pop("pos", None)
    persons = [dict(p) for p in base["persons"]]
    persons.append({
        "name": "Hale", "located_at": "TownSquare", "role": "npc", "brain": "routine", "guard": True,
        "personality": "steady, fair, slow to anger", "background": "The town watch. Walks the palisade and the square.",
        "goal": "Keep the peace in Riverford", "values": "order,duty", "mood": "alert", "speech_style": "brief",
        "hp": 130, "power_level": 16, "coins": 15,
        "routine": [
            {"start": 6, "end": 14, "location": "TownSquare", "activity": "watch the square"},
            {"start": 14, "end": 18, "location": "Market", "activity": "walk the market"},
            {"start": 18, "end": 22, "location": "RiverDocks", "activity": "check the docks and the bridge"},
            {"start": 22, "end": 6, "location": "Cottages", "activity": "sleep", "action": "sleep"},
        ],
    })
    persons.append({
        "name": "GreyWolf", "located_at": "Meadow", "role": "npc", "brain": "routine", "faction": "beast",
        "personality": "hungry, wary", "background": "A lone wolf that hunts the meadow across the bridge.",
        "goal": "Eat", "values": "survival", "mood": "hungry", "speech_style": "growls",
        "hp": 60, "power_level": 9, "coins": 0,
        "routine": [{"start": 0, "end": 24, "location": "Meadow", "activity": "prowl the long grass"}],
    })
    world = {
        "id": "riverford",
        "title": "Riverford",
        "premise": (
            "A walled river town. The square, the market row and the docks are inside the palisade; "
            "a bridge crosses to the meadow and the quarry road, the west gate opens on the forest "
            "and the south gate on the fields. Beneath the guild hall lies a crypt the guildmaster "
            "sealed years ago. The town watch keeps the peace; a wolf hunts the meadow across the bridge."
        ),
        "locations": locations,
        "adjacencies": [{"a": a, "b": b} for a, b in pairs],
        "persons": persons,
        "items": items,
        "quests": base.get("quests", []),
        "facts": base.get("facts", []),
        "rumors": base.get("rumors", []),
        "social": base.get("social", []),
        "terrain": {
            "legend": LEGEND,
            "rows": rows,
            "decor": decor_rows,
        },
    }
    out = ROOT / "worlds" / "riverford.json"
    out.write_text(json.dumps(world, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out.name}: {W}x{H}, {len(locations)} places")
    for a, b in pairs:
        print(f"  {a} <-> {b}")
    print("\n".join(rows))
