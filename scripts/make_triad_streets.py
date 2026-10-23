"""生成 worlds/triad_streets.json：古惑仔争街的大地图 + 街头经济。用法：python scripts/make_triad_streets.py ."""

import json
import sys
from pathlib import Path

S = 3  # 地图格距

LOCATIONS = [
    ("TempleStreet", "The night market spine: hawker lamps, fortune tellers, crowds until 2 a.m. Every side wants it.", "hub,market,social", (0, 0), (24, 13)),
    ("FishMarket", "Wet market of tanks and ice. Big Ma's curry fish ball cart steams at the corner.", "market,food", (-1, 0), (20, 12)),
    ("Wharf", "Godowns and cranes. Ho's wholesale crates come off the boats here every day.", "work,trade", (-2, 0), (22, 12)),
    ("FerryPier", "The night ferry lands newcomers with cardboard suitcases and nowhere to sleep.", "arrival", (-2, 1), (16, 10)),
    ("Tenements", "Six storeys of subdivided flats, washing on bamboo poles. Dragon Leung owns the building.", "home", (-1, 1), (20, 12)),
    ("Rooftops", "A rooftop shrine and mahjong table above the tenements: the Silver Dragons' hall.", "hideout", (-1, 2), (16, 10)),
    ("NoodleAlley", "A narrow lane of plastic stools around Mrs Lau's noodle stall.", "food,social", (0, 1), (18, 10)),
    ("TeaHouseRow", "Uncle Wing's cha chaan teng and its neighbours: milk tea, newspapers, gossip.", "food,social", (1, 1), (20, 12)),
    ("NeonStrip", "Karaoke signs and the Lucky Bar. Money, beer and trouble after dark.", "nightlife,social", (1, 0), (22, 12)),
    ("MahjongHall", "Auntie Fong's mahjong parlour; the clack of tiles never stops.", "nightlife", (2, 0), (18, 10)),
    ("BackAlley", "Bins, steam vents and blind corners where arguments get settled.", "danger", (2, 1), (16, 10)),
    ("GarageYard", "A car-repair yard behind roll-down shutters: the Black Tigers' den.", "hideout", (2, 2), (20, 12)),
    ("TinHauTemple", "Incense coils and the sea goddess's altar. Nobody fights in front of her — usually.", "temple,neutral", (0, 2), (18, 10)),
    ("PoliceStation", "A tired district station with one sergeant who knows everyone.", "police", (1, 2), (18, 10)),
    ("HerbalLane", "Dried roots and bitter brews. Mr Chow patches up whoever limps in.", "medicine", (1, 3), (16, 10)),
    ("BuildingSite", "Scaffolding for a new tower nobody owns yet. Day labour, no questions.", "work", (0, 3), (20, 12)),
]
EDGES = [
    ("Wharf", "FishMarket"), ("Wharf", "FerryPier"), ("FerryPier", "Tenements"), ("FishMarket", "TempleStreet"),
    ("FishMarket", "Tenements"), ("TempleStreet", "NeonStrip"), ("TempleStreet", "NoodleAlley"),
    ("NeonStrip", "MahjongHall"), ("NeonStrip", "TeaHouseRow"), ("MahjongHall", "BackAlley"),
    ("Tenements", "NoodleAlley"), ("NoodleAlley", "TeaHouseRow"), ("TeaHouseRow", "BackAlley"),
    ("Tenements", "Rooftops"), ("NoodleAlley", "TinHauTemple"), ("TeaHouseRow", "PoliceStation"),
    ("BackAlley", "GarageYard"), ("TinHauTemple", "PoliceStation"), ("PoliceStation", "HerbalLane"),
    ("GarageYard", "HerbalLane"), ("TinHauTemple", "BuildingSite"), ("BuildingSite", "HerbalLane"),
]


# ---------------------------------------------------------------------------
# 手画街区地图（worlds/*.json 的 "terrain" 段；地点之间连不连通由地图决定）
#
#   x 向右、y 向下，88 × 47 格。西边是海港：码头（Wharf）和渡轮码头（FerryPier，有一条栈桥伸进海里）。
#   两条东西向大街把城区切成方正的街区：
#     北街  TempleStreet（西）+ NeonStrip（东）
#     南街  NoodleAlley（西，另有一条小巷向北通到大街）+ TeaHouseRow（东）
#   街区里是有墙有门的楼：天后庙、麻雀馆、夜总会、卡拉 OK、唐楼（楼顶是银龙的天台）、差馆、
#   当铺、士多、酒楼；BackAlley 是夹在差馆和车房之间的后巷，GarageYard 是黑虎围着铁闸的车房，
#   BuildingSite 是围起来的地盘，HerbalLane 是南边的凉茶药材巷。
# ---------------------------------------------------------------------------

MAP_W, MAP_H = 88, 47

LEGEND = {
    "W": "Wharf", "P": "FerryPier", "F": "FishMarket", "T": "TempleStreet", "N": "NeonStrip",
    "A": "NoodleAlley", "R": "TeaHouseRow", "B": "BackAlley", "G": "GarageYard", "H": "HerbalLane",
    "S": "BuildingSite",
    "t": "Tenements", "r": "Rooftops", "m": "MahjongHall", "h": "TinHauTemple", "p": "PoliceStation",
    "c": "GoldenPhoenixClub", "k": "KaraokeLounge", "w": "PawnShop", "s": "CornerStore", "j": "JadeBanquetHall",
    # 唐楼里一户一户的家
    "l": "LauFlat", "n": "WingFlat", "o": "ChoiFlat", "f": "FongFlat", "a": "MaFlat", "i": "FaiFlat", "y": "YanFlat",
    # 有门面的工作地方
    "e": "TeaHouse", "b": "LuckyBar", "g": "HerbalShop", "d": "DockOffice", "x": "TigerDen",
}
# 室内地点 → 楼所在的那块室外地
INDOOR = {
    "Tenements": "NoodleAlley", "Rooftops": "NoodleAlley", "MahjongHall": "NeonStrip", "TinHauTemple": "TempleStreet",
    "PoliceStation": "TempleStreet", "GoldenPhoenixClub": "NeonStrip", "KaraokeLounge": "NeonStrip",
    "PawnShop": "TeaHouseRow", "CornerStore": "TeaHouseRow", "JadeBanquetHall": "TeaHouseRow",
    "LauFlat": "NoodleAlley", "WingFlat": "NoodleAlley", "ChoiFlat": "NoodleAlley", "FongFlat": "NoodleAlley",
    "MaFlat": "NoodleAlley", "FaiFlat": "NoodleAlley", "YanFlat": "NoodleAlley",
    "TeaHouse": "TeaHouseRow", "LuckyBar": "NeonStrip", "HerbalShop": "HerbalLane", "DockOffice": "Wharf",
    "TigerDen": "GarageYard",
}

# 地图上新加的地方（地点表 LOCATIONS 之外的）：名字, 描述, 标签
VENUES = [
    ("GoldenPhoenixClub", "The Golden Phoenix nightclub: red lanterns, a live band and private booths upstairs. Tiger Sze drinks here for free.", "nightlife,social"),
    ("KaraokeLounge", "Lucky Star Karaoke: little rooms with sticky microphones; whoever pays for the room hears all the gossip.", "nightlife,social"),
    ("PawnShop", "Wo Hing pawn shop behind an iron grille: watches, jade and IOUs, no questions asked.", "trade,shop"),
    ("CornerStore", "A cramped corner store: cigarettes, soda, phone cards and credit for regulars.", "shop,social"),
    ("JadeBanquetHall", "The Jade Banquet Hall, where weddings, funerals and peace talks all get the same roast pig.", "food,social"),
    ("TeaHouse", "Uncle Wing's Golden Tea House: marble tables, milk tea in thick cups, the racing pages and every rumour on the street.", "food,social,work"),
    ("LuckyBar", "Miss Choi's Lucky Bar: a long counter, a jukebox, and a back booth where the Tigers drink on credit.", "nightlife,social,work"),
    ("HerbalShop", "Mr Chow's herbal shop: drawers of dried roots, a clay pot always simmering, and a cot in the back room where he sleeps.", "medicine,work,home"),
    ("DockOffice", "Ho's office at the head of the wharf: a desk of ledgers, a kettle, and a camp bed for nights the boats run late.", "work,home"),
    ("TigerDen", "The Black Tigers' workshop inside the garage yard: car lifts, a heavy bag, mattresses on the oil-stained floor.", "hideout,home"),
    ("LauFlat", "Mrs Lau's two rooms on the second floor: a shrine to her husband, Ah Kit's bunk, the smell of stock bones.", "home"),
    ("WingFlat", "Uncle Wing's tidy flat: framed photographs of the tea house in 1962 and a locked drawer.", "home"),
    ("ChoiFlat", "Miss Choi's flat: silk curtains, too many shoes, and a window she keeps shut.", "home"),
    ("FongFlat", "Auntie Fong's flat: lucky charms on every door frame and a radio always on.", "home"),
    ("MaFlat", "Big Ma's cubicle flat: a bed, a hotplate and the cart's spare wheel.", "home"),
    ("FaiFlat", "The cramped room Ah Fai shares with his little sister; her school books on the only table.", "home"),
    ("YanFlat", "A bed-space cubicle Ah Yan rents by the month; a photo of her mother taped to the wall.", "home"),
]


def draw_map():
    grid = [[" "] * MAP_W for _ in range(MAP_H)]
    decor = [[" "] * MAP_W for _ in range(MAP_H)]

    def rect(x0, y0, x1, y1, ch, d=None):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                grid[y][x] = ch
                if d is not None:
                    decor[y][x] = d

    def building(x0, y0, x1, y1, ch, doors):
        """墙围起来的楼 / 房间，里面是 ch；doors 是墙上的门洞。"""
        rect(x0, y0, x1, y1, "#")
        rect(x0 + 1, y0 + 1, x1 - 1, y1 - 1, ch)
        for x, y in doors:
            grid[y][x] = "+"

    def fenced(x0, y0, x1, y1, ch, gates, d=None):
        rect(x0, y0, x1, y1, "%")
        rect(x0 + 1, y0 + 1, x1 - 1, y1 - 1, ch, d)
        for x, y in gates:
            grid[y][x] = "+"

    rect(0, 0, 9, MAP_H - 1, "~")                      # 海港
    rect(10, 0, 21, 23, "W", "p")                      # 码头（木板）
    building(12, 2, 19, 8, "d", [(15, 8)])             # 何老板的码头办公室（也睡这儿）
    rect(10, 24, 21, MAP_H - 1, "P", "p")              # 渡轮码头
    rect(3, 29, 9, 32, "P", "p")                       # 伸进海里的栈桥
    rect(22, 0, 37, 12, "F", "c")                      # 街市（石板地）
    for x in range(24, 37, 3):
        for y in (2, 5, 8):
            decor[y][x] = "s"                          # 档口

    # 北街
    rect(22, 13, 53, 16, "T", "c")
    rect(54, 13, MAP_W - 1, 16, "N", "c")
    building(38, 0, 53, 12, "h", [(45, 12)])                                   # 天后庙
    building(54, 0, 66, 12, "m", [(60, 12)])                                   # 麻雀馆
    building(66, 0, 79, 12, "c", [(72, 12)])                                   # 夜总会
    building(79, 0, MAP_W - 1, 12, "k", [(83, 12)])                            # 卡拉 OK

    # 唐楼：中间一条楼梯走廊（Tenements），两边一户户的家；左上角是银龙的天台
    building(22, 17, 37, 32, "t", [(30, 17), (30, 32), (22, 28)])
    building(22, 17, 29, 23, "r", [(29, 20)])                                  # 天台（楼梯上去）
    building(32, 17, 37, 21, "n", [(32, 19)])                                  # 荣叔的家
    building(32, 21, 37, 25, "o", [(32, 23)])                                  # 蔡小姐的家
    building(32, 25, 37, 29, "f", [(32, 27)])                                  # 芳姐的家
    building(32, 29, 37, 32, "a", [(32, 30)])                                  # 大妈的板间房
    building(22, 23, 26, 27, "i", [(24, 27)])                                  # 阿辉和妹妹
    building(26, 23, 29, 27, "y", [(27, 27)])                                  # 阿欣的床位
    building(22, 29, 29, 32, "l", [(26, 29)])                                  # 刘太和阿杰
    for y in (18, 19, 20, 21, 22):
        grid[y][30] = grid[y][31] = "t"                                        # 走廊（天台墙边）

    rect(38, 17, 40, 32, "A", "c")                                             # 小巷：北街 ↔ 南街
    building(41, 17, 53, 32, "p", [(47, 17), (47, 32)])                        # 差馆
    rect(54, 17, 57, 32, "B", "g")                                             # 后巷
    building(58, 17, 65, 24, "b", [(61, 17)])                                  # 幸运酒吧（门开向霓虹街）
    building(58, 24, 65, 32, "e", [(61, 32)])                                  # 金记茶楼（门开向茶楼街）
    fenced(65, 17, MAP_W - 1, 32, "G", [(76, 32)], d="g")                      # 黑虎车房（铁闸开向茶楼街）
    building(75, 19, 85, 26, "x", [(80, 26)])                                  # 车房里的工场（黑虎住这儿）

    # 南街
    rect(22, 33, 53, 35, "A", "c")
    rect(54, 33, MAP_W - 1, 35, "R", "c")
    for x in range(68, MAP_W - 1, 6):
        decor[34][x] = "s"                                                     # 茶楼街的摊位

    # 南边的街区
    fenced(22, 36, 37, MAP_H - 1, "S", [(30, 36)], d="g")                      # 地盘
    rect(38, 36, 53, MAP_H - 1, "H", "=")                                      # 药材巷
    building(38, 39, 47, MAP_H - 1, "g", [(43, 39)])                           # 周记药材铺（后面住人）
    building(54, 36, 65, MAP_H - 1, "w", [(59, 36)])                           # 当铺
    building(65, 36, 76, MAP_H - 1, "s", [(70, 36)])                           # 士多
    building(76, 36, MAP_W - 1, MAP_H - 1, "j", [(81, 36)])                    # 酒楼

    return ["".join(r) for r in grid], ["".join(r) for r in decor]


def map_links(rows):
    """地图上实际连通的地点对（和 riverford 一样，用地形算）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))
    from terrain_gen import from_ascii, links_present

    terrain = from_ascii(rows, LEGEND, INDOOR.keys(), INDOOR)
    return sorted(tuple(sorted(p)) for p in links_present(terrain))

# 生意：名字, 原型, 地点, 老板, 每日保护费, 描述
SHOPS = [
    ("LauNoodleStall", "noodle_stall", "NoodleAlley", "MrsLau", 2, "Mrs Lau's noodle stall. One dollar a bowl of wonton noodles."),
    ("MaFishballCart", "fishball_cart", "FishMarket", "BigMa", 2, "Big Ma's curry fish ball cart. One dollar a stick."),
    ("GoldenTeaHouse", "tea_house", "TeaHouse", "UncleWing", 3, "Uncle Wing's Golden Tea House. Milk tea and a bun for a dollar."),
    ("LuckyBarCounter", "bar_counter", "LuckyBar", "MissChoi", 4, "Miss Choi's Lucky Bar. A dollar a bottle."),
    ("FongMahjongParlour", "bar_counter", "MahjongHall", "AuntieFong", 4, "Auntie Fong's mahjong parlour; a dollar buys a drink and a seat at the tables."),
    ("ChowHerbalShop", "herbal_counter", "HerbalShop", "MrChow", 2, "Mr Chow's herbal shop. A dollar for a brew that mends bruises."),
    ("HoWholesale", "wholesale", "Wharf", "DockerHo", 4, "Ho's wholesale crates. A dollar buys a crate of supplies for any shop."),
]


def slot(start, end, loc, activity, **kw):
    s = {"start": start, "end": end, "location": loc, "activity": activity, "goal": "Go to location"}
    s.update(kw)
    return s


def eat(start, loc, shop, what="eat"):
    """付钱给摊档 → 拿到吃的 → 吃掉。钱进摊主口袋。"""
    return [
        {"start": start, "end": start + 0.5, "location": loc, "activity": f"buy {what} at {shop}",
         "goal": "Use item on", "target_entity": shop, "item_kind": "cash", "once": True, "social": True},
        {"start": start + 0.5, "end": start + 1, "location": loc, "activity": what,
         "goal": "Use item", "item_kind": "food", "once": True, "social": True},
    ]


def collect(start, end, loc):
    return {"start": start, "end": end, "location": loc, "activity": "collect protection money",
            "action": "collect_dues", "once": True}


def sleep(start, end, loc):
    return {"start": start, "end": end, "location": loc, "activity": "sleep", "action": "sleep"}


P = []


def person(name, loc, role, personality, background, goal, values, mood, speech, *, routine, cash,
           works_at=None, power=10, trust=0, policy="share_gossip", desire="", fear="", secret="", bonds=None):
    P.append({
        "name": name, "located_at": loc, "role": role, "personality": personality, "background": background,
        "goal": goal, "values": values, "mood": mood, "hp": 100, "power_level": power, "speech_style": speech,
        "secret_policy": policy, "trust_toward_player": trust, "works_at": works_at,
        "desire": desire, "fear": fear, "secret": secret, "bonds": bonds or {},
        "routine": routine, "_cash": cash,
    })


person("AhLok", "FerryPier", "player", "hungry, quick, stubborn",
       "Came over on the night ferry with four dollars and a cousin's address that turned out to be a pawnshop.",
       "Find a place on these streets before the money runs out", "survival,loyalty", "wary", "quick and cheeky",
       routine=None, cash=4, policy="open")

person("DragonLeung", "Rooftops", "npc", "patient, charming, ruthless when crossed",
       "Built the Silver Dragons from three boys and a borrowed cleaver; now owns the Tenements and half the lane.",
       "Take back TeaHouseRow before the Tigers dig in", "brotherhood,face,order", "calm", "soft-spoken, never repeats himself",
       routine=[sleep(2, 10, "Rooftops"), slot(10, 11, "Rooftops", "morning tea at the shrine"),
                *eat(11, "NoodleAlley", "LauNoodleStall", "a bowl of noodles"),
                slot(12, 18, "Rooftops", "hold court with his brothers", social=True),
                *eat(18, "FishMarket", "MaFishballCart", "fish balls"),
                slot(19, 2, "Rooftops", "count the night's money and plan", social=True)],
       cash=14, power=14, policy="guarded",
       desire="To leave the district to someone strong enough to keep it — and retire to a flat with a sea view.",
       fear="Dying the way his own big brother did: alone in an alley, forgotten by the next week.",
       secret="His doctor told him his heart will not last five more years; he has told no one, not even Bones.",
       bonds={"Bones": "his oldest brother-in-arms; they burned incense together at Tin Hau thirty years ago",
              "AhKit": "sees a successor in him, and a weakness: the boy loves his mother more than the gang",
              "TigerSze": "they were boxing-gym friends once, before the garage fire"})
person("Bones", "Rooftops", "npc", "silent, loyal, frightening",
       "Thirty years with the Dragons; his younger brother was cut in BackAlley two winters ago.",
       "Keep the Dragons' streets paying", "loyalty,revenge", "grim", "few words, all of them final",
       routine=[sleep(0, 9, "Rooftops"), slot(9, 11, "Tenements", "walk the building and be seen", social=True),
                *eat(11, "FishMarket", "MaFishballCart", "fish balls"), collect(12, 13, "FishMarket"),
                slot(13, 18, "Rooftops", "guard the hall"), collect(18, 19, "NoodleAlley"),
                *eat(19, "NoodleAlley", "LauNoodleStall", "noodles"),
                slot(20, 24, "TempleStreet", "watch the night market", social=True)],
       cash=4, power=16, policy="guarded",
       desire="To see the man who cut his brother on his knees.",
       fear="That Leung will choose peace with the Tigers and call it wisdom.",
       secret="He was in BackAlley the night the Tigers' garage burned, and he has never said why.",
       bonds={"DragonLeung": "would die for him, and has nearly done so twice",
              "MadDogWai": "the Tiger he believes held the knife that cut his brother"})
person("AhKit", "LauFlat", "npc", "proud, restless, softer than he lets on",
       "Grew up on Mrs Lau's noodles and joined the Dragons at sixteen to make sure nobody pushed her around.",
       "Prove to Leung he can run a street of his own", "family,face", "restless", "street slang, loud when nervous",
       routine=[slot(7, 8, "LauFlat", "wake up at his mother's flat"),
                slot(8, 9, "NoodleAlley", "help his mother set up the stall", social=True),
                *eat(9, "NoodleAlley", "LauNoodleStall", "breakfast noodles"), collect(10, 11, "NoodleAlley"),
                collect(11, 12, "FishMarket"), slot(12, 17, "Rooftops", "hang around the hall", social=True),
                collect(17, 18, "TeaHouse"), *eat(18, "TeaHouse", "GoldenTeaHouse", "milk tea and a bun"),
                *eat(19, "LuckyBar", "LuckyBarCounter", "a beer"), slot(20, 23, "LuckyBar", "drink and show his face", social=True),
                sleep(23, 7, "LauFlat")],
       cash=3, power=11, trust=2,
       desire="To be the big brother of NoodleAlley, so his mother never pays anyone but him.",
       fear="That his mother will see what he really does on his rounds.",
       secret="He collects protection from his own mother, and pays her back in secret from his wages.",
       bonds={"MrsLau": "his mother; everything he does is supposed to be for her",
              "AhFai": "best friend since primary school, now a Black Tiger; they still share cigarettes on the pier",
              "DragonLeung": "the father he chose; he would do anything for a nod from him"})

person("TigerSze", "TigerDen", "npc", "loud, generous, quick to violence",
       "A former boxer who turned a repair yard into the Black Tigers' den; still blames the Dragons for the fire.",
       "Push the Dragons out of TeaHouseRow and NoodleAlley", "strength,revenge,generosity", "restless", "booming, laughs before he hits",
       routine=[sleep(2, 10, "TigerDen"), slot(10, 11, "TigerDen", "train with the heavy bag"),
                *eat(11, "TeaHouse", "GoldenTeaHouse", "milk tea"),
                slot(12, 18, "GarageYard", "hold court", social=True),
                *eat(18, "MahjongHall", "FongMahjongParlour", "a drink at the tables"),
                slot(19, 22, "MahjongHall", "play mahjong for high stakes", social=True),
                slot(22, 2, "GoldenPhoenixClub", "own the night at the Golden Phoenix", social=True)],
       cash=14, power=15, policy="guarded",
       desire="To rebuild the garage that burned, bigger, with his name over the gate.",
       fear="Looking weak in front of his boys.",
       secret="The fire started from his own wiring; he blamed the Dragons because his boys needed an enemy.",
       bonds={"DragonLeung": "the old friend he turned into an enemy with a lie",
              "MadDogWai": "his attack dog; he worries he cannot hold the leash much longer",
              "AhFai": "a cousin's son he promised to keep alive"})
person("MadDogWai", "TigerDen", "npc", "reckless, funny, cruel when drunk",
       "Earned his name biting a man's ear off in a dispute over a parking space.",
       "Make the Dragons pay for every dollar on TeaHouseRow", "fear,reputation", "wired", "fast, swearing, grinning",
       routine=[sleep(0, 10, "TigerDen"), slot(10, 12, "BackAlley", "hang around the alley", social=True),
                collect(12, 13, "LuckyBar"), *eat(13, "LuckyBar", "LuckyBarCounter", "a beer"),
                slot(14, 17, "BackAlley", "look for trouble", social=True), collect(17, 18, "TeaHouse"),
                collect(18, 19, "NoodleAlley"), *eat(19, "NoodleAlley", "LauNoodleStall", "noodles"),
                slot(20, 24, "KaraokeLounge", "sing badly with the boys", social=True)],
       cash=3, power=14, trust=-1, policy="lie",
       desire="To be feared on every street in the district, not just this one.",
       fear="Ending up like his father: a nobody who swept the ferry deck.",
       secret="He did not cut Bones' brother; he took the credit because it made people afraid of him.",
       bonds={"Bones": "enjoys being hated by him",
              "TigerSze": "the only man he listens to — mostly"})
person("AhFai", "FaiFlat", "npc", "anxious, kind, easily led",
       "Joined the Black Tigers because his cousin's family owed Sze; still lives in Leung's building.",
       "Earn enough to get his little sister into a proper school", "family,friendship", "uneasy", "polite, stammers when lying",
       routine=[slot(8, 9, "FaiFlat", "get his sister off to school"), *eat(9, "FishMarket", "MaFishballCart", "fish balls"),
                slot(10, 12, "BuildingSite", "carry bricks for day wages"), collect(12, 13, "MahjongHall"),
                slot(13, 18, "GarageYard", "run errands for the Tigers", social=True),
                *eat(18, "TeaHouse", "GoldenTeaHouse", "milk tea"),
                slot(19, 22, "FerryPier", "smoke on the pier", social=True), sleep(22, 8, "FaiFlat")],
       cash=2, power=8, trust=3,
       desire="To get out of the life before it gets him or his sister.",
       fear="Being told to hurt Ah Kit.",
       secret="He warns Ah Kit whenever the Tigers plan to move on NoodleAlley.",
       bonds={"AhKit": "best friend since primary school; the one person he trusts on the other side",
              "TigerSze": "owes him for his family's debt, and hates it"})

person("MrsLau", "LauFlat", "npc", "tough, warm, sharp-tongued",
       "Has sold noodles in the same lane for twenty-two years and buried a husband who owed the wrong people.",
       "Keep the stall open and her son alive", "family,hard work", "tired", "scolding and affectionate in the same breath",
       routine=[slot(6, 7, "LauFlat", "wake before dawn and boil the stock"), slot(7, 14, "NoodleAlley", "cook noodles", social=True),
                *eat(14, "TeaHouse", "GoldenTeaHouse", "milk tea"), slot(15, 21, "NoodleAlley", "cook noodles", social=True),
                slot(21, 22, "TinHauTemple", "light incense for her son", social=True), sleep(22, 6, "LauFlat")],
       cash=6, works_at="NoodleAlley", power=6, trust=3,
       desire="To see Ah Kit married, with a job she can tell the neighbours about.",
       fear="A phone call at night about her son.",
       secret="She knows exactly what Ah Kit does on his rounds, and pretends not to, because the truth would end them.",
       bonds={"AhKit": "her only son; she cooks his noodles first every morning",
              "UncleWing": "the old friend who lent her the money for the stall after her husband died"})
person("UncleWing", "WingFlat", "npc", "dignified, cautious, stubborn",
       "Runs the Golden Tea House his father opened; chairman of the street merchants for eleven years.",
       "Unite the merchants so no gang can squeeze them one by one", "dignity,community", "worried", "formal, old-fashioned",
       routine=[slot(6, 12, "TeaHouse", "open the tea house", social=True), *eat(12, "NoodleAlley", "LauNoodleStall", "noodles"),
                slot(13, 20, "TeaHouse", "run the tea house", social=True),
                slot(20, 21, "TinHauTemple", "pray for the street", social=True), sleep(21, 6, "WingFlat")],
       cash=8, works_at="TeaHouse", power=6, trust=2, policy="guarded",
       desire="To hand the tea house to his granddaughter without a single debt to any gang.",
       fear="That the merchants will break and each pay alone.",
       secret="He has written down every dollar paid to both gangs for three years, in a ledger under the till.",
       bonds={"MrsLau": "like a sister; he still worries about her boy",
              "SergeantTam": "an old classmate he no longer trusts"})
person("MissChoi", "ChoiFlat", "npc", "glamorous, calculating, lonely",
       "Bought the Lucky Bar with money nobody asks about; the Tigers drink free, so she pays more.",
       "Keep the bar full and both gangs drinking", "independence,money", "guarded", "flirtatious, never gives a straight answer",
       routine=[sleep(4, 12, "ChoiFlat"), *eat(12, "FishMarket", "MaFishballCart", "fish balls"),
                slot(13, 4, "LuckyBar", "run the Lucky Bar", social=True)],
       cash=10, works_at="LuckyBar", power=7, policy="lie",
       desire="To own the whole NeonStrip and answer to nobody.",
       fear="Going back to being nobody's girl from the ferry.",
       secret="She sells what she hears in the bar to Sergeant Tam.",
       bonds={"TigerSze": "his tab is three months unpaid, and she smiles about it",
              "SergeantTam": "her buyer for secrets; neither of them likes the arrangement"})
person("AuntieFong", "FongFlat", "npc", "nosy, superstitious, generous",
       "Has run the mahjong parlour since the old days; knows every gambler's debts.",
       "Keep the tables clacking", "luck,gossip", "cheerful", "chatty, full of proverbs",
       routine=[*eat(8, "TeaHouse", "GoldenTeaHouse", "breakfast tea"),
                slot(9, 23, "MahjongHall", "run the parlour", social=True), sleep(23, 8, "FongFlat")],
       cash=8, works_at="MahjongHall", power=5, trust=3,
       desire="To win the district raffle once before she dies.",
       fear="Bad luck: a broken mirror, a crow, a Tiger with a cleaver.",
       secret="She lets Ah Fai lose on purpose and keeps his debts off the books.",
       bonds={"AhFai": "a good boy in a bad crowd; she slips him buns",
              "MrsLau": "rivals at gossip, friends at funerals"})
person("MrChow", "HerbalShop", "npc", "gentle, curious, morally flexible",
       "A failed medical student who treats knife wounds without asking questions.",
       "Keep the shop open and patients alive, whoever they are", "healing,neutrality", "calm", "slow, precise, a little dry",
       routine=[slot(6, 7, "TinHauTemple", "morning walk to the temple"), slot(7, 12, "HerbalShop", "brew remedies", social=True),
                *eat(12, "TeaHouse", "GoldenTeaHouse", "milk tea"), slot(13, 20, "HerbalShop", "see patients", social=True),
                sleep(20, 6, "HerbalShop")],
       cash=6, works_at="HerbalShop", power=5, trust=3, policy="open",
       desire="To be called 'doctor' again by someone who means it.",
       fear="Treating a wound he cannot close.",
       secret="He stitched up the man who cut Bones' brother — and it was not Mad Dog Wai.",
       bonds={"SergeantTam": "the sergeant brings him the ones the hospital would ask about"})
person("BigMa", "MaFlat", "npc", "loud, cheerful, fearless",
       "Pushes her fish ball cart through the wet market at dawn; the Tigers smashed it last winter.",
       "Earn enough to buy a real shopfront", "hard work,neighbourliness", "cheerful", "loud, teasing",
       routine=[slot(6, 19, "FishMarket", "sell fish balls", social=True), *eat(19, "NoodleAlley", "LauNoodleStall", "noodles"),
                sleep(20, 6, "MaFlat")],
       cash=5, works_at="FishMarket", power=7, trust=4, policy="open",
       desire="A shopfront with her name painted in gold.",
       fear="The sound of a cart tipping over.",
       secret="She still has the cleaver one of the Tigers dropped that night, wrapped in newspaper.",
       bonds={"MadDogWai": "the one who tipped her cart; she spits when he passes",
              "MrsLau": "fellow hawker; they share ice and complaints"})
person("DockerHo", "DockOffice", "npc", "practical, greedy, fair in his own way",
       "Moves every crate that feeds the district's stalls; pays both gangs and complains to neither.",
       "Keep the boats unloading and the prices up", "money,reliability", "busy", "brusque, counts while talking",
       routine=[slot(6, 18, "Wharf", "run the wholesale crates", social=True), *eat(18, "FishMarket", "MaFishballCart", "fish balls"),
                *eat(19, "LuckyBar", "LuckyBarCounter", "a beer"), slot(20, 21, "LuckyBar", "talk prices over a beer", social=True),
                sleep(21, 6, "DockOffice")],
       cash=12, works_at="Wharf", power=9, policy="guarded",
       desire="To own a boat of his own instead of renting space on other men's.",
       fear="A strike, a storm, or a gang war that stops the boats.",
       secret="He smuggles for whichever gang pays more, and right now that is neither.",
       bonds={"AhYan": "his porter; he pays her less than she is worth and knows it"})
person("AhYan", "YanFlat", "npc", "strong, quiet, observant",
       "Carries crates at the wharf for a dollar a day; sends half of it home on the ferry.",
       "Save enough to bring her mother over", "family,hard work", "tired", "short, plain words",
       routine=[*eat(6, "FishMarket", "MaFishballCart", "fish balls"), slot(7, 17, "Wharf", "carry crates", social=True),
                slot(17, 18, "BuildingSite", "pick up extra day work"), *eat(18, "NoodleAlley", "LauNoodleStall", "noodles"),
                slot(19, 22, "TempleStreet", "walk the night market", social=True), sleep(22, 6, "YanFlat")],
       cash=2, works_at="Wharf", power=9, trust=3,
       desire="To bring her mother over on the ferry before winter.",
       fear="Being sent back.",
       secret="She has seen what Ho's crates carry at night.",
       bonds={"DockerHo": "her boss; fair enough, never generous",
              "AhFai": "they talk on the pier at night; she is the only one who knows he wants out"})
person("SergeantTam", "PoliceStation", "npc", "weary, shrewd, compromised",
       "Twenty years on the district; knows which fights to stop and which to look away from.",
       "Keep the bodies off the streets until his pension", "order,survival", "tired", "dry, cynical, kind by accident",
       routine=[*eat(7, "TeaHouse", "GoldenTeaHouse", "milk tea"), slot(8, 12, "PoliceStation", "paperwork"),
                slot(12, 13, "NoodleAlley", "patrol the lane", social=True), slot(13, 14, "TempleStreet", "patrol the market", social=True),
                slot(14, 15, "NeonStrip", "patrol the strip", social=True), slot(15, 18, "PoliceStation", "paperwork"),
                *eat(18, "FishMarket", "MaFishballCart", "fish balls"), slot(19, 21, "TempleStreet", "night patrol", social=True),
                sleep(21, 7, "PoliceStation")],
       cash=8, works_at="PoliceStation", power=12, policy="guarded",
       desire="Three more years, then a pension and a fishing rod.",
       fear="A war he cannot look away from.",
       secret="He takes a red packet from both gangs every month and keeps the peace by selling it.",
       bonds={"UncleWing": "an old classmate who no longer looks him in the eye",
              "MissChoi": "she sells him secrets; he protects her bar"})

ALL_NPCS = [p["name"] for p in P if p["role"] != "player"]


def M(n, role, l):
    return {"name": n, "role": role, "loyalty": l}


def R(w, stance, reason):
    return {"with": w, "stance": stance, "reason": reason}


def T(i, why):
    return {"id": i, "why": why}


FACTIONS = [
    dict(name="SamShui", kind="town", scale="large", description="The district: everyone who lives, sells or fights between the wharf and the temple.",
         goal="Keep the night market open and the trouble off the main street", join_policy="open", join_standing=0,
         members=[M(n, "resident", 6) for n in ALL_NPCS], territory={"TinHauTemple": 4, "TempleStreet": 3},
         traits=[T("welcoming", "the ferry lands someone new every night, and the district has always taken them in")]),
    dict(name="SilverDragons", kind="gang", scale="small",
         description="An old triad of sworn brothers on the rooftops above the tenements.",
         goal="Hold NoodleAlley and the Tenements; take back TeaHouseRow", wage=1,
         join_policy="trial", join_standing=2,
         trial={"item": "cash", "text": "Hand Dragon Leung a red packet with your own money in it, and bow."},
         members=[M("DragonLeung", "boss", 9), M("Bones", "enforcer", 9), M("AhKit", "runner", 6)],
         territory={"Rooftops": 7, "Tenements": 5, "NoodleAlley": 4, "FishMarket": 3, "TeaHouseRow": 2,
                    "PawnShop": 4, "CornerStore": 2, "KaraokeLounge": 2, "TeaHouse": 2},
         relations=[R("BlackTigers", "hostile", "the garage fire, and the night Bones' brother was cut in BackAlley")],
         traits=[T("zealous", "sworn brothers who burned incense together at Tin Hau"),
                 T("expansionist", "Leung wants the whole district secured before his heart gives out")]),
    dict(name="BlackTigers", kind="gang", scale="small",
         description="A younger, louder gang out of a car-repair yard, built on boxing gyms and grudges.",
         goal="Drive the Dragons out of TeaHouseRow and NoodleAlley", wage=1,
         join_policy="trial", join_standing=2,
         trial={"item": "beer", "text": "Bring Tiger Sze a cold beer and drink it with him in front of the boys."},
         members=[M("TigerSze", "boss", 9), M("MadDogWai", "enforcer", 8), M("AhFai", "runner", 4)],
         territory={"GarageYard": 7, "BackAlley": 5, "NeonStrip": 4, "MahjongHall": 4, "TeaHouseRow": 3, "NoodleAlley": 2,
                    "GoldenPhoenixClub": 6, "KaraokeLounge": 3, "TigerDen": 8, "LuckyBar": 4, "TeaHouse": 3},
         traits=[T("militant", "Sze came up through the boxing gyms and settles everything with fists"),
                 T("vengeful", "they still blame the Dragons for the garage fire")]),
    dict(name="DistrictPolice", kind="authority", scale="medium",
         description="One tired sergeant and a station that closes at nine.",
         goal="Keep the peace, or at least keep it quiet", join_policy="closed",
         members=[M("SergeantTam", "sergeant", 7)], territory={"PoliceStation": 8, "HerbalLane": 3, "HerbalShop": 3},
         relations=[R("SilverDragons", "rival", "Leung's lawyer had Tam's best case thrown out"),
                    R("BlackTigers", "rival", "Sze's boys broke a constable's arm last year")],
         traits=[T("secretive", "Tam takes red packets from both gangs and tells no one")]),
    dict(name="StreetMerchants", kind="guild", scale="medium", parent="SamShui",
         description="The hawkers and shopkeepers who pay everybody and are owed by nobody.",
         goal="Stop paying two gangs for the same street", join_policy="sponsor", join_standing=3,
         members=[M("UncleWing", "chairman", 8), M("MrsLau", "member", 7), M("BigMa", "member", 7),
                  M("AuntieFong", "member", 6), M("MrChow", "member", 5), M("MissChoi", "member", 4), M("DockerHo", "member", 5)],
         territory={"TempleStreet": 4, "CornerStore": 3, "JadeBanquetHall": 4},
         relations=[R("BlackTigers", "rival", "the Tigers smashed Big Ma's cart last winter"),
                    R("DistrictPolice", "friendly", "the sergeant at least eats at their stalls")],
         traits=[T("mercantile", "they hold together by trading favours and credit"),
                 T("pacifist", "Uncle Wing has buried enough young men from this street")]),
    dict(name="DockCrew", kind="crew", scale="small", parent="SamShui",
         description="Ho's porters at the wharf: a dollar a day, paid at sunset.",
         goal="Keep the crates moving", join_policy="sponsor", join_standing=2, wage=1,
         members=[M("DockerHo", "boss", 8), M("AhYan", "porter", 6)], territory={"Wharf": 5, "DockOffice": 6},
         relations=[R("StreetMerchants", "friendly", "every stall depends on Ho's crates")]),
    dict(name="LauFamily", kind="family", scale="small", join_policy="closed",
         description="Mrs Lau and her son, in a two-room flat in Leung's building.",
         goal="Stay together", members=[M("MrsLau", "mother", 10), M("AhKit", "son", 8)], territory={}),
]

SOCIAL = [
    ("AhKit", "FRIENDS_WITH", "AhFai"), ("MrsLau", "FAMILY_OF", "AhKit"), ("Bones", "RIVALS_WITH", "MadDogWai"),
    ("UncleWing", "FRIENDS_WITH", "MrsLau"), ("AhYan", "FRIENDS_WITH", "AhFai"), ("DragonLeung", "KNOWS", "TigerSze"),
    ("BigMa", "FRIENDS_WITH", "MrsLau"), ("AuntieFong", "LIKES", "AhFai"),
] + [(n, "KNOWS", m) for n in ("SergeantTam", "UncleWing") for m in ALL_NPCS if m not in (n,)]

FACTS = [
    dict(name="TamsRedPackets", text="Sergeant Tam takes a red packet from both gangs every month to look the other way.",
         secret_level=3, known_by=["SergeantTam", "DragonLeung", "TigerSze"], faction="DistrictPolice"),
    dict(name="WingsLedger", text="Uncle Wing keeps a ledger of every dollar the merchants have paid both gangs for three years.",
         secret_level=2, known_by=["UncleWing", "MrsLau"], faction="StreetMerchants"),
    dict(name="GarageWiring", text="The Black Tigers' garage fire started from bad wiring, not from the Dragons.",
         secret_level=3, known_by=["TigerSze"], faction="BlackTigers"),
    dict(name="LeungsHeart", text="Dragon Leung's heart is failing; he is looking for a successor.",
         secret_level=3, known_by=["DragonLeung"], faction="SilverDragons"),
    dict(name="HowProtectionWorks", text="Whoever holds a street collects protection from its shops every day; three days behind and they smash the place.",
         secret_level=0, known_by=ALL_NPCS + ["AhLok"]),
    dict(name="WharfWork", text="Ho at the wharf pays porters a dollar a day; the DockCrew takes anyone Ah Yan vouches for.",
         secret_level=0, known_by=["DockerHo", "AhYan", "BigMa", "AhLok"]),
]
RUMORS = [
    dict(name="WaiCutBones", text="Everyone says Mad Dog Wai cut Bones' brother in BackAlley.", about="", believed_by=["Bones", "BigMa", "AhKit"]),
    dict(name="NewcomersOnFerry", text="People say a crew from upriver is coming in on the night ferry, looking for a street of their own.",
         about="", believed_by=["DockerHo", "AhYan", "AuntieFong"]),
]


def build(repo: Path) -> None:
    items = []
    for name, arch, loc, owner, fee, desc in SHOPS:
        items.append({"name": name, "archetype": arch, "located_at": loc, "description": desc,
                      "script": {"initial_state": {"owner": owner, "protection_fee": fee}}})
    tenants = ["MrsLau", "UncleWing", "MissChoi", "AuntieFong", "BigMa", "AhYan", "AhFai"]
    items.append({"name": "TenementRentBook", "archetype": "rent_book", "located_at": "Tenements",
                  "description": "Dragon Leung's rent book. A dollar a day per flat.",
                  "script": {"initial_state": {"landlord": "DragonLeung", "rent": 1, "tenants": tenants}}})
    n = 0
    persons = []
    for p in P:
        for _ in range(p.pop("_cash")):
            n += 1
            items.append({"name": f"Cash{n}", "archetype": "cash", "held_by": p["name"]})
        if p["routine"] is None:
            p.pop("routine")
        if not p.get("works_at"):
            p.pop("works_at", None)
        persons.append(p)

    rows, decor = draw_map()
    links = map_links(rows)
    places = [(n, d, t) for n, d, t, _, _ in LOCATIONS] + VENUES
    assert sorted(n for n, _, _ in places) == sorted(set(LEGEND.values())), "legend / places mismatch"
    locations = []
    for name, desc, tags in places:
        loc = {"name": name, "description": desc, "tags": tags}
        if name in INDOOR:
            loc.update(indoor=True, parent=INDOOR[name])
        locations.append(loc)

    world = {
        "id": "triad_streets",
        "title": "Neon Streets of Sam Shui",
        "premise": (
            "A crowded harbour district at night. Two triads — the old Silver Dragons on the rooftops and the young "
            "Black Tigers in their garage — fight street by street for the right to collect protection money. "
            "Every dollar changes hands: hawkers sell noodles and fish balls, buy their stock at the wharf, pay "
            "whoever holds their street, and the gangsters spend it again at the same stalls and bars. A newcomer "
            "has just stepped off the night ferry with four dollars."
        ),
        "locations": locations,
        "adjacencies": [{"a": a, "b": b} for a, b in links],
        "persons": persons,
        "items": items,
        "quests": [{
            "name": "AStreetToCallHome",
            "description": "Four dollars won't last. The Dragons, the Tigers, the merchants and the wharf all want something from a newcomer.",
            "stage": "arrived", "state": "active",
            "objective": "Find a way to eat every day on these streets: work the wharf, join a side, or play them against each other.",
            "involves": ["AhLok", "DragonLeung", "TigerSze", "UncleWing", "DockerHo"],
        }],
        "facts": FACTS,
        "rumors": RUMORS,
        "social": [{"a": a, "rel": r, "b": b} for a, r, b in SOCIAL],
        "factions": FACTIONS,
        "terrain": {"legend": LEGEND, "rows": rows, "decor": decor},
    }
    out = repo / "worlds" / "triad_streets.json"
    out.write_text(json.dumps(world, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(locations)} places, {len(persons)} people, {n} dollars, {len(items)} items")
    for a, b in links:
        print(f"  {a} <-> {b}")
    print("\n".join(rows))


if __name__ == "__main__":
    build(Path(sys.argv[1]))
