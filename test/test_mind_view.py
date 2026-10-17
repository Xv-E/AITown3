"""NPC 记忆页：按时间列、按类型筛、按意思搜；没有记忆库时也不报错。"""

from __future__ import annotations


class FakeMemory:
    def __init__(self, items):
        self.items = items
        self.searched = None

    def list(self, npc):
        return list(self.items)

    def search(self, npc, q, top_k=5, where=None):
        self.searched = (q, where)
        return [m for m in self.items if "wood" in m["text"]]


def _m(text, kind, ts):
    return {"id": text, "text": text, "metadata": {"type": kind, "timestamp": ts, "location": "Camp"}}


def test_memories_newest_first_filtered_and_searched():
    from mind_view import memories_view

    mem = FakeMemory([
        _m("Ash asked for wood.", "dialogue", "2026-10-17T08:00:00"),
        _m("Picked up Wood1.", "action", "2026-10-17T09:00:00"),
        _m("Learned: the key is in the shed.", "fact", "2026-10-17T07:00:00"),
    ])
    view = memories_view(mem, "Sage")
    assert [m["text"] for m in view["memories"]] == ["Picked up Wood1.", "Ash asked for wood.",
                                                      "Learned: the key is in the shed."]
    assert view["types"] == {"dialogue": 1, "action": 1, "fact": 1} and view["total"] == 3
    assert view["memories"][0]["time"] == "10-17 09:00"

    only = memories_view(mem, "Sage", kind="dialogue")
    assert [m["text"] for m in only["memories"]] == ["Ash asked for wood."]

    found = memories_view(mem, "Sage", query="wood", kind="dialogue")
    assert mem.searched == ("wood", {"type": "dialogue"}) and len(found["memories"]) == 1
    assert memories_view(None, "Sage")["available"] is False


def test_routine_marks_the_current_slot_and_what_is_done():
    import json
    from unittest.mock import MagicMock

    from game_loop import AgentState, GameLoop
    from mind_view import routine_view
    from routine import parse_routine

    raw = [
        {"start": 6, "end": 12, "location": "Forest", "activity": "gather wood", "once": True},
        {"start": 12, "end": 14, "location": "CampfireClearing", "activity": "eat", "social": True},
        {"start": 21, "end": 6, "location": "Shelter", "action": "sleep"},
    ]
    world = MagicMock()
    world.run_query = lambda *a, **k: [{"routine": json.dumps(raw)}]

    class Who:
        name = "Sage"

    loop = GameLoop(MagicMock(), verbose=False)
    state = AgentState(agent=Who())
    loop.agents = [state]
    loop.sim_time = 25.0  # 08:00 on day 1
    state.routine_done = [f"1:{parse_routine(raw)[0].key}"]
    view = routine_view(world, "Sage", loop)
    first, lunch, night = view["slots"]
    assert first == {"hours": "06:00-12:00", "where": "Forest", "what": "gather wood",
                     "notes": "once a day", "now": True, "done": True}
    assert not lunch["now"] and lunch["notes"] == "chats with people around"
    assert night["hours"] == "21:00-06:00" and night["what"] == "sleep"
