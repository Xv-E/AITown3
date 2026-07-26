"""钥匙对门 use_on + Use item on 规划（可选 Neo4j）。"""

from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
for sub in ("neo4j", "actions", "agent", "planner", "utility", "web"):
    p = os.path.join(_ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)


class TestUseOnDoor(unittest.TestCase):
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

    def _reload(self):
        from director import build_from_blueprint, load_world

        build_from_blueprint(self.world, load_world("key_and_door"), clear=True)

    def test_door_not_pickable(self):
        if self.world is None:
            self.skipTest(getattr(self, "skip_reason", "no neo4j"))
        from registry import get_registry

        self._reload()
        reg = get_registry()
        ok, reason = reg.execute(
            self.world, "pick_up", c="Traveler", i="VaultDoor", l="Hall"
        )
        self.assertFalse(ok)
        self.assertIn("不可拾取", reason)

    def test_collect_then_use_on_unlocks_vault(self):
        if self.world is None:
            self.skipTest(getattr(self, "skip_reason", "no neo4j"))
        from goal_planner import plan_for_goal
        from registry import get_registry

        self._reload()
        reg = get_registry()

        collect = plan_for_goal(
            self.world,
            "Traveler",
            {"goal": "Collect item", "target_entity": "DoorKey"},
        )
        self.assertTrue(collect.success, collect.error)
        self.assertTrue(any(s[0] == "pick_up" for s in collect.plan), collect.plan)
        for name, args in collect.plan:
            kwargs = {}
            # align with Action.parameters order via registry
            action = reg.get(name)
            self.assertIsNotNone(action)
            for p, v in zip(action.parameters, args):
                kwargs[p.name] = v
            ok, msg = reg.execute(self.world, name, **kwargs)
            self.assertTrue(ok, f"{name} {args}: {msg}")

        unlock = plan_for_goal(
            self.world,
            "Traveler",
            {"goal": "Use item on", "target_entity": "VaultDoor"},
        )
        self.assertTrue(unlock.success, unlock.error)
        self.assertTrue(any(s[0] == "use_on" for s in unlock.plan), unlock.plan)
        for name, args in unlock.plan:
            action = reg.get(name)
            kwargs = {p.name: v for p, v in zip(action.parameters, args)}
            ok, msg = reg.execute(self.world, name, **kwargs)
            self.assertTrue(ok, f"{name} {args}: {msg}")

        self.assertTrue(
            self.world.has_relationship("Hall", "Location", "Vault", "Location", "ADJACENT_TO")
        )
        rows = self.world.run_query(
            "MATCH (i:Item {name: 'VaultDoor'}) RETURN coalesce(i.locked, false) AS locked"
        )
        self.assertFalse(rows[0]["locked"])
        # 钥匙已消耗
        self.assertNotIn("DoorKey", self.world.list_entities("Item"))

    def test_profile_prefers_collect_then_unlock(self):
        if self.world is None:
            self.skipTest(getattr(self, "skip_reason", "no neo4j"))
        from agent_profile import AgentProfile
        from profile_planner import profile_aware_pick_goal

        self._reload()
        agent = AgentProfile.from_world(self.world, "Traveler")
        g1 = profile_aware_pick_goal(self.world, agent)
        self.assertEqual(g1["goal"], "Collect item")
        self.assertEqual(g1["target_entity"], "DoorKey")

        # 模拟已持有钥匙
        from registry import get_registry

        reg = get_registry()
        reg.execute(self.world, "move", c="Traveler", **{"from": "Hall", "to": "Storage"})
        reg.execute(self.world, "pick_up", c="Traveler", i="DoorKey", l="Storage")
        agent.refresh_from_world()
        g2 = profile_aware_pick_goal(self.world, agent)
        self.assertEqual(g2["goal"], "Use item on")
        self.assertEqual(g2["target_entity"], "VaultDoor")


if __name__ == "__main__":
    unittest.main()
