"""
Neo4jMemory：NPC 记忆存在世界库的 :Memory 节点里。需要 Neo4j（连不上就跳过）。
只动 test_ 开头的 NPC 的记忆；嵌入用 conftest 里的词袋假模型。
"""

from __future__ import annotations

A, B = "test_alice", "test_bob"


def _set_clock(world, t):
    world.run_query("MERGE (m:WorldMeta {name: 'WorldMeta'}) SET m.sim_time = $t", t=float(t))


def test_add_list_count_keeps_write_order(mem):
    texts = [f"step {i}: walked to the well" for i in range(12)]
    for text in texts:
        mem.add(A, text, type="action")
    assert mem.count(A) == 12
    assert [m["text"] for m in mem.list(A)] == texts
    assert all(m["metadata"]["owner"] == A for m in mem.list(A))


def test_memories_are_stamped_with_game_time(mem, world):
    _set_clock(world, 123.5)
    mem.add(A, "saw the blacksmith at the forge", type="observation")
    _set_clock(world, 200.0)
    mem.add(A, "explicit time wins", type="observation", t=50.0)
    first, second = mem.list(A)
    assert first["metadata"]["t"] == 123.5
    assert second["metadata"]["t"] == 50.0


def test_semantic_search_ranks_closest_first(mem):
    mem.add(A, "Bob gave me a rusty key in the tavern", type="dialogue")
    mem.add(A, "the river is cold this morning", type="observation")
    mem.add(A, "bought bread from the baker", type="action")
    hits = mem.search(A, "who gave me the rusty key", top_k=2)
    assert hits[0]["text"] == "Bob gave me a rusty key in the tavern"
    assert hits[0]["distance"] <= hits[1]["distance"]
    assert "embedding" not in hits[0]["metadata"]


def test_npcs_do_not_see_each_others_memories(mem):
    mem.add(A, "alice secret: the key is under the mat")
    mem.add(B, "bob likes fishing")
    assert [m["text"] for m in mem.search(B, "where is the key", top_k=5)] == ["bob likes fishing"]
    assert mem.count(A) == 1 and mem.count(B) == 1


def test_where_filter(mem):
    mem.add(A, "talked about the harvest", type="dialogue")
    mem.add(A, "talked to nobody, picked apples", type="action")
    hits = mem.search(A, "talked", top_k=5, where={"type": "dialogue"})
    assert [h["text"] for h in hits] == ["talked about the harvest"]


def test_delete_and_clear(mem):
    keep = mem.add(A, "keep me")
    drop = mem.add(A, "drop me")
    mem.delete(A, drop)
    assert [m["id"] for m in mem.list(A)] == [keep]
    mem.add(B, "bob stays")
    mem.clear(A)
    assert mem.count(A) == 0 and mem.count(B) == 1
    assert A not in mem.list_npcs() and B in mem.list_npcs()


def test_save_and_restore_keeps_order_and_game_time(mem, world):
    from savegame import dump_memory, restore_memory

    _set_clock(world, 10.0)
    for i in range(5):
        mem.add(A, f"memory {i}", type="action")
    saved = {A: dump_memory(mem)[A]}
    _set_clock(world, 999.0)
    mem.clear(A)
    restore_memory(mem, saved)  # 会 clear_all：本测试库里只有测试数据
    restored = mem.list(A)
    assert [m["text"] for m in restored] == [f"memory {i}" for i in range(5)]
    assert {m["metadata"]["t"] for m in restored} == {10.0}
