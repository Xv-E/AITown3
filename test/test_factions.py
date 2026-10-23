"""势力机制：成员继承、立场、态度、声望波及（不依赖 Neo4j）。"""

import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
for sub in ("agent", "utility", "actions", ""):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import factions  # noqa: E402
from factions import Membership  # noqa: E402


class GraphStub:
    """用 town_daily_life 的势力搭一个内存图，替换 factions 里的查询函数。"""

    MEMBERS = {
        "Rowan": [("Axemen", "gang", "small", "boss", 9), ("OldCampKin", "family", "small", "guardian", 8),
                  ("EmberCamp", "town", "large", "lumberjack", 7)],
        "Reed": [("Axemen", "gang", "small", "runner", 6), ("EmberCamp", "town", "large", "hauler", 6)],
        "Ash": [("BerryCircle", "crew", "small", "keeper", 7), ("OldCampKin", "family", "small", "ward", 9),
                ("EmberCamp", "town", "large", "firekeeper", 9)],
        "Sage": [("BerryCircle", "crew", "small", "elder", 8), ("EmberCamp", "town", "large", "gatherer", 8)],
        "Traveler": [],
    }
    STANCES = {frozenset(("Axemen", "BerryCircle")): ("rival", "who eats first")}

    def __init__(self):
        self.standing = {}

    def memberships(self, world, person):
        return [Membership(f, k, s, r, float(l)) for f, k, s, r, l in self.MEMBERS.get(person, [])]

    def faction_relations(self, world, a, b):
        out = []
        for x in sorted(set(a)):
            for y in sorted(set(b)):
                hit = self.STANCES.get(frozenset((x, y)))
                if hit and x != y:
                    out.append({"a": x, "b": y, "stance": hit[0], "reason": hit[1]})
        return out

    def stances_of(self, world, faction):
        return [
            {"other": next(iter(k - {faction})), "stance": v[0]}
            for k, v in self.STANCES.items() if faction in k
        ]

    def get(self, world, person, faction):
        return self.standing.get((person, faction), 0.0)

    def adjust(self, world, person, faction, delta):
        v = max(-10.0, min(10.0, self.get(world, person, faction) + delta))
        self.standing[(person, faction)] = round(v, 2)
        return self.standing[(person, faction)]


class FactionTestCase(unittest.TestCase):
    def setUp(self):
        self.g = GraphStub()
        self._saved = {}
        for name, fn in (
            ("memberships", self.g.memberships),
            ("faction_relations", self.g.faction_relations),
            ("stances_of", self.g.stances_of),
            ("standing", self.g.get),
            ("adjust_standing", self.g.adjust),
            ("faction_secret_lines", lambda world, me, view: []),
        ):
            self._saved[name] = getattr(factions, name)
            setattr(factions, name, fn)
        self.world = object()

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(factions, name, fn)


class TestAttitude(FactionTestCase):
    def test_family_outweighs_rival_crews(self):
        view = factions.pair_view(self.world, "Ash", "Rowan")
        self.assertIn("OldCampKin", view.shared)
        self.assertTrue(any(c["stance"] == "rival" for c in view.cross))
        self.assertGreater(view.score, 1.5)  # 家人优先，但被帮派对立拉低
        self.g.STANCES = {}
        self.assertGreater(factions.pair_view(self.world, "Ash", "Rowan").score, view.score)

    def test_rival_gangs_lean_away(self):
        view = factions.pair_view(self.world, "Sage", "Reed")
        self.assertEqual(view.shared, ["EmberCamp"])
        self.assertLess(view.score, 1.0)

    def test_stranger_standing_counts(self):
        self.assertEqual(factions.pair_view(self.world, "Rowan", "Traveler").score, 0.0)
        self.g.standing[("Traveler", "Axemen")] = -6.0
        view = factions.pair_view(self.world, "Rowan", "Traveler")
        self.assertLess(view.score, -1.5)
        self.assertIn("Axemen thinks of them as hated", view.reasons)

    def test_block_mentions_factions_and_rule(self):
        text = factions.faction_block(self.world, "Ash", "Rowan")
        self.assertIn("Your factions:", text)
        self.assertIn("Your BerryCircle is rival with their Axemen — who eats first.", text)
        self.assertIn("You and Rowan are both of OldCampKin.", text)
        self.assertIn("never hand rivals", text)


class TestRipple(FactionTestCase):
    def test_attack_hits_victims_factions_and_pleases_rivals(self):
        notes = factions.on_action(self.world, "attack", ["Traveler", "Rowan", "Forest"])
        st = self.g.standing
        self.assertAlmostEqual(st[("Traveler", "Axemen")], -3.0)
        self.assertAlmostEqual(st[("Traveler", "OldCampKin")], -3.0)
        self.assertAlmostEqual(st[("Traveler", "EmberCamp")], -0.9)
        self.assertGreater(st[("Traveler", "BerryCircle")], 0)  # 敌人的敌人
        self.assertTrue(any("attacked Rowan" in n for n in notes))

    def test_betrayal_inside_own_faction_hurts_more(self):
        factions.ripple(self.world, "Reed", "Rowan", -6.0)
        self.assertAlmostEqual(self.g.standing[("Reed", "Axemen")], -4.5)

    def test_gift_raises_standing(self):
        factions.on_action(self.world, "give", ["Traveler", "Berry1", "Sage", "BerryGrove"])
        self.assertAlmostEqual(self.g.standing[("Traveler", "BerryCircle")], 1.0)
        self.assertLess(self.g.standing[("Traveler", "Axemen")], 0)

    def test_unknown_capability_is_noop(self):
        self.assertEqual(factions.on_action(self.world, "move", ["Traveler", "Forest"]), [])
        self.assertEqual(self.g.standing, {})


class TestLabels(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(factions.standing_label(7), "trusted")
        self.assertEqual(factions.standing_label(0), "unknown")
        self.assertEqual(factions.standing_label(-5), "hated")
        self.assertEqual(factions.attitude_label(-5), "hostile")


class TestBlueprintFactions(unittest.TestCase):
    def test_template_world_factions_load(self):
        from director.world_loader import load_world

        bp = load_world("town_daily_life")
        by = {f.name: f for f in bp.factions}
        self.assertEqual(set(by), {"EmberCamp", "Axemen", "BerryCircle", "OldCampKin"})
        self.assertEqual(by["Axemen"].kind, "gang")
        self.assertEqual(by["Axemen"].parent, "EmberCamp")
        self.assertEqual(by["Axemen"].relations[0]["stance"], "rival")
        fact = next(f for f in bp.facts if f.name == "AxemenWoodpile")
        self.assertEqual(fact.faction, "Axemen")
        # to_dict → from_dict 不丢势力
        from director.blueprint import EventBlueprint

        again = EventBlueprint.from_dict({k: v for k, v in bp.to_dict().items() if k != "counts"})
        self.assertEqual(len(again.factions), 4)

    def test_normalize_drops_dangling_refs(self):
        from director.blueprint import EventBlueprint

        bp = EventBlueprint.from_dict({
            "locations": [{"name": "Camp"}],
            "persons": [{"name": "Ash", "located_at": "Camp"}],
            "factions": [
                {"name": "Kin", "kind": "family", "parent": "Nowhere",
                 "members": [{"name": "Ash", "loyalty": 99}, {"name": "Ghost"}],
                 "relations": [{"with": "Nobody", "stance": "hostile"}, {"with": "Gang", "stance": "weird"}]},
                {"name": "Gang", "kind": "gang"},
            ],
        })
        kin = next(f for f in bp.factions if f.name == "Kin")
        self.assertEqual(kin.scale, "small")
        self.assertIsNone(kin.parent)
        self.assertEqual([m["name"] for m in kin.members], ["Ash"])
        self.assertEqual(kin.members[0]["loyalty"], 10)
        self.assertEqual(kin.relations, [{"with": "Gang", "stance": "neutral", "reason": ""}])

    def test_all_template_worlds_load(self):
        from director.world_loader import list_worlds, load_world

        for w in list_worlds():
            bp = load_world(w["id"])
            for f in bp.factions:
                self.assertTrue(f.members, f"{w['id']}:{f.name} has no members")


if __name__ == "__main__":
    unittest.main()
