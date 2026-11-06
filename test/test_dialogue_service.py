"""
dialogue_service 测试 — Mock 对话生成与记忆双写（Neo4jMemory，需要 Neo4j）。
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AITOWN3 = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for sub in ("neo4j", "actions", "planner", "agent", "utility", "memory", "test"):
    p = os.path.join(_AITOWN3, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from dialogue_service import (  # noqa: E402
    generate_talk_dialogue,
    handle_talk_dialogue,
    write_dialogue_memories,
)
from neo4j_memory import Neo4jMemory  # noqa: E402
import test_build_neo4j as builder  # noqa: E402
from neo4j_world import Neo4jWorld  # noqa: E402


def _expect(ok: bool, msg: str) -> bool:
    print(f"  [{'OK' if ok else 'FAIL'}] {msg}")
    return ok


def main() -> int:
    ok_all = True

    dlg = generate_talk_dialogue(
        None, "Alice", "Bob", "Tavern", force_mock=True
    )
    ok_all &= _expect(bool(dlg.get("lines")), "mock 对话生成 lines")
    ok_all &= _expect(len(dlg["lines"]) >= 2, "mock 对话至少 2 句")
    ok_all &= _expect("summary" in dlg, "mock 对话含 summary")

    try:
        world = Neo4jWorld.get_instance()
        world.run_query("RETURN 1")
    except Exception as e:
        print(f"跳过记忆与 Neo4j 集成测试: {e}")
        return 0 if ok_all else 1

    Neo4jMemory._instance = None  # type: ignore[attr-defined]
    memory = Neo4jMemory.get_instance(world.driver)
    memory.clear("Alice")
    memory.clear("Bob")

    write_dialogue_memories(
        memory,
        dlg["lines"],
        "Tavern",
        speaker_a="Alice",
        speaker_b="Bob",
    )
    ok_all &= _expect(memory.count("Alice") >= 1, "Alice 写入 dialogue 记忆")
    ok_all &= _expect(memory.count("Bob") >= 1, "Bob 写入 dialogue 记忆")

    alice_items = memory.list("Alice")
    ok_all &= _expect(
        any((m.get("metadata") or {}).get("type") == "dialogue" for m in alice_items),
        "Alice 记忆 type=dialogue",
    )

    try:
        world.clear_all()
        builder.build_world(world)

        rows = world.run_query(
            """
            MATCH (a:Person {name: 'Alice'}), (b:Person {name: 'Bob'})
            MATCH (l:Location {name: 'Tavern'})
            RETURN a.name AS a, b.name AS b, l.name AS l
            """
        )
        if rows:
            world.run_query(
                """
                MATCH (a:Person {name: 'Alice'}), (l:Location {name: 'Tavern'})
                OPTIONAL MATCH (a)-[r:LOCATED_AT]->(:Location)
                DELETE r
                WITH a, l
                CREATE (a)-[:LOCATED_AT]->(l)
                """
            )
            world.run_query(
                """
                MATCH (b:Person {name: 'Bob'}), (l:Location {name: 'Tavern'})
                OPTIONAL MATCH (b)-[r:LOCATED_AT]->(:Location)
                DELETE r
                WITH b, l
                CREATE (b)-[:LOCATED_AT]->(l)
                """
            )

        from registry import get_registry

        reg = get_registry()
        ok, _ = reg.execute(world, "talk", a="Alice", b="Bob", l="Tavern")
        ok_all &= _expect(ok, "talk 动作成功")

        integrated = handle_talk_dialogue(
            world, "Alice", "Bob", "Tavern", memory=memory, force_mock=True
        )
        ok_all &= _expect(bool(integrated.get("summary")), "handle_talk_dialogue 返回 summary")
        ok_all &= _expect(memory.count("Alice") >= 2, "集成后 Alice 记忆增加")
    finally:
        memory.close()
        try:
            world.close()
        except Exception:
            pass

    print("\nALL PASS" if ok_all else "\nSOME CHECKS FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
