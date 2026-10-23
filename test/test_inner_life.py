"""内心 / 关系 / 人情任务：不依赖 Neo4j 的单元测试。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utility"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import inner_life  # noqa: E402
from inner_life import (  # noqa: E402
    InnerLife,
    favor_quest_name,
    inner_life_block,
    is_close,
    parse_favor,
    register_favor,
)


class FakeWorld:
    """按查询里的关键字返回预设结果，并记下写入。"""

    def __init__(self, routes=None):
        self.routes = routes or []
        self.queries = []
        self.entities = []
        self.links = []

    def run_query(self, query, **params):
        self.queries.append((query, params))
        for key, rows in self.routes:
            if key in query:
                return rows(params) if callable(rows) else rows
        return []

    def add_entity(self, name, entity_type="Entity", **props):
        self.entities.append((name, entity_type, props))

    def link_entities(self, a, la, b, lb, rel, **props):
        self.links.append((a, rel, b))


class TestInnerLifeBlock(unittest.TestCase):
    def setUp(self):
        self.inner = InnerLife(
            name="Ash",
            desire="sleep one night without panic",
            fear="the dark",
            secret="burns tomorrow's wood",
            bonds={"Rowan": "owes him their life", "Sage": "closest friend"},
        )

    def test_secret_hidden_from_stranger(self):
        text = inner_life_block(self.inner, listener="Traveler", rels=["KNOWS"], trust=2)
        self.assertIn("keep it hidden", text)
        self.assertIn("the dark", text)
        self.assertIn("Others on your mind", text)

    def test_secret_may_be_confided_to_friend(self):
        text = inner_life_block(self.inner, listener="Sage", rels=["FRIENDS_WITH"])
        self.assertIn("may confide", text)
        self.assertIn("You and Sage: friends with; closest friend", text)
        self.assertNotIn("Sage —", text)

    def test_high_trust_counts_as_close(self):
        self.assertTrue(is_close([], trust=inner_life.CONFIDE_TRUST))
        self.assertFalse(is_close(["KNOWS"], trust=1))

    def test_load_parses_bonds_json(self):
        world = FakeWorld([
            ("p.desire", [{"desire": "d", "fear": "f", "secret": "",
                           "bonds": json.dumps({"Bruno": "old friend"})}]),
        ])
        inner = inner_life.load_inner_life(world, "Tilda")
        self.assertEqual(inner.bonds, {"Bruno": "old friend"})
        self.assertEqual(inner.desire, "d")


class TestFavor(unittest.TestCase):
    def test_parse_favor_requires_reason(self):
        self.assertIsNone(parse_favor({"item": "firewood"}))
        self.assertIsNone(parse_favor({"item": "<item>", "why": "x"}))
        self.assertIsNone(parse_favor("firewood"))
        fav = parse_favor({"item": "firewood", "why": "the dark scares me", "reward": "a hot meal"})
        self.assertEqual(fav["item"], "firewood")
        self.assertEqual(fav["reward"], "a hot meal")

    def test_quest_name(self):
        self.assertEqual(favor_quest_name("Ash", "fire wood"), "FavorAshFireWood")

    def _world(self, *, exists=True, holds=False, open_favors=0):
        return FakeWorld([
            ("GIVES_QUEST]->(q:Quest {kind: 'favor'})", [{"n": "Q"}] * open_favors),
            ("HAS_ITEM", [{"n": "Wood1"}] if holds else []),
            ("MATCH (i:Item) WHERE", [{"n": "Wood1"}] if exists else []),
        ])

    def test_register_creates_quest_with_reason(self):
        world = self._world()
        fav = {"item": "firewood", "why": "the fire is all that keeps the dark away", "reward": "my blanket"}
        name = register_favor(world, "Ash", "Traveler", fav, location="Camp")
        self.assertEqual(name, "FavorAshFirewood")
        (qname, label, props), = world.entities
        self.assertEqual(label, "Quest")
        self.assertEqual(props["kind"], "favor")
        self.assertEqual(props["description"], fav["why"])
        self.assertIn("my blanket", props["objective"])
        self.assertIn(("Ash", "GIVES_QUEST", name), world.links)
        self.assertIn(("Traveler", "ACCEPTED_QUEST", name), world.links)

    def test_register_rejects_bad_requests(self):
        fav = {"item": "firewood", "why": "w", "reward": ""}
        self.assertIsNone(register_favor(self._world(exists=False), "Ash", "Traveler", fav))
        self.assertIsNone(register_favor(self._world(holds=True), "Ash", "Traveler", fav))
        self.assertIsNone(register_favor(self._world(open_favors=1), "Ash", "Traveler", fav))
        self.assertIsNone(register_favor(self._world(), "Ash", "Ash", fav))

    def test_apply_mood_only_short_words(self):
        world = FakeWorld()
        self.assertEqual(inner_life.apply_mood(world, "Ash", "Relieved"), "relieved")
        self.assertIsNone(inner_life.apply_mood(world, "Ash", "{bad}"))


class TestDialogueIntegration(unittest.TestCase):
    def test_reply_payload_keeps_mood_and_favor(self):
        from dialogue_service import _parse_dialogue_payload

        raw = json.dumps({
            "lines": [{"speaker": "Ash", "text": "Please... the fire is low again."}],
            "trust_delta": 1,
            "mood": "anxious",
            "favor": {"item": "firewood", "why": "I can't face another dark night", "reward": "supper"},
        })
        parsed = _parse_dialogue_payload(raw, "Traveler", "Ash")
        self.assertEqual(parsed["mood"], "anxious")
        self.assertEqual(parsed["favor"]["item"], "firewood")

    def test_reply_prompt_carries_heart_and_craft(self):
        from dialogue_service import _build_dialogue_prompt

        class P:
            personality = "anxious"
            background = "firekeeper"
            mood = "worried"

        prompt = _build_dialogue_prompt(
            speaker_a="Rowan", speaker_b="Ash", location="Camp", safe_msg="hey",
            agent_b=P(), style="", clue_block="", mem_hints=[], limits_block="",
            limits={"npc_lines_max": 2, "npc_text": 220},
            heart_block="What you fear: the dark", favor_block="Optional favor: ...",
        )
        self.assertIn('Player (Rowan): "hey"', prompt)  # 对话提示会标出谁是玩家
        self.assertIn("What you fear: the dark", prompt)
        self.assertIn("say WHY it matters", prompt)
        self.assertIn("Optional favor", prompt)

    def test_heart_outcomes_register_favor_and_memory(self):
        import dialogue_service

        world = FakeWorld([
            ("GIVES_QUEST]->(q:Quest {kind: 'favor'})", []),
            ("HAS_ITEM", []),
            ("MATCH (i:Item) WHERE", [{"n": "Wood1"}]),
        ])

        class Mem:
            def __init__(self):
                self.rows = []

            def add(self, who, text, **kw):
                self.rows.append((who, text))

        mem = Mem()
        dialogue = {
            "mood": "hopeful",
            "favor": {"item": "firewood", "why": "the dark", "reward": ""},
        }
        dialogue_service._apply_heart_outcomes(world, mem, dialogue, "Traveler", "Ash", "Camp")
        self.assertEqual(dialogue["quests_created"], ["FavorAshFirewood"])
        self.assertEqual({w for w, _ in mem.rows}, {"Ash", "Traveler"})


class TestBlueprintHearts(unittest.TestCase):
    def test_from_dict_reads_inner_life(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "director"))
        from director.blueprint import EventBlueprint

        bp = EventBlueprint.from_dict({
            "locations": [{"name": "Camp"}],
            "persons": [
                {"name": "Ash", "located_at": "Camp", "desire": "rest", "fear": "dark",
                 "secret": "s", "bonds": [{"with": "Sage", "feeling": "love", "history": "grew up together"}]},
                {"name": "Sage", "located_at": "Camp"},
            ],
        }).normalized()
        ash = next(p for p in bp.persons if p.name == "Ash")
        self.assertEqual(ash.fear, "dark")
        self.assertEqual(ash.bonds, {"Sage": "love; grew up together"})

    def test_template_worlds_bonds_point_at_real_people(self):
        root = os.path.join(os.path.dirname(__file__), "..", "worlds")
        for fn in os.listdir(root):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(root, fn), encoding="utf-8") as f:
                data = json.load(f)
            names = {p["name"] for p in data.get("persons", [])}
            for p in data.get("persons", []):
                for who in (p.get("bonds") or {}):
                    self.assertIn(who, names, f"{fn}: {p['name']} bond -> {who}")


if __name__ == "__main__":
    unittest.main()
