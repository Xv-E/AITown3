"""势力演化：地盘、张力 → 立场、忠诚、新势力、结算时钟（不依赖 Neo4j）。"""

import os
import random
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
for sub in ("agent", "utility", "actions", ""):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import faction_sim as fs  # noqa: E402


class View:
    def __init__(self, score):
        self.score = score


class FakeSim:
    """营火营地的小号：Axemen（伐木帮）与 BerryCircle（采集圈）在营火处对立，都属于 EmberCamp。"""

    def __init__(self):
        self.factions = {
            "EmberCamp": {"kind": "town", "scale": "large", "traits": []},
            "Axemen": {"kind": "gang", "scale": "small", "traits": []},
            "BerryCircle": {"kind": "crew", "scale": "small", "traits": []},
        }
        self.members = {
            ("Rowan", "Axemen"): {"loyalty": 9.0, "role": "boss"},
            ("Reed", "Axemen"): {"loyalty": 6.0, "role": "runner"},
            ("Sage", "BerryCircle"): {"loyalty": 8.0, "role": "elder"},
            ("Rowan", "EmberCamp"): {"loyalty": 7.0, "role": "member"},
        }
        self.where = {"Rowan": "Forest", "Reed": "Forest", "Sage": "BerryGrove"}
        self.control = {("Axemen", "Forest"): 6.0, ("BerryCircle", "BerryGrove"): 6.0,
                        ("Axemen", "Campfire"): 2.0, ("BerryCircle", "Campfire"): 2.0,
                        ("EmberCamp", "Campfire"): 6.0}
        self.locations = ["Forest", "BerryGrove", "Campfire", "River"]
        self.controlled_by = {}
        self.rels = {frozenset(("Axemen", "BerryCircle")):
                     {"a": "Axemen", "b": "BerryCircle", "stance": "rival", "tension": 0.0, "reason": "who eats first"}}
        self.nested = {frozenset(("Axemen", "EmberCamp")), frozenset(("BerryCircle", "EmberCamp"))}
        self.events = []
        self.meta = {}
        self.scores = {}
        self.founded = []
        self.refresh()

    # —— 读 ——
    def active(self, world):
        return [{"name": n, "kind": f["kind"], "scale": f["scale"]} for n, f in sorted(self.factions.items())]

    def control_rows(self, world):
        return [{"faction": f, "location": l, "strength": s} for (f, l), s in self.control.items()]

    def presence(self, world):
        return [{"person": p, "faction": f, "loyalty": m["loyalty"], "role": m["role"],
                 "location": self.where.get(p), "is_player": False}
                for (p, f), m in self.members.items()]

    def relations(self, world):
        return list(self.rels.values())

    # —— 写 ——
    def set_strength(self, world, f, loc, s):
        if s <= 0.05:
            self.control.pop((f, loc), None)
        else:
            self.control[(f, loc)] = round(min(10.0, s), 2)

    def refresh(self, world=None):
        best = {}
        for (f, loc), s in self.control.items():
            if s >= fs.CONTROL_MIN and (loc not in best or s > best[loc][1]):
                best[loc] = (f, s)
        self.controlled_by = {loc: best.get(loc, (None,))[0] for loc in self.locations}
        return dict(self.controlled_by)

    def set_stance(self, world, a, b, stance, reason, tension=0.0):
        self.rels[frozenset((a, b))] = {"a": a, "b": b, "stance": stance, "tension": tension, "reason": reason}

    def set_tension(self, world, a, b, t):
        self.rels[frozenset((a, b))]["tension"] = round(t, 2)

    def set_loyalty(self, world, p, f, l):
        self.members[(p, f)]["loyalty"] = round(max(0.0, min(10.0, l)), 2)

    def record(self, world, now, kind, text, factions, *, location="", memory=None):
        ev = {"day": fs.day_of(now), "kind": kind, "text": text, "factions": factions, "location": location}
        self.events.append(ev)
        return ev

    def found(self, world, now, ctx, rng, *, memory=None):
        self.founded.append(ctx)
        return [{"kind": ctx["type"], "text": "founded", "new_faction": "X"}]


class SimTestCase(unittest.TestCase):
    def setUp(self):
        self.g = g = FakeSim()
        self.saved = {}
        patches = {
            "active_factions": g.active,
            "control_rows": g.control_rows,
            "presence_rows": g.presence,
            "relation_rows": g.relations,
            "set_strength": g.set_strength,
            "refresh_control": g.refresh,
            "set_stance": g.set_stance,
            "set_tension": g.set_tension,
            "set_loyalty": g.set_loyalty,
            "record_event": g.record,
            "_meta": lambda world: dict(g.meta),
            "_set_meta": lambda world, **kw: g.meta.update(kw),
            "_nested_pairs": lambda world: set(g.nested),
            "faction_traits": lambda world, f: list(g.factions.get(f, {}).get("traits", [])),
            "pair_view": lambda world, a, b: View(g.scores.get((a, b), 0.0)),
            "leader_of": lambda world, f: None,
            "_personally_close": lambda world, a, b: (a, b) in g.scores,
            "_tick_recruit": lambda world, now, control, memory=None: [],
            "_tick_scale_and_dissolve": lambda world, now, memory=None: [],
            "_found": g.found,
            "_rows": lambda world, q, **kw: [{"l": loc, "c": g.controlled_by.get(loc)} for loc in g.locations]
            if "RETURN l.name AS l, l.controlled_by AS c" in q else [],
        }
        for name, fn in patches.items():
            self.saved[name] = getattr(fs, name)
            setattr(fs, name, fn)
        self.world = object()

    def tearDown(self):
        for name, fn in self.saved.items():
            setattr(fs, name, fn)

    def tick(self, n=1, start=1):
        out = []
        for i in range(n):
            out += fs.tick_once(self.world, fs.tick_period() * (start + i), start + i, rng=random.Random(0))
        return out


class TestTerritory(SimTestCase):
    def test_presence_strengthens_and_absence_decays(self):
        self.tick()
        self.assertAlmostEqual(self.g.control[("Axemen", "Forest")], 6.0 + (0.9 + 0.6) * fs.CLAIM_RATE)
        self.assertAlmostEqual(self.g.control[("BerryCircle", "BerryGrove")], 6.0 + 0.8 * fs.CLAIM_RATE)
        self.assertAlmostEqual(self.g.control[("EmberCamp", "Campfire")], 6.0 - fs.DECAY)

    def test_rivals_wear_each_other_down_and_control_changes(self):
        self.g.where["Reed"] = "Campfire"  # 伐木帮的人守在营火边
        self.g.control.pop(("EmberCamp", "Campfire"))
        self.tick(3)
        self.assertGreater(self.g.control.get(("Axemen", "Campfire"), 0), 1.0)
        self.assertNotIn(("BerryCircle", "Campfire"), {k for k, v in self.g.control.items() if v >= 1.0})
        self.assertEqual(self.g.controlled_by["Campfire"], "Axemen")
        self.assertTrue(any(e["kind"] == "territory" and "Campfire" in e["text"] for e in self.g.events))

    def test_expansionist_trait_claims_faster(self):
        self.g.factions["Axemen"]["traits"] = [{"id": "expansionist", "why": "x"}]
        self.tick()
        self.assertAlmostEqual(self.g.control[("Axemen", "Forest")], 6.0 + 1.5 * fs.CLAIM_RATE * 1.5)


class TestTension(SimTestCase):
    def test_shared_ground_escalates_rivals_to_hostile(self):
        self.tick(6)
        rel = self.g.rels[frozenset(("Axemen", "BerryCircle"))]
        self.assertEqual(rel["stance"], "hostile")
        self.assertTrue(any(e["kind"] == "stance" for e in self.g.events))

    def test_pacifist_slows_escalation(self):
        self.g.factions["BerryCircle"]["traits"] = [{"id": "pacifist", "why": "x"}]
        self.tick(6)
        self.assertEqual(self.g.rels[frozenset(("Axemen", "BerryCircle"))]["stance"], "rival")

    def test_parent_and_child_never_build_tension(self):
        self.tick(5)
        self.assertNotIn(frozenset(("Axemen", "EmberCamp")), self.g.rels)


class TestLoyalty(SimTestCase):
    def test_home_ground_vs_away(self):
        self.tick()
        self.assertAlmostEqual(self.g.members[("Rowan", "Axemen")]["loyalty"], 9.15)
        # Rowan 在森林，不在 EmberCamp 的地盘上
        self.assertAlmostEqual(self.g.members[("Rowan", "EmberCamp")]["loyalty"], 6.9)


class TestEmergence(SimTestCase):
    def test_disloyal_member_splinters_with_like_minded(self):
        self.g.members[("Reed", "Axemen")]["loyalty"] = 2.0
        self.tick()
        self.assertEqual(len(self.g.founded), 1)
        ctx = self.g.founded[0]
        self.assertEqual((ctx["type"], ctx["parent"], ctx["founders"][0]), ("splinter", "Axemen", "Reed"))
        self.assertIn("emerge", " ".join(self.g.meta.keys()))

    def test_cooldown_blocks_back_to_back_emergence(self):
        self.g.members[("Reed", "Axemen")]["loyalty"] = 2.0
        self.tick(2)
        self.assertEqual(len(self.g.founded), 1)

    def test_coalition_across_sides(self):
        self.g.where["Sage"] = "Forest"
        self.g.members[("Reed", "Axemen")]["loyalty"] = 4.0
        self.g.members[("Sage", "BerryCircle")]["loyalty"] = 4.5
        self.g.scores[("Reed", "Sage")] = 2.0
        self.g.scores[("Sage", "Reed")] = 2.0
        self.tick()
        self.assertEqual(self.g.founded[0]["type"], "coalition")
        self.assertEqual(sorted(self.g.founded[0]["founders"]), ["Reed", "Sage"])

    def test_arrival_needs_free_ground(self):
        os.environ["FACTION_ARRIVAL_CHANCE"] = "1.0"
        try:
            self.tick()
            self.assertEqual(self.g.founded[0]["type"], "arrival")
            self.assertEqual(self.g.founded[0]["location"], "River")
        finally:
            os.environ.pop("FACTION_ARRIVAL_CHANCE", None)

    def test_fallback_story_ties_traits_to_story(self):
        fs._taken = lambda world, n: False
        try:
            story = fs.fallback_story(self.world, {"type": "splinter", "parent": "Axemen", "parent_kind": "gang",
                                                   "founders": ["Reed"], "location": "Forest"}, random.Random(1))
        finally:
            fs._taken = self.saved.get("_taken", fs._taken)
        self.assertEqual(story["name"], "FreeAxemen")
        self.assertEqual(story["kind"], "gang")
        self.assertTrue(all(t["id"] in fs.TRAITS and t["why"] for t in story["traits"]))
        self.assertIn("Reed", story["traits"][0]["why"])
        self.assertEqual(story["relations"][0], {"with": "Axemen", "stance": "rival", "reason": "Reed broke away in anger"})


class TestClock(SimTestCase):
    def test_first_call_only_arms_the_clock(self):
        self.assertEqual(fs.maybe_tick(self.world, 100.0), {})
        self.assertEqual(self.g.meta["faction_tick_t"], 100.0)

    def test_catches_up_but_caps(self):
        fs.maybe_tick(self.world, 0.0)
        out = fs.maybe_tick(self.world, fs.tick_period() * 10 + 1)
        self.assertEqual(out["ticks"], fs.MAX_TICKS_PER_CALL)
        self.assertAlmostEqual(self.g.meta["faction_tick_t"], fs.tick_period() * 10)
        self.assertEqual(fs.maybe_tick(self.world, fs.tick_period() * 10 + 2), {})

    def test_disabled_by_env(self):
        os.environ["FACTION_SIM"] = "0"
        try:
            self.assertEqual(fs.maybe_tick(self.world, 1e6), {})
        finally:
            os.environ.pop("FACTION_SIM", None)


class TestTraits(unittest.TestCase):
    def test_trait_math(self):
        saved = fs.faction_traits
        fs.faction_traits = lambda world, f: [{"id": "vengeful", "why": ""}, {"id": "militant", "why": ""}]
        try:
            self.assertAlmostEqual(fs.trait_mult(None, "X", "escalate"), 1.5 * 1.25)
            self.assertEqual(fs.trait_mult(None, "X", "claim"), 1.0)
            self.assertEqual(fs.trait_sum(None, "X", "join"), 0.0)
        finally:
            fs.faction_traits = saved


if __name__ == "__main__":
    unittest.main()
