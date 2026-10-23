"""街头经济：收保护费、过界收数、差人在场、拖欠累计；以及古惑仔街道世界的完整性（不依赖 Neo4j）。"""

import json
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
for sub in ("agent", "utility", "actions", ""):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import street_economy as eco  # noqa: E402


class FakeStreets:
    """NoodleAlley：银龙收 Mrs Lau 的数；Ah Kit（银龙）、Mad Dog Wai（黑虎）都可能来收。"""

    def __init__(self):
        self.wallet = {"MrsLau": 6, "AhKit": 0, "DragonLeung": 10, "MadDogWai": 0, "UncleWing": 3}
        self.where = {"AhKit": "NoodleAlley", "MadDogWai": "NoodleAlley", "SergeantTam": "PoliceStation"}
        self.gangs = {"AhKit": ["SilverDragons"], "DragonLeung": ["SilverDragons"], "MadDogWai": ["BlackTigers"]}
        self.biz = {
            "LauNoodleStall": {"owner": "MrsLau", "location": "NoodleAlley", "fee": 2.0, "stock": 4,
                               "controller": "SilverDragons", "rackets": {"SilverDragons": 2.5}},
            "GoldenTeaHouse": {"owner": "UncleWing", "location": "TeaHouseRow", "fee": 3.0, "stock": 5,
                               "controller": "DistrictPolice", "rackets": {}},
        }
        self.turf = {}
        self.standing = {}
        self.tension = {}
        self.events = []

    def businesses(self, world, location=None):
        return [
            {"name": n, "owner": b["owner"], "location": b["location"], "fee": b["fee"], "stock": b["stock"],
             "takings": 0, "controller": b["controller"],
             "rackets": [{"faction": f, "dues": d, "paid": 0} for f, d in b["rackets"].items()]}
            for n, b in self.biz.items() if location is None or b["location"] == location
        ]

    def transfer(self, world, frm, to, n):
        n = min(int(n), self.wallet.get(frm, 0))
        if n <= 0 or frm == to:
            return 0
        self.wallet[frm] -= n
        self.wallet[to] = self.wallet.get(to, 0) + n
        return n

    def set_dues(self, world, faction, item, *, dues, paid_delta=0, fee=0.0):
        self.biz[item]["rackets"][faction] = round(max(0.0, dues), 2)

    def police(self, world, loc, exclude):
        return [{"person": p, "faction": "DistrictPolice"} for p, l in self.where.items()
                if l == loc and p == "SergeantTam" and p != exclude]

    def adjust(self, world, person, faction, d):
        self.standing[(person, faction)] = self.standing.get((person, faction), 0.0) + d
        return self.standing[(person, faction)]


class EconomyTestCase(unittest.TestCase):
    def setUp(self):
        self.g = g = FakeStreets()
        self.saved = {}
        patches = {
            "businesses": g.businesses,
            "cash_of": lambda world, p: g.wallet.get(p, 0),
            "transfer_cash": g.transfer,
            "gangs_of": lambda world, p: list(g.gangs.get(p, [])),
            "_set_dues": g.set_dues,
            "_police_here": g.police,
            "_add_turf": lambda world, f, loc, a: g.turf.__setitem__((f, loc), g.turf.get((f, loc), 0) + a),
            "_add_tension": lambda world, a, b, t: g.tension.__setitem__(frozenset((a, b)), t),
            "leader_of": lambda world, f: {"SilverDragons": "DragonLeung", "BlackTigers": "TigerSze"}.get(f),
            "adjust_standing": g.adjust,
            "_location_of": lambda world, p: g.where.get(p),
            "_chronicle": lambda world, text, fs, loc, memory=None: g.events.append(text),
            "_gang_members": lambda world, f: {p for p, gs in g.gangs.items() if f in gs},
            "_faction_kind": lambda world, f: {"SilverDragons": "gang", "BlackTigers": "gang"}.get(f, "authority"),
            "_rows": lambda world, q, **kw: [],
        }
        for name, fn in patches.items():
            self.saved[name] = getattr(eco, name)
            setattr(eco, name, fn)
        self.world = object()

    def tearDown(self):
        for name, fn in self.saved.items():
            setattr(eco, name, fn)


class TestCollect(EconomyTestCase):
    def test_collect_own_racket_with_boss_cut(self):
        res = eco.collect(self.world, "AhKit")
        self.assertEqual(res["collected"], 2)
        self.assertEqual(self.g.wallet["MrsLau"], 4)
        self.assertEqual(self.g.wallet["AhKit"], 1)
        self.assertEqual(self.g.wallet["DragonLeung"], 11)
        self.assertAlmostEqual(self.g.biz["LauNoodleStall"]["rackets"]["SilverDragons"], 0.5)
        self.assertAlmostEqual(self.g.turf[("SilverDragons", "NoodleAlley")], eco.TURF_PER_PAYMENT)

    def test_money_is_conserved(self):
        before = sum(self.g.wallet.values())
        eco.collect(self.world, "AhKit")
        eco.collect(self.world, "MadDogWai")
        self.assertEqual(sum(self.g.wallet.values()), before)

    def test_owner_who_cannot_pay(self):
        self.g.wallet["MrsLau"] = 0
        res = eco.collect(self.world, "AhKit")
        self.assertEqual(res["collected"], 0)
        self.assertIn("can't pay", res["notes"][0])

    def test_muscling_in_takes_rivals_money_and_starts_trouble(self):
        res = eco.collect(self.world, "MadDogWai")
        self.assertEqual(res["collected"], 2)
        self.assertEqual(self.g.wallet["MadDogWai"], 2)  # 黑虎老大不在这里抽成：钱先在小弟手里
        self.assertAlmostEqual(self.g.biz["LauNoodleStall"]["rackets"]["SilverDragons"], 0.5)
        self.assertEqual(self.g.standing[("MadDogWai", "SilverDragons")], eco.MUSCLE_IN_STANDING)
        self.assertEqual(self.g.tension[frozenset(("BlackTigers", "SilverDragons"))], eco.MUSCLE_IN_TENSION)
        self.assertEqual(self.g.turf[("BlackTigers", "NoodleAlley")], eco.TURF_PER_NEW_RACKET)
        self.assertTrue(self.g.events)

    def test_police_present_blocks_collection(self):
        self.g.where["SergeantTam"] = "NoodleAlley"
        res = eco.collect(self.world, "AhKit")
        self.assertEqual(res["collected"], 0)
        self.assertEqual(self.g.wallet["MrsLau"], 6)
        self.assertEqual(self.g.standing[("AhKit", "DistrictPolice")], eco.POLICE_STANDING)

    def test_unprotected_business_gets_a_new_racket(self):
        self.g.where["AhKit"] = "TeaHouseRow"
        res = eco.collect(self.world, "AhKit")
        self.assertEqual(res["collected"], 3)
        self.assertIn("started taking protection", self.g.events[0])
        self.assertEqual(self.g.biz["GoldenTeaHouse"]["rackets"]["SilverDragons"], 0.0)

    def test_civilian_cannot_collect(self):
        res = eco.collect(self.world, "UncleWing", "NoodleAlley")
        self.assertEqual(res["collected"], 0)
        self.assertIn("no gang", res["notes"][0])


class TestAccrue(EconomyTestCase):
    def test_dues_grow_for_holder_only(self):
        eco.accrue_dues(self.world, 12.0)
        self.assertAlmostEqual(self.g.biz["LauNoodleStall"]["rackets"]["SilverDragons"], 3.5)
        self.assertEqual(self.g.biz["GoldenTeaHouse"]["rackets"], {})  # 差人的地盘没人收

    def test_economy_text_for_owner_and_gangster(self):
        eco.has_economy = lambda world: True
        try:
            owner = eco.economy_text(self.world, "MrsLau")
            gangster = eco.economy_text(self.world, "AhKit")
        finally:
            eco.has_economy = self.saved.get("has_economy", eco.has_economy)
        self.assertIn("You owe SilverDragons $2.5", owner)
        self.assertIn("owes SilverDragons $2", gangster)
        self.assertIn("collect_dues", gangster)


class TestTriadStreetsWorld(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from director.world_loader import load_world

        cls.bp = load_world("triad_streets")
        with open(os.path.join(ROOT, "worlds", "triad_streets.json"), encoding="utf-8") as f:
            cls.raw = json.load(f)

    def test_big_connected_hand_made_map(self):
        locs = {l.name for l in self.bp.locations}
        self.assertGreaterEqual(len(locs), 15)
        self.assertTrue(all(l.map and l.grid for l in self.bp.locations))
        nb = {n: set() for n in locs}
        for e in self.bp.adjacencies:
            nb[e.a].add(e.b)
            nb[e.b].add(e.a)
        seen, todo = {"TempleStreet"}, ["TempleStreet"]
        while todo:
            for m in nb[todo.pop()] - seen:
                seen.add(m)
                todo.append(m)
        self.assertEqual(seen, locs)

    def test_every_shop_has_an_owner_who_works_there(self):
        people = {p.name: p for p in self.bp.persons}
        shops = [i for i in self.bp.items if (i.script or {}).get("initial_state", {}).get("protection_fee")]
        self.assertGreaterEqual(len(shops), 6)
        for s in shops:
            owner = s.script["initial_state"]["owner"]
            self.assertIn(owner, people, s.name)
            self.assertEqual(people[owner].works_at, s.located_at, s.name)

    def test_routines_point_at_real_places_and_shops(self):
        locs = {l.name for l in self.bp.locations}
        items = {i.name for i in self.bp.items}
        for p in self.bp.persons:
            for slot in p.routine:
                self.assertIn(slot["location"], locs, p.name)
                if slot.get("target_entity"):
                    self.assertIn(slot["target_entity"], items, p.name)

    def test_everyone_has_some_cash_and_the_district_has_a_money_supply(self):
        held = {}
        for i in self.bp.items:
            if i.archetype == "cash":
                held[i.held_by] = held.get(i.held_by, 0) + 1
        self.assertEqual(set(held), {p.name for p in self.bp.persons})
        self.assertGreater(sum(held.values()), 80)

    def test_factions_rent_and_gangs(self):
        by = {f.name: f for f in self.bp.factions}
        self.assertEqual({by["SilverDragons"].kind, by["BlackTigers"].kind}, {"gang"})
        self.assertEqual(by["SilverDragons"].relations[0]["stance"], "hostile")
        self.assertTrue(by["SilverDragons"].wage and by["DockCrew"].wage)
        people = {p.name for p in self.bp.persons}
        book = next(i for i in self.bp.items if i.name == "TenementRentBook")
        self.assertTrue(set(book.script["initial_state"]["tenants"]) <= people)
        self.assertIn(book.script["initial_state"]["landlord"], by["SilverDragons"].members[0]["name"])

    def test_cash_pays_every_kind_of_shop(self):
        from content import apply_archetype

        cash = apply_archetype({"name": "Cash1", "archetype": "cash"})
        targets = {e["target"] for e in cash["effects_on"]}
        for i in self.bp.items:
            fee = (i.script or {}).get("initial_state", {}).get("protection_fee")
            if fee:
                self.assertIn(f"kind:{i.item_kind}", targets, i.name)
        # 付钱是换手：钞票交给 @target.owner，不会被销毁
        for e in cash["effects_on"]:
            self.assertFalse(e["consume"])
            self.assertTrue(any(op.get("op") == "link" and op.get("a") == "@target.owner" for op in e["ops"]))


if __name__ == "__main__":
    unittest.main()
