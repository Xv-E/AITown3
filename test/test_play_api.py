"""Play API smoke tests (mock world, no HTTP LLM)."""

from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
for sub in ("neo4j", "actions", "web", "utility"):
    p = os.path.join(_ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)


class TestPlayApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = None
        try:
            from neo4j_world import Neo4jWorld
            from director import build_from_blueprint, load_world

            Neo4jWorld._instance = None  # type: ignore[attr-defined]
            cls.world = Neo4jWorld.get_instance()
            build_from_blueprint(cls.world, load_world("key_and_door"), clear=True)
        except Exception as e:
            cls.skip_reason = str(e)
            cls.world = None

    @classmethod
    def tearDownClass(cls):
        if cls.world is not None:
            try:
                cls.world.close()
            except Exception:
                pass

    def test_play_state_and_move(self):
        if self.world is None:
            self.skipTest(getattr(self, "skip_reason", "no neo4j"))
        from play_api import get_play_state, get_player
        from registry import get_registry

        player = get_player(self.world)
        self.assertIsNotNone(player)
        self.assertTrue(player["is_player"])
        st = get_play_state(self.world)
        self.assertTrue(st["found"])
        self.assertEqual(st["player"]["name"], "Traveler")
        self.assertIn("Storage", st["adjacent"])
        self.assertTrue(any(ex.get("to") == "Vault" for ex in (st.get("locked_exits") or [])))
        self.assertTrue(st["map"]["locations"])
        # Hall：门在可交互区，不在地上物品
        hall_items = [
            (it["name"] if isinstance(it, dict) else it) for it in (st["here"].get("items") or [])
        ]
        hall_ix = [
            (it["name"] if isinstance(it, dict) else it)
            for it in (st["here"].get("interactables") or [])
        ]
        self.assertNotIn("VaultDoor", hall_items)
        self.assertIn("VaultDoor", hall_ix)

        reg = get_registry()
        ok, _ = reg.execute(
            self.world,
            "move",
            c="Traveler",
            **{"from": "Hall", "to": "Storage"},
        )
        self.assertTrue(ok)
        st2 = get_play_state(self.world)
        self.assertEqual(st2["player"]["location"], "Storage")
        item_names = [
            (it["name"] if isinstance(it, dict) else it) for it in (st2["here"]["items"] or [])
        ]
        self.assertIn("DoorKey", item_names)
        self.assertEqual(st2["here"].get("interactables") or [], [])


if __name__ == "__main__":
    unittest.main()
