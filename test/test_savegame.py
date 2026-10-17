"""存档：模拟进度 / NPC 记忆 / 存档文件能原样存下并读回（图的部分要连 Neo4j，这里用假的世界）。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class Who:
    def __init__(self, name):
        self.name = name


def _loop(*names):
    from game_loop import AgentState, GameLoop
    from storyteller import Storyteller

    loop = GameLoop(MagicMock(), verbose=False)
    loop.storyteller = Storyteller(interval=60.0, use_llm=False)
    loop.agents = [AgentState(agent=Who(n)) for n in names]
    return loop


class FakeMemory:
    def __init__(self):
        self.store = {}

    def list_npcs(self):
        return list(self.store)

    def list(self, npc):
        return [{"id": i, "text": t, "metadata": m} for i, (t, m) in self.store[npc].items()]

    def clear_all(self):
        self.store = {}

    def add(self, npc, text, *, mem_id=None, **meta):
        self.store.setdefault(npc, {})[mem_id] = (text, meta)
        return mem_id


def test_a_running_simulation_comes_back_exactly(tmp_path, monkeypatch):
    import savegame
    from game_loop import PendingAction
    from registry import act
    from triggers import Trigger

    monkeypatch.setattr(savegame, "SAVE_DIR", tmp_path)
    loop = _loop("Ash", "Sage")
    loop.sim_time, loop.tick_count = 123.5, 400
    loop.chronicle = [{"t": 100, "clock": "Day 1 08:00", "text": "A trader arrives."}]
    loop.conflicts = [{"loser": "Sage", "culprit": "Ash", "item": "Wood1", "t": 110}]
    loop._touched = {"Wood1": ("Ash", "pick_up", 110.0)}
    loop.storyteller.arc, loop.storyteller.last_at = "Wood is scarce.", 90.0
    ash = loop.get_agent_state("Ash")
    ash.current_plan = [(act("move"), ["Ash", "Shelter", "Forest"]), (act("pick_up"), ["Ash", "Wood1", "Forest"])]
    ash.plan_index, ash.status = 1, "acting"
    ash.last_goal = {"goal": "Collect item", "target_entity": "Wood1", "reason": "The fire is dying."}
    ash.pending_action = PendingAction(act("pick_up"), ["Ash", "Wood1", "Forest"], 120.0, 4.0, 124.0,
                                       path=[(1, 2), (2, 2)], walk_seconds=1.0)
    ash.pending_triggers = [Trigger("event", "news", "A trader arrives.", 0.5, 100.0, {"with": "Sage"})]
    ash.history = [{"t": 120, "kind": "start", "text": "pick_up(Ash, Wood1, Forest)", "why": "The fire is dying."}]
    memory = FakeMemory()
    memory.add("Sage", "Ash took my wood.", mem_id="m1", type="conflict")

    meta = savegame.save_game("slot1", MagicMock(run_query=lambda *a, **k: []), loop, memory)
    assert meta["slot"] == "slot1" and meta["clock"]

    fresh = _loop("Ash", "Sage")
    body = savegame.read_save("slot1")
    savegame.restore_loop(fresh, body["sim"])
    memory2 = FakeMemory()
    savegame.restore_memory(memory2, body["memory"])

    back = fresh.get_agent_state("Ash")
    assert (fresh.sim_time, fresh.tick_count) == (123.5, 400)
    assert fresh.chronicle == loop.chronicle and fresh.conflicts == loop.conflicts
    assert fresh._touched == {"Wood1": ("Ash", "pick_up", 110.0)}
    assert fresh.storyteller.arc == "Wood is scarce." and fresh.storyteller.last_at == 90.0
    assert back.current_plan == ash.current_plan and back.plan_index == 1 and back.status == "acting"
    assert back.pending_action == ash.pending_action, "mid-walk: same route, same timing"
    assert back.pending_triggers[0].data == {"with": "Sage"} and back.pending_triggers[0].kind == "news"
    assert back.last_goal == ash.last_goal and back.history == ash.history
    assert memory2.store == {"Sage": {"m1": ("Ash took my wood.", {"type": "conflict"})}}


def test_saves_are_listed_newest_first_and_names_are_checked(tmp_path, monkeypatch):
    import savegame

    monkeypatch.setattr(savegame, "SAVE_DIR", tmp_path)
    world = MagicMock(run_query=lambda *a, **k: [])
    savegame.save_game("first", world, None)
    savegame.save_game("second", world, _loop("Ash"))
    listed = savegame.list_saves()
    assert {s["slot"] for s in listed} == {"first", "second"}
    assert all(s.get("saved_at") for s in listed)
    with pytest.raises(ValueError):
        savegame.save_game("../evil", world, None)
    with pytest.raises(FileNotFoundError):
        savegame.read_save("missing")


def test_a_restart_continues_the_save_the_database_came_from(tmp_path, monkeypatch):
    import savegame

    monkeypatch.setattr(savegame, "SAVE_DIR", tmp_path)

    class World:
        token = ""

        def run_query(self, q, **k):
            if "MERGE (s:SaveStamp" in q:
                World.token = k["t"]
            if "MATCH (s:SaveStamp" in q:
                return [{"t": World.token}] if World.token else []
            return []

    world = World()
    assert savegame.matching_save(world) is None, "nothing saved yet"
    savegame.save_game("slot1", world, _loop("Ash"))
    savegame.save_game("autosave", world, _loop("Ash", "Sage"))
    body = savegame.matching_save(world)
    assert body["meta"]["slot"] == "autosave", "the database was last stamped by the autosave"
    assert [a["name"] for a in body["sim"]["agents"]] == ["Ash", "Sage"]
    World.token = "some-other-world"
    assert savegame.matching_save(world) is None, "a different world (e.g. a template was loaded): start fresh"
