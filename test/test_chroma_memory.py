"""
ChromaMemory 测试脚本

覆盖：
  1. 单例语义
  2. 添加 / 列举 / 计数
  3. 语义检索的相关性（自然语言 query 命中相近文本）
  4. metadata 过滤（where）
  5. NPC 之间彼此隔离
  6. 删除单条 / 清空 NPC / 清空全库

注意：
  - 首次运行 SentenceTransformer 会从 HuggingFace 下载模型，可能需要 30s+
  - 数据持久化到 AITown3/data/chroma/，跑完默认会保留，方便下次复用

运行（仓库根目录）：
    python AITown3/test/test_chroma_memory.py
    python AITown3/test/test_chroma_memory.py --reset   # 开头先清空全库
    python AITown3/test/test_chroma_memory.py --cleanup # 结束后再清空
"""

import argparse
import os
import sys

# 把 memory 子目录加进 sys.path（独立于 neo4j/actions）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AITOWN3 = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for sub in ("memory",):
    p = os.path.join(_AITOWN3, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from chroma_memory import ChromaMemory  # noqa: E402


# 测试样本 —— 模拟 Alice / Bob 的若干条记忆
ALICE_MEMORIES = [
    ("Alice 在 Tavern 遇到了酒馆老板 Bob，他们聊了村庄的传说。",
     {"type": "dialogue", "location": "Tavern", "actor": "Alice", "target": "Bob"}),
    ("Alice 在 Forest 捡到一把生锈的钥匙 Key。",
     {"type": "observation", "location": "Forest", "actor": "Alice", "target": "Key"}),
    ("Alice 把 Apple 交给了 Bob。",
     {"type": "event", "location": "Tavern", "actor": "Alice", "target": "Bob"}),
    ("Alice 听到 Castle 方向传来狼嚎。",
     {"type": "observation", "location": "TownSquare"}),
    ("Alice 在 Market 看见 Merchant 正在叫卖药水。",
     {"type": "observation", "location": "Market", "actor": "Alice", "target": "Merchant"}),
]

BOB_MEMORIES = [
    ("Bob 在 Tavern 给 Alice 倒了一杯麦酒。",
     {"type": "dialogue", "location": "Tavern", "actor": "Bob", "target": "Alice"}),
    ("Bob 听说 Forest 里最近有怪物出没。",
     {"type": "rumor", "location": "Tavern"}),
]


def _hr(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def _expect(ok: bool, msg: str) -> bool:
    flag = "OK  " if ok else "FAIL"
    print(f"  [{flag}] {msg}")
    return ok


def test_singleton() -> bool:
    _hr("Singleton")
    m1 = ChromaMemory.get_instance()
    m2 = ChromaMemory.get_instance()
    return _expect(m1 is m2, "get_instance() 多次调用返回同一对象")


def test_add_list_count(mem: ChromaMemory) -> bool:
    _hr("Add / List / Count")
    all_ok = True

    mem.clear("Alice")
    mem.clear("Bob")

    for text, meta in ALICE_MEMORIES:
        mem.add("Alice", text, **meta)
    for text, meta in BOB_MEMORIES:
        mem.add("Bob", text, **meta)

    all_ok &= _expect(mem.count("Alice") == len(ALICE_MEMORIES),
                      f"Alice 记忆数 = {mem.count('Alice')}（期望 {len(ALICE_MEMORIES)}）")
    all_ok &= _expect(mem.count("Bob") == len(BOB_MEMORIES),
                      f"Bob 记忆数 = {mem.count('Bob')}（期望 {len(BOB_MEMORIES)}）")

    npcs = sorted(mem.list_npcs())
    all_ok &= _expect("Alice" in npcs and "Bob" in npcs,
                      f"list_npcs() 返回包含 Alice/Bob: {npcs}")
    return all_ok


def test_semantic_search(mem: ChromaMemory) -> bool:
    _hr("Semantic Search（重点：query 与文本不同字面但相关）")
    all_ok = True

    cases = [
        ("我在 Tavern 跟谁说过话？",  "Bob"),
        ("我捡到过什么东西？",         "Key"),
        ("最近有怪物的传闻吗？",       "怪物"),  # 这是给 Bob 的
    ]

    # 前两条是 Alice 的，第三条是 Bob 的
    queries_and_targets = [
        ("Alice", "我在 Tavern 跟谁说过话？", ["Bob"]),
        ("Alice", "我捡到过什么东西？", ["Key", "钥匙"]),
        ("Bob",   "最近有怪物的传闻吗？", ["怪物"]),
    ]

    for npc, query, expected_keywords in queries_and_targets:
        hits = mem.search(npc, query, top_k=3)
        if not hits:
            all_ok &= _expect(False, f"[{npc}] '{query}' 无结果")
            continue
        top_text = hits[0]["text"]
        ok = any(kw in top_text for kw in expected_keywords)
        all_ok &= _expect(
            ok,
            f"[{npc}] '{query}' -> top1={top_text!r}（期望命中 {expected_keywords}）",
        )

    return all_ok


def test_isolation(mem: ChromaMemory) -> bool:
    _hr("Isolation: NPC 之间记忆互不污染")
    # Bob 没有「捡到钥匙」的记忆；用相近 query 搜 Bob 应该不会命中 Alice 的钥匙记忆
    hits = mem.search("Bob", "我捡到过什么东西？", top_k=3)
    if not hits:
        return _expect(True, "Bob 上没有命中任何记忆（也合理）")
    top = hits[0]["text"]
    return _expect("Key" not in top and "钥匙" not in top,
                   f"Bob 的检索结果不包含 Alice 的钥匙记忆（top={top!r}）")


def test_where_filter(mem: ChromaMemory) -> bool:
    _hr("Metadata Filter: where 过滤")
    hits = mem.search("Alice", "发生过什么事", top_k=10,
                      where={"type": "observation"})
    types = [h["metadata"].get("type") for h in hits]
    return _expect(
        all(t == "observation" for t in types) and len(types) > 0,
        f"全部命中 type=observation: {types}",
    )


def test_delete_and_clear(mem: ChromaMemory) -> bool:
    _hr("Delete / Clear")
    all_ok = True

    items = mem.list("Alice")
    if not items:
        return _expect(False, "Alice 应有至少 1 条记忆")
    target = items[0]
    mem.delete("Alice", target["id"])
    all_ok &= _expect(
        mem.count("Alice") == len(ALICE_MEMORIES) - 1,
        f"删除单条后 Alice.count = {mem.count('Alice')}（期望 {len(ALICE_MEMORIES) - 1}）",
    )

    mem.clear("Bob")
    all_ok &= _expect(mem.count("Bob") == 0, "clear(Bob) 后 Bob.count == 0")
    return all_ok


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ChromaMemory 测试")
    parser.add_argument("--reset", action="store_true", help="开头清空整个 chroma 库")
    parser.add_argument("--cleanup", action="store_true", help="结束后清空整个 chroma 库")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    mem = ChromaMemory.get_instance()
    print(f"[chroma path] {mem.persist_path}")
    print(f"[embedding model] {mem.embedder.model_path}")

    if args.reset:
        print("WARNING: --reset 会清空整个 chroma 库")
        mem.clear_all()

    results = []
    results.append(("singleton", test_singleton()))
    results.append(("add_list_count", test_add_list_count(mem)))
    results.append(("semantic_search", test_semantic_search(mem)))
    results.append(("isolation", test_isolation(mem)))
    results.append(("where_filter", test_where_filter(mem)))
    results.append(("delete_clear", test_delete_and_clear(mem)))

    if args.cleanup:
        mem.clear_all()
        print("\n[cleanup] 已清空整个 chroma 库")

    _hr("Summary")
    all_ok = True
    for name, ok in results:
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
        all_ok &= ok

    if not args.cleanup:
        print(f"\n数据保留在 {mem.persist_path}（下次运行会复用）")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
