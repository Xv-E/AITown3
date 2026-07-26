"""
GBNF 语法生成单元测试

测试对象：AITown3/utility/gbnf.py

实体名一律从 Neo4j 图数据库拉取（collect_entities_by_type），不使用硬编码假数据。

测什么：
  1. 通用 JSON 语法（步骤 1–4）能否被 LlamaGrammar 解析
  2. synthesis 语法按 goal 类型配对 location_entity / person_entity / item_entity
  3. 图中全部 Location / Person / Item 进入对应 GBNF 枚举
  4. 空 goal_terms 时回退宽松 JSON 语法

运行（需 Neo4j 已启动，仓库根目录）：
    python AITown3/test/test_gbnf.py
    python AITown3/test/test_gbnf.py --quiet
    python AITown3/test/test_gbnf.py --grammar-only
"""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from typing import Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_AITOWN3 = os.path.abspath(os.path.join(_THIS_DIR, ".."))
for sub in ("neo4j", "utility", "agent", "test"):
    p = os.path.join(_AITOWN3, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from gbnf import (  # noqa: E402
    ITEM_GOALS,
    LOCATION_GOALS,
    PERSON_GOALS,
    EntitiesByType,
    _quoted_alternatives,
    collect_entities_by_type,
    collect_entity_names,
    dialogue_limits_prompt_block,
    generate_dialogue_grammar,
    generate_json_array_grammar,
    generate_json_grammar,
    generate_synthesis_array_grammar,
    generate_synthesis_grammar,
)
from goal_terms import get_all_goal_terms  # noqa: E402

try:
    from llama_cpp import LlamaGrammar

    HAS_LLAMA_GRAMMAR = True
except ImportError:
    HAS_LLAMA_GRAMMAR = False

# 模块级 Neo4j 世界（setUpModule / main 初始化）
_WORLD = None
_TYPED: Optional[EntitiesByType] = None
_NEO4J_SKIP_REASON = ""


def _hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _parse_grammar(grammar_str: str) -> None:
    if not HAS_LLAMA_GRAMMAR:
        raise unittest.SkipTest("llama_cpp 未安装，跳过 GBNF 解析测试")
    LlamaGrammar.from_string(grammar_str)


def _build_world():
    from neo4j_world import Neo4jWorld
    import test_build_neo4j as builder

    Neo4jWorld._instance = None  # type: ignore[attr-defined]
    world = Neo4jWorld.get_instance()
    world.clear_all()
    builder.build_world(world)
    return world


def _ensure_neo4j() -> bool:
    global _WORLD, _TYPED, _NEO4J_SKIP_REASON
    if _WORLD is not None:
        return True
    if "--no-neo4j" in sys.argv:
        _NEO4J_SKIP_REASON = "已指定 --no-neo4j"
        return False
    try:
        _WORLD = _build_world()
        _TYPED = collect_entities_by_type(_WORLD)
        return True
    except Exception as exc:
        _NEO4J_SKIP_REASON = str(exc)
        return False


def _require_neo4j(test_case: unittest.TestCase) -> EntitiesByType:
    if not _ensure_neo4j() or _TYPED is None:
        test_case.skipTest(_NEO4J_SKIP_REASON or "Neo4j 不可用")
    return _TYPED


def _partial_goals_from_db() -> list[str]:
    """从 goal_terms 取地点+人物类（用于单条 synthesis 示例）。"""
    terms = get_all_goal_terms()
    out = [g for g in terms if g in LOCATION_GOALS]
    out += [g for g in terms if g in PERSON_GOALS]
    return out


def _quote_in_grammar(name: str) -> str:
    return f'"\\"{name}\\""'


def print_generated_grammars() -> None:
    """打印语法；实体全部来自 Neo4j。"""
    if not _ensure_neo4j() or _WORLD is None or _TYPED is None:
        print("无法连接 Neo4j，跳过 synthesis 语法打印。请启动 Neo4j 后重试。")
        return

    terms = get_all_goal_terms()
    typed = _TYPED
    partial = _partial_goals_from_db()

    specs = [
        ("1. 通用 JSON 对象（步骤 1–4）", generate_json_grammar()),
        ("2. 通用 JSON 数组", generate_json_array_grammar()),
        (
            "3. synthesis 单条（Neo4j 实体 + 地点/人物 goal）",
            generate_synthesis_grammar(partial, typed, include_priority=False),
        ),
        (
            "4. synthesis 单条（含 priority）",
            generate_synthesis_grammar(partial, typed, include_priority=True),
        ),
        (
            "5. synthesis 数组（地点/人物 goal）",
            generate_synthesis_array_grammar(partial, typed),
        ),
        (
            f"6. synthesis 数组（全部 {len(terms)} 个 goal + Neo4j 实体）",
            generate_synthesis_array_grammar(terms, typed),
        ),
        (
            "7. 空 goal_terms 回退通用 JSON",
            generate_synthesis_grammar([], typed),
        ),
    ]

    _hr("生成的 GBNF 语法（实体来自 Neo4j）")
    print(f"llama_cpp 可用: {HAS_LLAMA_GRAMMAR}")
    print(f"Location ({len(typed.locations)}): {', '.join(typed.locations)}")
    print(f"Person ({len(typed.persons)}): {', '.join(typed.persons)}")
    print(f"Item ({len(typed.items)}): {', '.join(typed.items)}")
    print(f"goal_terms: {', '.join(terms)}")

    for title, grammar in specs:
        _hr(title)
        print(grammar)
        if HAS_LLAMA_GRAMMAR:
            try:
                LlamaGrammar.from_string(grammar)
                print("\n[OK] LlamaGrammar 解析通过")
            except Exception as e:
                print(f"\n[FAIL] LlamaGrammar 解析失败: {e}")


class TestQuotedAlternatives(unittest.TestCase):
    def test_empty_returns_empty_literal(self):
        self.assertEqual(_quoted_alternatives([]), '""')

    def test_joins_quoted_literals(self):
        alt = _quoted_alternatives(["Foo", "Bar Baz"])
        self.assertIn('"\\"Foo\\""', alt)
        self.assertIn(" | ", alt)


class TestGenericGrammars(unittest.TestCase):
    def test_json_grammar_parses(self):
        _parse_grammar(generate_json_grammar())

    def test_json_array_grammar_parses(self):
        _parse_grammar(generate_json_array_grammar())

    def test_dialogue_grammar_parses(self):
        g = generate_dialogue_grammar(
            "Traveler",
            "Bob",
            "Where is the key?",
        )
        self.assertIn("npc-text", g)
        self.assertIn("char{1,160}", g)
        self.assertNotIn("revealed", g)
        self.assertNotIn("clue", g)
        self.assertNotIn("player-line", g)
        self.assertNotIn('"\\"Traveler\\""', g)
        self.assertIn('"\\"Bob\\""', g)
        self.assertIn("Field length limits", dialogue_limits_prompt_block())
        _parse_grammar(g)

    def test_dialogue_grammar_trust_only(self):
        g = generate_dialogue_grammar("A", "B", "hi")
        self.assertIn("trust-delta", g)
        self.assertNotIn("clue-name", g)
        _parse_grammar(g)

    def test_empty_goals_falls_back_to_generic_json(self):
        typed = _require_neo4j(self)
        g = generate_synthesis_grammar([], typed)
        self.assertIn("members ::=", g)
        self.assertNotIn("location_object", g)


class TestNeo4jEntities(unittest.TestCase):
    def test_collects_by_type(self):
        typed = _require_neo4j(self)
        self.assertGreater(len(typed.locations), 0)
        self.assertGreater(len(typed.persons), 0)
        self.assertGreater(len(typed.items), 0)
        self.assertIn("TownSquare", typed.locations)
        self.assertIn("Alice", typed.persons)
        self.assertIn("Sword", typed.items)

    def test_collects_sorted_unique_names(self):
        typed = _require_neo4j(self)
        names = collect_entity_names(_WORLD)
        self.assertEqual(names, sorted(set(names)))
        for n in typed.locations + typed.persons + typed.items:
            self.assertIn(n, names)

    def test_excludes_character_optional(self):
        typed = _require_neo4j(self)
        bob_filtered = collect_entities_by_type(_WORLD, exclude_character="Bob")
        self.assertIn("Bob", typed.persons)
        self.assertNotIn("Bob", bob_filtered.persons)


class TestSynthesisFromNeo4j(unittest.TestCase):
    def test_synthesis_grammar_parses(self):
        typed = _require_neo4j(self)
        g = generate_synthesis_grammar(_partial_goals_from_db(), typed)
        _parse_grammar(g)

    def test_all_db_persons_in_person_entity(self):
        typed = _require_neo4j(self)
        g = generate_synthesis_grammar(_partial_goals_from_db(), typed)
        for person in typed.persons:
            self.assertIn(_quote_in_grammar(person), g, msg=f"missing person: {person}")

    def test_all_db_locations_in_location_entity(self):
        typed = _require_neo4j(self)
        g = generate_synthesis_grammar(_partial_goals_from_db(), typed)
        for loc in typed.locations:
            self.assertIn(_quote_in_grammar(loc), g, msg=f"missing location: {loc}")

    def test_splits_entity_types(self):
        typed = _require_neo4j(self)
        g = generate_synthesis_grammar(_partial_goals_from_db(), typed)
        self.assertIn("location_object", g)
        self.assertIn("person_object", g)
        self.assertIn("location_entity ::=", g)
        self.assertIn("person_entity ::=", g)
        self.assertNotIn("entity_option ::=", g)
        self.assertNotIn(_quote_in_grammar("town"), g)

    def test_item_goal_uses_db_items_only(self):
        typed = _require_neo4j(self)
        item_goals = [g for g in get_all_goal_terms() if g in ITEM_GOALS]
        self.assertTrue(item_goals)
        g = generate_synthesis_grammar([item_goals[0]], typed)
        self.assertIn("item_object", g)
        for item in typed.items:
            self.assertIn(_quote_in_grammar(item), g)
        self.assertNotIn("location_object", g)

    def test_full_goal_array_includes_all_types(self):
        typed = _require_neo4j(self)
        terms = get_all_goal_terms()
        g = generate_synthesis_array_grammar(terms, typed)
        _parse_grammar(g)
        self.assertIn("location_object", g)
        self.assertIn("person_object", g)
        self.assertIn("item_object", g)
        for term in terms:
            self.assertIn(_quote_in_grammar(term), g, msg=f"missing goal: {term}")

    def test_priority_flag(self):
        typed = _require_neo4j(self)
        partial = _partial_goals_from_db()
        self.assertNotIn('"\\"priority\\":', generate_synthesis_grammar(partial, typed, include_priority=False))
        self.assertIn('"\\"priority\\":', generate_synthesis_array_grammar(partial, typed))


def setUpModule():
    _ensure_neo4j()


def main() -> int:
    parser = argparse.ArgumentParser(description="GBNF 语法生成测试（Neo4j 实体）")
    parser.add_argument("--no-neo4j", action="store_true", help="跳过需 Neo4j 的用例")
    parser.add_argument("--quiet", action="store_true", help="不打印 GBNF 语法")
    parser.add_argument("--grammar-only", action="store_true", help="只打印语法")
    args, _ = parser.parse_known_args()

    if args.no_neo4j and "--no-neo4j" not in sys.argv:
        sys.argv.append("--no-neo4j")

    if not args.quiet or args.grammar_only:
        print_generated_grammars()

    if args.grammar_only:
        return 0 if _WORLD is not None else 1

    _hr("单元测试")
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
