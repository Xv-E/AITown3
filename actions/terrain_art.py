"""
地形美术：把地形（terrain_gen.Terrain + 装饰层）画成一张像素画地图。

每格 16×16 像素的贴图都是程序画出来的（不用外部素材）：草地、林地、土路、石板、木板、
水（靠岸有浪花）、石墙（下方投影）、木栅栏、门、城门、树、麦田、菜地、碎石、集市摊位、花、
木地板 / 石地板。同一种格子有几种变体，按格子位置稳定地挑，所以不会一眼看出重复。

    from terrain_art import render
    image = render(terrain, labels=True)   # PIL.Image
    image.save("map.png")
"""

from __future__ import annotations

import random
from functools import lru_cache
from typing import Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from terrain_gen import DOOR, FENCE, FLOOR, GROUND, TREE, VOID, WALL, WATER, Terrain

T = 16  # 每格像素
RGB = Tuple[int, int, int]


def _shade(c: RGB, k: float) -> RGB:
    return tuple(max(0, min(255, int(v * k))) for v in c)  # type: ignore[return-value]


def _noise_tile(base: RGB, spots: Tuple[Tuple[RGB, float], ...], seed: int) -> Image.Image:
    """底色上撒点：spots = ((颜色, 概率), ...)。"""
    rnd = random.Random(seed)
    img = Image.new("RGBA", (T, T), base + (255,))
    px = img.load()
    for y in range(T):
        for x in range(T):
            r = rnd.random()
            acc = 0.0
            for color, p in spots:
                acc += p
                if r < acc:
                    px[x, y] = color + (255,)
                    break
    return img


# ---------------------------------------------------------------------
# 地面
# ---------------------------------------------------------------------

GRASS = (88, 142, 64)
MEADOW = (112, 156, 70)
FOREST_FLOOR = (60, 100, 52)
DIRT = (150, 114, 74)
LANE = (168, 140, 100)
COBBLE = (140, 138, 132)
PLANK = (150, 106, 62)
WATER_C = (52, 104, 168)
FLOOR_WOOD = (168, 124, 80)
FLOOR_STONE = (128, 126, 122)
GRAVEL = (152, 148, 140)
SOIL = (112, 80, 52)


@lru_cache(maxsize=None)
def grass(seed: int, base: RGB = GRASS) -> Image.Image:
    img = _noise_tile(base, ((_shade(base, 0.82), 0.12), (_shade(base, 1.18), 0.08)), seed)
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed * 7 + 1)
    for _ in range(3):  # 几根草
        x, y = rnd.randrange(1, T - 1), rnd.randrange(3, T)
        d.line([(x, y), (x + rnd.choice((-1, 0, 1)), y - 2)], fill=_shade(base, 0.7) + (255,))
    return img


@lru_cache(maxsize=None)
def forest_floor(seed: int) -> Image.Image:
    img = _noise_tile(FOREST_FLOOR, ((_shade(FOREST_FLOOR, 0.78), 0.18), ((92, 78, 46), 0.05)), seed)
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed + 99)
    for _ in range(2):  # 松针
        x, y = rnd.randrange(2, T - 3), rnd.randrange(2, T - 2)
        d.line([(x, y), (x + 2, y + 1)], fill=(110, 86, 50, 255))
    return img


@lru_cache(maxsize=None)
def flowers(seed: int) -> Image.Image:
    img = grass(seed, MEADOW).copy()
    rnd = random.Random(seed + 5)
    px = img.load()
    for _ in range(4):
        x, y = rnd.randrange(2, T - 2), rnd.randrange(2, T - 2)
        c = rnd.choice(((236, 220, 90), (232, 120, 140), (240, 240, 240), (160, 130, 230)))
        for dx, dy in ((0, 0), (1, 0), (0, 1), (-1, 0), (0, -1)):
            px[x + dx, y + dy] = (c if (dx, dy) != (0, 0) else (250, 210, 60)) + (255,)
    return img


@lru_cache(maxsize=None)
def dirt(seed: int, base: RGB = DIRT) -> Image.Image:
    img = _noise_tile(base, ((_shade(base, 0.85), 0.14), (_shade(base, 1.15), 0.08)), seed)
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed + 3)
    for _ in range(2):  # 石子
        x, y = rnd.randrange(1, T - 2), rnd.randrange(1, T - 2)
        d.rectangle([x, y, x + 1, y], fill=_shade(base, 0.65) + (255,))
    return img


@lru_cache(maxsize=None)
def cobble(seed: int) -> Image.Image:
    img = Image.new("RGBA", (T, T), _shade(COBBLE, 0.68) + (255,))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for row, y in enumerate(range(0, T, 4)):
        off = 0 if row % 2 == 0 else 3
        for x in range(-off, T, 6):
            k = 0.92 + rnd.random() * 0.2
            d.rectangle([x + 1, y + 1, x + 5, y + 3], fill=_shade(COBBLE, k) + (255,))
            d.point((x + 2, y + 1), fill=_shade(COBBLE, k * 1.15) + (255,))
    return img


@lru_cache(maxsize=None)
def planks(seed: int, horizontal: bool = True, base: RGB = PLANK) -> Image.Image:
    img = Image.new("RGBA", (T, T), base + (255,))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for i in range(0, T, 4):
        k = 0.9 + rnd.random() * 0.2
        box = [0, i, T - 1, i + 3] if horizontal else [i, 0, i + 3, T - 1]
        d.rectangle(box, fill=_shade(base, k) + (255,))
        line = [(0, i + 3), (T - 1, i + 3)] if horizontal else [(i + 3, 0), (i + 3, T - 1)]
        d.line(line, fill=_shade(base, 0.62) + (255,))
        cut = rnd.randrange(3, T - 3)
        if horizontal:
            d.line([(cut, i), (cut, i + 2)], fill=_shade(base, 0.7) + (255,))
        else:
            d.line([(i, cut), (i + 2, cut)], fill=_shade(base, 0.7) + (255,))
    return img


@lru_cache(maxsize=None)
def stone_floor(seed: int, base: RGB = FLOOR_STONE) -> Image.Image:
    img = Image.new("RGBA", (T, T), _shade(base, 0.7) + (255,))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for y in (0, 8):
        for x in (0, 8):
            d.rectangle([x, y, x + 6, y + 6], fill=_shade(base, 0.9 + rnd.random() * 0.15) + (255,))
    return img


@lru_cache(maxsize=None)
def gravel(seed: int) -> Image.Image:
    img = _noise_tile(GRAVEL, ((_shade(GRAVEL, 0.75), 0.2), (_shade(GRAVEL, 1.15), 0.12)), seed)
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed + 11)
    for _ in range(2):
        x, y = rnd.randrange(1, T - 4), rnd.randrange(1, T - 4)
        d.ellipse([x, y, x + 3, y + 2], fill=(118, 114, 108, 255), outline=(90, 86, 82, 255))
    return img


@lru_cache(maxsize=None)
def wheat(seed: int) -> Image.Image:
    img = Image.new("RGBA", (T, T), SOIL + (255,))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for x in range(1, T, 3):
        for y in range(0, T, 2):
            gold = (214 + rnd.randrange(-14, 14), 184 + rnd.randrange(-14, 10), 86)
            d.line([(x, y), (x, y + 1)], fill=gold + (255,))
        d.point((x, rnd.randrange(T)), fill=(240, 214, 120, 255))
    return img


@lru_cache(maxsize=None)
def vegetables(seed: int) -> Image.Image:
    img = _noise_tile(SOIL, ((_shade(SOIL, 0.8), 0.2),), seed)
    d = ImageDraw.Draw(img)
    for y in (3, 8, 13):
        d.line([(0, y + 1), (T - 1, y + 1)], fill=_shade(SOIL, 0.7) + (255,))
        for x in range(2, T, 5):
            d.ellipse([x - 1, y - 1, x + 2, y + 1], fill=(84, 150, 64, 255))
            d.point((x, y - 1), fill=(130, 190, 90, 255))
    return img


@lru_cache(maxsize=None)
def stall(seed: int) -> Image.Image:
    img = cobble(seed).copy()
    d = ImageDraw.Draw(img)
    colors = ((196, 60, 52), (60, 110, 180), (70, 150, 80))
    c = colors[seed % len(colors)]
    for i, x in enumerate(range(1, T - 1, 3)):  # 条纹遮阳篷
        d.rectangle([x, 2, x + 2, 9], fill=(c if i % 2 == 0 else (236, 230, 216)) + (255,))
    d.line([(1, 10), (T - 2, 10)], fill=(70, 50, 32, 255))
    d.line([(2, 10), (2, 14)], fill=(90, 64, 40, 255))
    d.line([(T - 3, 10), (T - 3, 14)], fill=(90, 64, 40, 255))
    return img


@lru_cache(maxsize=None)
def water(seed: int) -> Image.Image:
    img = _noise_tile(WATER_C, ((_shade(WATER_C, 0.9), 0.15),), seed)
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed + 21)
    for _ in range(2):
        x, y = rnd.randrange(1, T - 5), rnd.randrange(2, T - 2)
        d.line([(x, y), (x + 1, y - 1), (x + 3, y - 1), (x + 4, y)], fill=(110, 160, 214, 255))
    return img


# ---------------------------------------------------------------------
# 障碍物与门
# ---------------------------------------------------------------------

WALL_C = (124, 118, 110)


@lru_cache(maxsize=None)
def wall(seed: int) -> Image.Image:
    img = Image.new("RGBA", (T, T), _shade(WALL_C, 0.62) + (255,))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for row, y in enumerate(range(0, T, 4)):
        off = 0 if row % 2 == 0 else 4
        for x in range(-off, T, 8):
            k = 0.9 + rnd.random() * 0.22
            d.rectangle([x + 1, y + 1, x + 7, y + 3], fill=_shade(WALL_C, k) + (255,))
            d.line([(x + 1, y + 1), (x + 6, y + 1)], fill=_shade(WALL_C, k * 1.18) + (255,))
    return img


@lru_cache(maxsize=None)
def palisade(seed: int, horizontal: bool) -> Image.Image:
    """木栅栏：一根根削尖的木桩。"""
    img = Image.new("RGBA", (T, T), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    wood = (132, 94, 56)
    for i in range(0, T, 4):
        k = 0.85 + (seed + i) % 3 * 0.1
        if horizontal:
            d.rectangle([i, 3, i + 3, T - 3], fill=_shade(wood, k) + (255,))
            d.polygon([(i, 3), (i + 3, 3), (i + 1, 0)], fill=_shade(wood, k * 1.15) + (255,))
            d.line([(i + 3, 3), (i + 3, T - 3)], fill=_shade(wood, 0.55) + (255,))
        else:
            d.rectangle([3, i, T - 3, i + 3], fill=_shade(wood, k) + (255,))
            d.line([(3, i + 3), (T - 3, i + 3)], fill=_shade(wood, 0.55) + (255,))
    d.line([(0, 7), (T - 1, 7)] if horizontal else [(7, 0), (7, T - 1)], fill=(84, 60, 36, 255))
    return img


@lru_cache(maxsize=None)
def door(horizontal_wall: bool) -> Image.Image:
    img = planks(3, horizontal=not horizontal_wall, base=(126, 82, 44)).copy()
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, T - 1, T - 1], outline=(70, 46, 26, 255))
    d.point((T - 4, T // 2) if horizontal_wall else (T // 2, T - 4), fill=(230, 200, 90, 255))
    return img


@lru_cache(maxsize=None)
def tree(seed: int) -> Image.Image:
    img = Image.new("RGBA", (T, T), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    d.ellipse([3, 10, 14, 15], fill=(20, 40, 20, 120))                # 影子
    d.rectangle([7, 9, 8, 13], fill=(90, 60, 36, 255))                # 树干
    dark, mid, light = (34, 82, 40), (52, 114, 52), (92, 156, 74)
    if rnd.random() < 0.5:   # 松树
        d.polygon([(8, 0), (14, 9), (2, 9)], fill=mid + (255,), outline=dark + (255,))
        d.polygon([(8, 0), (8, 9), (2, 9)], fill=dark + (255,))
        d.line([(8, 1), (11, 6)], fill=light + (255,))
    else:                    # 阔叶树
        d.ellipse([1, 0, 14, 11], fill=dark + (255,))
        d.ellipse([2, 0, 12, 9], fill=mid + (255,))
        d.ellipse([4, 1, 8, 4], fill=light + (255,))
    return img


# ---------------------------------------------------------------------
# 拼图
# ---------------------------------------------------------------------

STONE_FLOOR_PLACES = ("chapel", "crypt", "guild")


def _ground_tile(terrain: Terrain, x: int, y: int, deco: str, seed: int) -> Image.Image:
    owner = (terrain.owner_at((x, y)) or "").lower()
    if deco == "=":
        return dirt(seed % 4)
    if deco == "c":
        return cobble(seed % 4)
    if deco == "p":
        horizontal = terrain.kind_at((x, y - 1)) == WATER or terrain.kind_at((x, y + 1)) == WATER
        return planks(seed % 3, horizontal=not horizontal)
    if deco == "w":
        return wheat(seed % 3)
    if deco == "v":
        return vegetables(seed % 2)
    if deco == "g":
        return gravel(seed % 4)
    if deco == "s":
        return stall(seed % 3)
    if deco == "f":
        return flowers(seed % 4)
    if "forest" in owner or "wood" in owner:
        return forest_floor(seed % 4)
    if "quarry" in owner:
        return gravel(seed % 4)
    if "meadow" in owner or "field" in owner:
        return grass(seed % 4, MEADOW)
    if "square" in owner or "town" in owner or "street" in owner or "lane" in owner:
        return dirt(seed % 4, LANE)
    if "dock" in owner or "pier" in owner:
        return planks(seed % 3)
    return grass(seed % 4)


def _floor_tile(terrain: Terrain, x: int, y: int, seed: int) -> Image.Image:
    owner = (terrain.owner_at((x, y)) or "").lower()
    if any(k in owner for k in STONE_FLOOR_PLACES):
        return stone_floor(seed % 3, (96, 92, 98) if "crypt" in owner else FLOOR_STONE)
    return planks(seed % 3, horizontal=False, base=FLOOR_WOOD)


def _under_tile(terrain: Terrain, x: int, y: int, decor: list, seed: int) -> Image.Image:
    """障碍物（树）脚下画什么地面：看旁边的地。"""
    for dx, dy in ((0, 1), (1, 0), (-1, 0), (0, -1)):
        k = terrain.kind_at((x + dx, y + dy))
        if k == GROUND:
            d = decor[y + dy][x + dx] if decor else " "
            return _ground_tile(terrain, x + dx, y + dy, d if d in "fg" else " ", seed)
    return forest_floor(seed % 4)


def render(terrain: Terrain, *, labels: bool = True, scale: int = 2) -> Image.Image:
    decor = [row.ljust(terrain.width) for row in terrain.decor] if terrain.decor else []
    img = Image.new("RGBA", (terrain.width * T, terrain.height * T), (24, 34, 28, 255))
    trees = []
    for y in range(terrain.height):
        for x in range(terrain.width):
            kind = terrain.kind_at((x, y))
            seed = (x * 73856093) ^ (y * 19349663)
            deco = decor[y][x] if decor else " "
            at = (x * T, y * T)
            if kind == VOID:
                img.paste(forest_floor(seed % 4), at)
                continue
            if kind == GROUND:
                tile = _ground_tile(terrain, x, y, deco, seed)
            elif kind == FLOOR:
                tile = _floor_tile(terrain, x, y, seed)
            elif kind == WATER:
                tile = water(seed % 4)
            elif kind == WALL:
                tile = wall(seed % 3)
            elif kind == FENCE:
                horizontal = terrain.kind_at((x - 1, y)) in (FENCE, DOOR) or terrain.kind_at((x + 1, y)) in (FENCE, DOOR)
                img.paste(_under_tile(terrain, x, y, decor, seed), at)
                tile = palisade(seed % 3, horizontal)
                img.alpha_composite(tile, at)
                continue
            elif kind == TREE:
                img.paste(_under_tile(terrain, x, y, decor, seed), at)
                trees.append((at, seed))
                continue
            elif kind == DOOR:
                walls_lr = terrain.kind_at((x - 1, y)) in (WALL, FENCE) or terrain.kind_at((x + 1, y)) in (WALL, FENCE)
                in_fence = FENCE in (terrain.kind_at((x - 1, y)), terrain.kind_at((x + 1, y)),
                                     terrain.kind_at((x, y - 1)), terrain.kind_at((x, y + 1)))
                tile = dirt(seed % 4) if in_fence else door(walls_lr)   # 城门敞着：土路穿过栅栏
            else:
                tile = grass(seed % 4)
            img.paste(tile, at)
    # 水边的浪花、墙的投影
    d = ImageDraw.Draw(img)
    for y in range(terrain.height):
        for x in range(terrain.width):
            kind = terrain.kind_at((x, y))
            if kind == WATER:
                for dx, dy, box in ((0, -1, (0, 0, T - 1, 1)), (0, 1, (0, T - 2, T - 1, T - 1)),
                                    (-1, 0, (0, 0, 1, T - 1)), (1, 0, (T - 2, 0, T - 1, T - 1))):
                    if terrain.kind_at((x + dx, y + dy)) not in (WATER, VOID):
                        bx, by = x * T, y * T
                        d.rectangle([bx + box[0], by + box[1], bx + box[2], by + box[3]], fill=(176, 208, 236, 255))
            if kind == WALL and terrain.kind_at((x, y + 1)) in (GROUND, FLOOR, DOOR):
                shade = Image.new("RGBA", (T, 4), (0, 0, 0, 70))
                img.alpha_composite(shade, (x * T, (y + 1) * T))
    for at, seed in trees:
        img.alpha_composite(tree(seed % 6), at)
    if labels:
        _labels(img, terrain)
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    return img


def _font(size: int):
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _labels(img: Image.Image, terrain: Terrain) -> None:
    d = ImageDraw.Draw(img)
    for name in terrain.names:
        mid = terrain.anchor(name)
        if mid is None:
            continue
        font = _font(9 if name in terrain.indoor else 12)
        text = name
        w = d.textlength(text, font=font)
        cx, cy = mid[0] * T + T // 2, mid[1] * T + T // 2
        x, y = cx - w / 2, cy - 6
        for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
            d.text((x + ox, y + oy), text, font=font, fill=(20, 20, 20, 255))
        d.text((x, y), text, font=font, fill=(250, 244, 222, 255))


def render_world_json(path: str, out: str, *, scale: int = 2, labels: bool = True) -> Optional[str]:
    """world JSON 里有手画地形就画出来；没有就按地点和连接关系生成一张再画。"""
    import json

    from terrain_gen import Place, from_ascii, generate

    data = json.loads(open(path, encoding="utf-8").read())
    locs = data.get("locations") or []
    indoor = {l["name"]: l.get("parent", "") for l in locs if l.get("indoor")}
    raw = data.get("terrain")
    if raw and raw.get("rows"):
        terrain = from_ascii(raw["rows"], raw["legend"], indoor.keys(), indoor, decor=raw.get("decor"))
    else:
        places = [Place(l["name"], tuple(l.get("grid") or (20, 12)), bool(l.get("indoor")), l.get("parent", ""),
                        map=tuple(l["map"]) if l.get("map") else None) for l in locs]
        links = {frozenset((e["a"], e["b"])): "door" if e.get("locked") else "open" for e in data.get("adjacencies") or []}
        terrain = generate(places, links)
    render(terrain, labels=labels, scale=scale).save(out)
    return out
