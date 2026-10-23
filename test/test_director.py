"""
导演系统单元测试 — 蓝图规范化 + 模板世界（不依赖 LLM）。

可选：连接 Neo4j 时跑物化构建（无库则跳过）。
"""

from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_UTIL = os.path.join(_ROOT, "utility")
_NEO4J = os.path.join(_ROOT, "neo4j")
for p in (_UTIL, _NEO4J):
    if p not in sys.path:
        sys.path.insert(0, p)

from director import (  # noqa: E402
    EventBlueprint,
    ItemSpec,
    LocationSpec,
    PersonSpec,
    QuestSpec,
    build_from_blueprint,
    default_world_id,
    list_worlds,
    load_world,
    plan_event,
)


class TestDirectorBlueprint(unittest.TestCase):
    def test_normalize_fills_adjacency_chain(self):
        bp = EventBlueprint(
            title="T",
            premise="P",
            locations=[
                LocationSpec("A", "a"),
                LocationSpec("B", "b"),
                LocationSpec("C", "c"),
            ],
            persons=[PersonSpec("Hero", located_at="MissingPlace", role="player")],
            items=[ItemSpec("Gem", located_at=None, held_by=None)],
            quests=[QuestSpec("Q1", involves=["Gem", "Hero"])],
            source="test",
        ).normalized()
        self.assertEqual(bp.persons[0].located_at, "A")
        self.assertTrue(bp.adjacencies)
        self.assertEqual(bp.items[0].located_at, "A")
        self.assertIn("Hero", bp.persons[0].name)

    def test_from_dict_roundtrip(self):
        raw = {
            "title": "Demo",
            "premise": "test premise",
            "locations": [{"name": "Town Square", "description": "hub"}],
            "persons": [{"name": "Traveler", "located_at": "Town Square", "role": "player"}],
            "items": [{"name": "Key", "value": 10, "located_at": "Town Square"}],
            "quests": [{"name": "Find Key", "involves": ["Key"]}],
            "adjacencies": [],
            "social": [],
        }
        bp = EventBlueprint.from_dict(raw)
        self.assertEqual(bp.locations[0].name, "Town_Square")
        d = bp.to_dict()
        self.assertEqual(d["counts"]["locations"], 1)
        self.assertEqual(d["counts"]["persons"], 1)

    def test_locked_adjacency_roundtrip(self):
        bp = EventBlueprint.from_dict(
            {
                "title": "Lock",
                "premise": "need key",
                "locations": [
                    {"name": "Hall", "description": "hub"},
                    {"name": "Vault", "description": "goal"},
                ],
                "persons": [
                    {"name": "Traveler", "located_at": "Hall", "role": "player"},
                ],
                "items": [
                    {"name": "DoorKey", "located_at": "Hall", "quest_flag": "main_macguffin"},
                ],
                "adjacencies": [
                    {"a": "Hall", "b": "Vault", "locked": True, "key": "DoorKey"},
                ],
                "quests": [{"name": "Open", "involves": ["DoorKey"]}],
            }
        )
        locked = [e for e in bp.adjacencies if e.locked]
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0].key, "DoorKey")
        d = bp.to_dict()
        self.assertTrue(any(e.get("locked") for e in d["adjacencies"]))

    def test_dangling_involves_becomes_item(self):
        """quest.involves 引用不存在的实体时，规范化应物化为 Item。"""
        bp = EventBlueprint.from_dict(
            {
                "title": "Ledger Theft",
                "premise": "Someone stole the town ledger",
                "locations": [
                    {"name": "TownSquare", "description": "hub"},
                    {"name": "Market", "description": "shops"},
                ],
                "persons": [
                    {"name": "Traveler", "located_at": "TownSquare", "role": "player"},
                    {"name": "Merchant", "located_at": "Market", "role": "antagonist"},
                    {"name": "Elder", "located_at": "TownSquare", "role": "npc"},
                ],
                "items": [],
                "quests": [
                    {
                        "name": "RecoverLedger",
                        "description": "Recover the stolen ledger",
                        "involves": ["StolenLedger", "Merchant", "Elder"],
                        "objective": "Return StolenLedger to Elder",
                    }
                ],
            }
        )
        names = {i.name for i in bp.items}
        self.assertIn("StolenLedger", names)
        ledger = next(i for i in bp.items if i.name == "StolenLedger")
        self.assertTrue(ledger.held_by or ledger.located_at)
        self.assertTrue(any("StolenLedger" in (q.involves or []) for q in bp.quests))

    def test_quest_without_items_derives_macguffin(self):
        """有任务却零物品时，应从任务标识推导主线道具。"""
        bp = EventBlueprint.from_dict(
            {
                "title": "Relic Hunt",
                "premise": "A relic vanished from the shrine",
                "locations": [{"name": "TownSquare", "description": "hub"}],
                "persons": [
                    {"name": "Traveler", "located_at": "TownSquare", "role": "player"},
                    {"name": "Priest", "located_at": "TownSquare", "role": "npc"},
                ],
                "items": [],
                "quests": [
                    {
                        "name": "FindMoonRelic",
                        "description": "Find what was taken",
                        "involves": ["Priest"],
                        "objective": "Recover MoonRelic",
                    }
                ],
            }
        )
        self.assertTrue(bp.items, "quests require at least one item after repair")
        mac = [i for i in bp.items if (i.quest_flag or "") == "main_macguffin"]
        self.assertTrue(mac)
        self.assertTrue(any(mac[0].name in (q.involves or []) for q in bp.quests))


class TestWorldTemplates(unittest.TestCase):
    def test_list_and_load_key_and_door(self):
        worlds = list_worlds()
        ids = [w["id"] for w in worlds]
        self.assertIn("key_and_door", ids)
        self.assertEqual(default_world_id(), "key_and_door")
        bp = load_world("key_and_door")
        self.assertEqual(bp.source, "world")
        self.assertTrue(any(p.role == "player" for p in bp.persons))
        names = {i.name for i in bp.items}
        self.assertIn("DoorKey", names)
        self.assertIn("VaultDoor", names)
        door = next(i for i in bp.items if i.name == "VaultDoor")
        self.assertEqual(door.item_kind, "door")
        self.assertTrue(door.locked)
        self.assertIn("DoorKey", door.accepts_from)
        key = next(i for i in bp.items if i.name == "DoorKey")
        self.assertTrue(key.effects_on)
        self.assertEqual(key.effects_on[0].get("target"), "VaultDoor")
        self.assertFalse(any(e.locked for e in bp.adjacencies))

    def test_list_and_load_sealed_archive(self):
        import os
        import sys

        actions_path = os.path.join(
            os.path.dirname(__file__), "..", "actions"
        )
        actions_path = os.path.abspath(actions_path)
        if actions_path not in sys.path:
            sys.path.insert(0, actions_path)

        worlds = list_worlds()
        ids = [w["id"] for w in worlds]
        self.assertIn("sealed_archive", ids)
        bp = load_world("sealed_archive")
        self.assertGreaterEqual(len(bp.locations), 6)
        self.assertGreaterEqual(len(bp.persons), 4)
        self.assertTrue(any(p.role == "player" for p in bp.persons))
        names = {i.name for i in bp.items}
        for n in ("Crowbar", "Trapdoor", "RustyKey", "ArchiveDoor", "HealingSalve", "BrassSeal", "BlankWarrant"):
            self.assertIn(n, names)
        from world_interactions import compile_interactions_from_blueprint

        inters = compile_interactions_from_blueprint(bp)
        keys = {f"{x.tool}|{x.target}" for x in inters}
        self.assertIn("Crowbar|Trapdoor", keys)
        self.assertIn("RustyKey|ArchiveDoor", keys)
        self.assertIn("HealingSalve|ScholarElise", keys)
        self.assertIn("BrassSeal|BlankWarrant", keys)
        self.assertIn("BlankWarrant|GuardRenn", keys)
        crowbar = next(i for i in inters if i.tool == "Crowbar")
        self.assertFalse(crowbar.consumes_tool)
        rusty = next(i for i in inters if i.tool == "RustyKey")
        self.assertTrue(rusty.consumes_tool)

    def test_list_and_load_sick_girlfriend(self):
        import os
        import sys

        actions_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "actions"))
        if actions_path not in sys.path:
            sys.path.insert(0, actions_path)

        worlds = list_worlds()
        self.assertIn("sick_girlfriend", [w["id"] for w in worlds])
        bp = load_world("sick_girlfriend")
        self.assertTrue(any(p.name == "Lila" and p.hp < 50 for p in bp.persons))
        self.assertTrue(any(p.name == "DoctorRowan" for p in bp.persons))
        names = {i.name for i in bp.items}
        self.assertEqual(names & {"BlueHerb", "RedHerb", "YellowHerb"}, names)
        # 信息驱动：蓝草疗法事实只在医生侧
        blue = next(f for f in bp.facts if f.name == "BlueHerbCure")
        self.assertIn("DoctorRowan", blue.known_by)
        self.assertNotIn("Traveler", blue.known_by)
        from world_interactions import compile_interactions_from_blueprint

        keys = {f"{x.tool}|{x.target}" for x in compile_interactions_from_blueprint(bp)}
        self.assertIn("BlueHerb|Lila", keys)

    def test_plan_event_world_id(self):
        bp = plan_event("ignored", force_mock=True, world_id="key_and_door")
        self.assertEqual(bp.source, "world")
        self.assertTrue(any(p.name == "Traveler" for p in bp.persons))

    def test_mock_plan_falls_back_to_world(self):
        bp = plan_event("fetch key and open door", force_mock=True)
        self.assertTrue(bp.locations)
        self.assertTrue(any(p.role == "player" for p in bp.persons))
        self.assertTrue(bp.quests)

    def test_unknown_world(self):
        with self.assertRaises(ValueError):
            plan_event("x", world_id="no_such_world")


class TestPlayabilityAndMechanics(unittest.TestCase):
    def test_ensure_playable_mechanics_wires_key_door(self):
        from director import ensure_playable_mechanics, validate_blueprint
        from director.blueprint import AdjacencySpec

        bp = EventBlueprint(
            title="KeyDoor",
            premise="Open the vault",
            locations=[
                LocationSpec("Hall", "hub", "hub"),
                LocationSpec("Storage", "items", "item"),
                LocationSpec("Vault", "goal", "goal,secret"),
            ],
            adjacencies=[AdjacencySpec("Hall", "Storage")],
            persons=[PersonSpec("Traveler", located_at="Hall", role="player")],
            items=[
                ItemSpec(
                    name="DoorKey",
                    located_at="Storage",
                    quest_flag="main_macguffin",
                    item_kind="key",
                    portable=True,
                ),
                ItemSpec(
                    name="VaultDoor",
                    located_at="Hall",
                    item_kind="door",
                    locked=True,
                    portable=False,
                    connects_to="Vault",
                    accepts_from=["DoorKey"],
                ),
            ],
            quests=[
                QuestSpec(
                    "OpenTheVault",
                    involves=["DoorKey", "VaultDoor", "Traveler", "Vault"],
                    objective="Use DoorKey on VaultDoor",
                )
            ],
            source="test",
        )
        fixed = ensure_playable_mechanics(bp)
        key = next(i for i in fixed.items if i.name == "DoorKey")
        door = next(i for i in fixed.items if i.name == "VaultDoor")
        self.assertIn("DoorKey", door.accepts_from)
        self.assertTrue(key.effects_on)
        self.assertEqual(key.effects_on[0].get("target"), "VaultDoor")
        ops = key.effects_on[0].get("ops") or []
        self.assertTrue(any(o.get("op") == "set_prop" for o in ops))
        self.assertTrue(any(o.get("op") == "link" for o in ops))
        issues = validate_blueprint(fixed)
        self.assertNotIn("door_without_interaction", issues)
        self.assertNotIn("unplayable_interactions", issues)

    def test_skeleton_fallback_world_key_door(self):
        from director.planner import _fallback_world_from_skeleton, _normalize_skeleton

        sk = _normalize_skeleton(
            {
                "title": "Vault Job",
                "premise": "Get key open vault",
                "player_name": "Traveler",
                "antagonist_name": "",
                "npc_names": [],
                "quest": {
                    "name": "OpenVault",
                    "objective": "Unlock VaultDoor with DoorKey",
                    "macguffin": "DoorKey",
                },
                "puzzle_kind": "key_door",
                "key_name": "DoorKey",
                "door_name": "VaultDoor",
                "goal_location": "Vault",
                "start_location": "Hall",
            },
            "Get key open vault",
        )
        self.assertIsNotNone(sk)
        assert sk is not None
        from director.maps import load_map
        from director.planner import _fit_skeleton_to_map

        map_data = load_map("key_and_door")
        sk = _fit_skeleton_to_map(sk, map_data)
        raw = _fallback_world_from_skeleton(sk, map_data)
        self.assertEqual({x["name"] for x in raw["locations"]}, {"Hall", "Storage", "Vault"})
        bp = EventBlueprint.from_dict(raw, source="test")
        from director import ensure_playable_mechanics, is_minimally_playable

        bp = ensure_playable_mechanics(bp)
        ok, issues = is_minimally_playable(bp)
        self.assertTrue(ok, issues)
        names = {i.name for i in bp.items}
        self.assertIn("DoorKey", names)
        self.assertIn("VaultDoor", names)
        key = next(i for i in bp.items if i.name == "DoorKey")
        self.assertTrue(key.effects_on)

    def test_normalize_skeleton_defaults_key_door(self):
        from director.planner import _normalize_skeleton

        sk = _normalize_skeleton(
            {
                "title": "T",
                "premise": "locked vault",
                "player_name": "Hero",
                "quest": {"name": "Q", "objective": "open vault", "macguffin": ""},
                "puzzle_kind": "key_door",
            },
            "locked vault",
        )
        self.assertEqual(sk["player_name"], "Hero")
        self.assertEqual(sk["key_name"], "DoorKey")
        self.assertEqual(sk["door_name"], "VaultDoor")
        self.assertEqual(sk["goal_location"], "Vault")

    def test_normalize_skeleton_builds_cast_and_informant(self):
        from director.planner import _normalize_skeleton

        sk = _normalize_skeleton(
            {
                "title": "Gossip",
                "premise": "Ask the innkeeper about the missing ledger",
                "player_name": "Traveler",
                "cast": [
                    {"name": "Traveler", "role": "player", "function": "hero"},
                    {
                        "name": "InnkeeperMara",
                        "role": "npc",
                        "function": "Knows ledger rumor",
                    },
                ],
                "quest": {
                    "name": "FindLedger",
                    "objective": "Learn where StolenLedger is",
                    "macguffin": "StolenLedger",
                },
                "puzzle_kind": "talk_info",
                "start_location": "Tavern",
            },
            "Ask innkeeper",
        )
        names = {c["name"] for c in sk["cast"]}
        self.assertIn("Traveler", names)
        self.assertIn("InnkeeperMara", names)
        self.assertEqual(sk["npc_names"], ["InnkeeperMara"])

        sk2 = _normalize_skeleton(
            {
                "title": "Gossip2",
                "premise": "Need a lead",
                "player_name": "Traveler",
                "quest": {"name": "Q", "objective": "Find a lead", "macguffin": "Clue"},
                "puzzle_kind": "talk_info",
            },
            "Need a lead",
        )
        self.assertTrue(any(c["role"] == "npc" for c in sk2["cast"]))

    def test_ensure_cast_quality_fills_profiles_and_social(self):
        from director import ensure_cast_quality
        from director.blueprint import AdjacencySpec

        bp = EventBlueprint(
            title="Cast",
            premise="Ask around",
            locations=[
                LocationSpec("Tavern", "hub", "social"),
                LocationSpec("Square", "outdoors", "hub"),
            ],
            adjacencies=[AdjacencySpec("Tavern", "Square")],
            persons=[
                PersonSpec("Traveler", located_at="Square", role="player"),
                PersonSpec("InnkeeperMara", located_at="Tavern", role="npc"),
            ],
            items=[ItemSpec("StolenLedger", located_at="Tavern", quest_flag="main_macguffin")],
            quests=[
                QuestSpec(
                    "FindLedger",
                    involves=["StolenLedger", "InnkeeperMara", "Traveler"],
                    objective="Learn where StolenLedger is",
                )
            ],
            source="test",
        )
        fixed = ensure_cast_quality(bp, puzzle_kind="talk_info")
        mara = next(p for p in fixed.persons if p.name == "InnkeeperMara")
        self.assertTrue(mara.personality)
        self.assertTrue(mara.background)
        self.assertTrue(mara.speech_style)
        self.assertTrue(mara.secret_policy)
        self.assertTrue(
            any(
                (a == "Traveler" and b == "InnkeeperMara")
                or (a == "InnkeeperMara" and b == "Traveler")
                for a, _r, b in fixed.social
            )
        )
        self.assertTrue(fixed.facts)
        self.assertTrue(
            any(
                "Traveler" not in (f.known_by or []) and "InnkeeperMara" in (f.known_by or [])
                for f in fixed.facts
            )
        )

    def test_key_and_door_is_solvable(self):
        from director import check_solvable, ensure_playable_mechanics, load_world

        bp = ensure_playable_mechanics(load_world("key_and_door"))
        res = check_solvable(bp, puzzle_kind="key_door")
        self.assertTrue(res.ok, res.reason)
        self.assertTrue(any(s.startswith("pickup:") for s in res.plan_sketch))
        self.assertTrue(any(s.startswith("use_on:") for s in res.plan_sketch))
        self.assertTrue(any("Vault" in s for s in res.plan_sketch))

    def test_broken_door_without_key_unsolvable(self):
        from director import check_solvable, ensure_playable_mechanics

        bp = EventBlueprint(
            title="Broken",
            premise="Door but key elsewhere unreachable",
            locations=[
                LocationSpec("Hall", "hub", "hub"),
                LocationSpec("Vault", "goal", "goal,secret"),
                LocationSpec("Island", "isolated", "item"),
            ],
            adjacencies=[],  # Island 与 Hall 断开，钥匙不可达
            persons=[PersonSpec("Traveler", located_at="Hall", role="player")],
            items=[
                ItemSpec(
                    name="DoorKey",
                    located_at="Island",
                    item_kind="key",
                    quest_flag="main_macguffin",
                    portable=True,
                    effects_on=[
                        {
                            "target": "VaultDoor",
                            "consume": True,
                            "ops": [
                                {
                                    "op": "set_prop",
                                    "entity": "VaultDoor",
                                    "label": "Item",
                                    "prop": "locked",
                                    "value": False,
                                },
                                {
                                    "op": "link",
                                    "a": "Hall",
                                    "a_label": "Location",
                                    "b": "Vault",
                                    "b_label": "Location",
                                    "rel": "ADJACENT_TO",
                                    "bidirectional": True,
                                },
                            ],
                        }
                    ],
                ),
                ItemSpec(
                    name="VaultDoor",
                    located_at="Hall",
                    item_kind="door",
                    locked=True,
                    portable=False,
                    connects_to="Vault",
                    accepts_from=["DoorKey"],
                ),
            ],
            quests=[QuestSpec("Open", involves=["DoorKey", "VaultDoor", "Vault"])],
            source="test",
        )
        bp = ensure_playable_mechanics(bp)
        res = check_solvable(bp, puzzle_kind="key_door")
        self.assertFalse(res.ok)
        self.assertIn("key_unreachable", res.reason)


class TestDirectorBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = None
        try:
            from neo4j_world import Neo4jWorld

            Neo4jWorld._instance = None  # type: ignore[attr-defined]
            cls.world = Neo4jWorld.get_instance()
            cls.world.run_query("RETURN 1 AS ok")
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

    def test_build_key_and_door_world(self):
        if self.world is None:
            self.skipTest(f"Neo4j unavailable: {getattr(self, 'skip_reason', '')}")
        bp = load_world("key_and_door")
        result = build_from_blueprint(self.world, bp, clear=True)
        self.assertTrue(result["ok"])
        persons = self.world.list_entities("Person")
        locations = self.world.list_entities("Location")
        self.assertIn("Traveler", persons)
        self.assertIn("Hall", locations)
        self.assertIn("Vault", locations)
        self.assertTrue(
            self.world.has_relationship(
                "DoorKey", "Item", "Storage", "Location", "LOCATED_AT"
            )
        )
        # Hall <-> Storage open; Vault 仅靠门开通
        self.assertTrue(
            self.world.has_relationship("Hall", "Location", "Storage", "Location", "ADJACENT_TO")
        )
        self.assertFalse(
            self.world.has_relationship("Hall", "Location", "Vault", "Location", "ADJACENT_TO")
        )
        rows = self.world.run_query(
            """
            MATCH (i:Item {name: 'VaultDoor'})
            RETURN coalesce(i.item_kind, '') AS kind,
                   coalesce(i.locked, false) AS locked,
                   coalesce(i.accepts_from, '') AS accepts_from
            """
        )
        self.assertTrue(rows)
        self.assertEqual(rows[0]["kind"], "door")
        self.assertTrue(rows[0]["locked"])
        self.assertIn("DoorKey", rows[0]["accepts_from"] or "")
        from world_interactions import list_interactions

        keys = {f"{x.tool}|{x.target}" for x in list_interactions()}
        self.assertIn("DoorKey|VaultDoor", keys)


if __name__ == "__main__":
    unittest.main()


class TestFixedMaps(unittest.TestCase):
    """导演只用手动配置的地图。"""

    def _sk(self, puzzle="key_door", start="Nowhere", goal="Moon"):
        from director.planner import _normalize_skeleton

        return _normalize_skeleton(
            {
                "title": "T", "premise": "p", "player_name": "Traveler",
                "cast": [{"name": "Traveler", "role": "player", "function": "hero"},
                         {"name": "Mira", "role": "npc", "function": "helper"}],
                "quest": {"name": "Q", "objective": "o", "macguffin": "DoorKey"},
                "puzzle_kind": puzzle, "key_name": "DoorKey", "door_name": "VaultDoor",
                "goal_location": goal, "start_location": start,
            },
            "p",
        )

    def test_maps_list_includes_worlds(self):
        from director.maps import list_maps, load_map

        ids = {m["id"] for m in list_maps()}
        self.assertIn("frontier_village", ids)
        m = load_map("frontier_village")
        self.assertEqual(m["source"], "worlds")
        self.assertTrue(all("map" in x for x in m["locations"]))
        with self.assertRaises(ValueError):
            load_map("no_such_map")

    def test_skeleton_snaps_to_map_and_needs_a_dead_end(self):
        from director.maps import load_map
        from director.planner import _fit_skeleton_to_map

        m = load_map("market_town")
        sk = _fit_skeleton_to_map(self._sk(), m)
        names = {x["name"] for x in m["locations"]}
        self.assertIn(sk["start_location"], names)
        # market_town 没有死角 → 门谜题改成 deliver
        if sk["puzzle_kind"] == "key_door":
            self.assertIn(sk["goal_location"], names)
        else:
            self.assertEqual(sk["puzzle_kind"], "deliver")

    def test_fixed_map_never_grows(self):
        from director.maps import load_map
        from director.planner import _apply_fixed_map, _fit_skeleton_to_map

        m = load_map("frontier_village")
        sk = _fit_skeleton_to_map(self._sk(puzzle="deliver"), m)
        world = {
            "locations": [{"name": "InventedCastle"}],
            "adjacencies": [{"a": "InventedCastle", "b": "Village"}],
            "persons": [{"name": "Mira", "located_at": "InventedCastle"}],
            "items": [{"name": "Thing", "located_at": "InventedCastle"}],
        }
        out = _apply_fixed_map(world, m, sk)
        self.assertEqual([x["name"] for x in out["locations"]], [x["name"] for x in m["locations"]])
        self.assertEqual(out["adjacencies"], m["adjacencies"])
        self.assertEqual(out["persons"][0]["located_at"], sk["start_location"])
        self.assertEqual(out["items"][0]["located_at"], sk["start_location"])

    def test_door_replaces_the_edge_into_a_dead_end(self):
        from director.planner import _apply_fixed_map, _fit_skeleton_to_map

        m = {
            "id": "t", "locations": [{"name": "Hub", "tags": "hub"}, {"name": "Yard"}, {"name": "Crypt"}],
            "adjacencies": [{"a": "Hub", "b": "Yard"}, {"a": "Yard", "b": "Crypt"}],
        }
        sk = _fit_skeleton_to_map(self._sk(goal="Crypt"), m)
        self.assertEqual((sk["start_location"], sk["goal_location"]), ("Hub", "Crypt"))
        out = _apply_fixed_map({"persons": [], "items": [{"name": "VaultDoor", "located_at": "Hub"}]}, m, sk)
        self.assertEqual(out["adjacencies"], [{"a": "Hub", "b": "Yard"}])
        door = out["items"][0]
        self.assertEqual((door["located_at"], door["connects_to"]), ("Yard", "Crypt"))
