"""NPC 知识加载与 LLM revealed 校验（不依赖硬编码念白）。"""

from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
for sub in ("neo4j", "actions", "web", "utility", "agent"):
    p = os.path.join(_ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)


class TestNpcKnowledge(unittest.TestCase):
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

    def test_traveler_loads_facts_from_graph(self):
        if self.world is None:
            self.skipTest(getattr(self, "skip_reason", "no neo4j"))
        from npc_knowledge import load_npc_knowledge, knowledge_prompt_block

        kb = load_npc_knowledge(self.world, "Traveler")
        self.assertTrue(kb.facts or kb.rumors, kb)
        block = knowledge_prompt_block(kb)
        blob = block.lower()
        self.assertTrue(
            "storage" in blob or "doorkey" in blob or "key" in blob or "钥匙" in blob,
            blob,
        )

    def test_force_mock_yields_no_npc_lines(self):
        if self.world is None:
            self.skipTest(getattr(self, "skip_reason", "no neo4j"))
        from dialogue_service import generate_talk_dialogue

        # key_and_door 只有玩家；对话目标用自己应失败/无 NPC 台词
        dlg = generate_talk_dialogue(
            self.world,
            "Traveler",
            "Traveler",
            "Hall",
            force_mock=True,
            player_message="Where is the key?",
        )
        self.assertIn(dlg.get("source"), ("error", "mock", "rule"))
        npc_lines = [
            ln for ln in dlg.get("lines") or [] if ln.get("speaker") not in (None, "Traveler", "system")
        ]
        # mock/error 路径不应编造第三方 NPC 台词
        self.assertTrue(dlg.get("source") == "error" or not npc_lines)


if __name__ == "__main__":
    unittest.main()
