"""世界临时交互编译（无需 Neo4j）。"""

from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
for sub in ("actions", "director"):
    p = os.path.join(_ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)


class TestWorldInteractionsCompile(unittest.TestCase):
    def test_compile_key_and_door_blueprint(self):
        from director import load_world
        from world_interactions import (
            compile_interactions_from_blueprint,
            destination_from_interaction,
            set_interactions,
        )

        bp = load_world("key_and_door")
        items = compile_interactions_from_blueprint(bp)
        self.assertEqual(len(items), 1)
        inter = items[0]
        self.assertEqual(inter.tool, "DoorKey")
        self.assertEqual(inter.target, "VaultDoor")
        self.assertTrue(any(op.get("op") == "link" for op in inter.ops))
        self.assertEqual(
            destination_from_interaction(inter, from_loc="Hall"),
            "Vault",
        )
        self.assertTrue(inter.consumes_tool)
        set_interactions(items)

    def test_accepts_from_required(self):
        from blueprint import ItemSpec, EventBlueprint, LocationSpec
        from world_interactions import compile_interactions_from_blueprint

        bp = EventBlueprint(
            title="t",
            premise="p",
            locations=[LocationSpec(name="Hall")],
            items=[
                ItemSpec(
                    name="KeyA",
                    effects_on=[{"target": "DoorA", "ops": [{"op": "set_prop", "entity": "DoorA", "label": "Item", "prop": "locked", "value": False}]}],
                ),
                ItemSpec(name="DoorA", item_kind="door", locked=True, accepts_from=[]),
            ],
        ).normalized()
        self.assertEqual(compile_interactions_from_blueprint(bp), [])

        bp2 = EventBlueprint(
            title="t",
            premise="p",
            locations=[LocationSpec(name="Hall")],
            items=[
                ItemSpec(
                    name="KeyA",
                    effects_on=[{"target": "DoorA", "ops": []}],
                ),
                ItemSpec(name="DoorA", item_kind="door", locked=True, accepts_from=["KeyA"]),
            ],
        ).normalized()
        self.assertEqual(len(compile_interactions_from_blueprint(bp2)), 1)

    def test_consume_flag_adds_delete(self):
        from world_interactions import normalize_effect

        e = normalize_effect({
            "target": "DoorA",
            "consume": True,
            "ops": [{"op": "set_prop", "entity": "DoorA", "label": "Item", "prop": "locked", "value": False}],
        })
        self.assertTrue(any(o.get("op") == "delete_entity" for o in e["ops"]))

        e2 = normalize_effect({
            "target": "DoorA",
            "consume": False,
            "ops": [
                {"op": "set_prop", "entity": "DoorA", "label": "Item", "prop": "locked", "value": False},
                {"op": "delete_entity", "entity": "@tool", "label": "Item"},
            ],
        })
        self.assertFalse(any(o.get("op") == "delete_entity" for o in e2["ops"]))



if __name__ == "__main__":
    unittest.main()
