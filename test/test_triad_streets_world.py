"""古惑仔街道世界的完整性（不依赖 Neo4j）。街头经济的规则见 test_groups.py。"""

import json
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
for sub in ("agent", "utility", "actions", ""):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

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
        # 手画的方正街区：每个地点在地图上都有自己的地
        self.assertTrue(self.bp.terrain and self.bp.terrain.get("rows"))
        self.assertEqual(set(self.bp.terrain["legend"].values()), locs)
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
