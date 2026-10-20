"""GBNF helpers for JSON-constrained LLM output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

# goal_term -> 允许的 target_entity 种类
LOCATION_GOALS = frozenset({
    "Go to location",
    "Explore unknown location",
})
PERSON_GOALS = frozenset({
    "Gain information",
    "Defeat enemy",
})
ITEM_GOALS = frozenset({
    "Collect item",
    "Accumulate items",
    "Strengthen abilities",
    "Enhance capabilities",
})


@dataclass(frozen=True)
class EntitiesByType:
    locations: List[str]
    persons: List[str]
    items: List[str]


def _quoted_alternatives(values: List[str]) -> str:
    """Turn strings into GBNF quoted-literal alternation: \"Foo\" | \"Bar\"."""
    if not values:
        return "\"\""
    return " | ".join(f'"\\"{v}\\""' for v in values)


def collect_entities_by_type(
    world: "Neo4jWorld",
    *,
    exclude_character: Optional[str] = None,
) -> EntitiesByType:
    """按 Location / Person / Item 分类收集图中实体名。"""
    locations = sorted(world.list_entities("Location") or [])
    persons = sorted(
        p for p in (world.list_entities("Person") or [])
        if not (exclude_character and p == exclude_character)
    )
    items = sorted(world.list_entities("Item") or [])
    return EntitiesByType(locations=locations, persons=persons, items=items)


def collect_entity_names(
    world: "Neo4jWorld",
    *,
    exclude_character: Optional[str] = None,
) -> List[str]:
    """扁平列表（兼容旧调用）。"""
    e = collect_entities_by_type(world, exclude_character=exclude_character)
    return sorted(set(e.locations + e.persons + e.items))


def _entity_rule(rule_name: str, names: List[str]) -> str:
    if names:
        return f"{rule_name} ::= {_quoted_alternatives(names)} | null"
    return f"{rule_name} ::= null"


def _factors_members(*, include_priority: bool) -> str:
    if include_priority:
        return (
            '"\\"values\\":" ws number "," ws "\\"emotion\\":" ws number "," ws '
            '"\\"background\\":" ws number "," ws "\\"environment\\":" ws number "," ws '
            '"\\"priority\\":" ws number'
        )
    return (
        '"\\"values\\":" ws number "," ws "\\"emotion\\":" ws number "," ws '
        '"\\"background\\":" ws number "," ws "\\"environment\\":" ws number'
    )


def _shared_field_rules(*, include_priority: bool) -> str:
    factors = _factors_members(include_priority=include_priority)
    return f"""
reasoning_field ::= "\\"reasoning\\":" ws string
confidence_field ::= "\\"confidence\\":" ws number
factors_field ::= "\\"contributing_factors\\":" ws factors_object
factors_object ::= "{{" ws {factors} ws "}}"
"""


def _object_variant(variant: str, goal_rule: str, entity_rule: str) -> str:
    return (
        f'{variant} ::= "{{" ws "\\"final_goal\\":" ws {goal_rule} "," ws '
        f'"\\"target_entity\\":" ws {entity_rule} "," ws reasoning_field "," ws '
        f'confidence_field "," ws factors_field ws "}}"'
    )


def _build_typed_synthesis(
    goal_terms: List[str],
    entities: EntitiesByType,
    *,
    include_priority: bool,
) -> str:
    """按 goal 类型生成 location_object / person_object / item_object 分支。"""
    variants: List[str] = []
    rules: List[str] = []

    loc_goals = [g for g in goal_terms if g in LOCATION_GOALS]
    person_goals = [g for g in goal_terms if g in PERSON_GOALS]
    item_goals = [g for g in goal_terms if g in ITEM_GOALS]

    if loc_goals:
        variants.append("location_object")
        rules.append(f"location_goal ::= {_quoted_alternatives(loc_goals)}")
        rules.append(_entity_rule("location_entity", entities.locations))
        rules.append(_object_variant("location_object", "location_goal", "location_entity"))

    if person_goals:
        variants.append("person_object")
        rules.append(f"person_goal ::= {_quoted_alternatives(person_goals)}")
        rules.append(_entity_rule("person_entity", entities.persons))
        rules.append(_object_variant("person_object", "person_goal", "person_entity"))

    if item_goals:
        variants.append("item_object")
        rules.append(f"item_goal ::= {_quoted_alternatives(item_goals)}")
        rules.append(_entity_rule("item_entity", entities.items))
        rules.append(_object_variant("item_object", "item_goal", "item_entity"))

    if not variants:
        # 无识别 goal 时回退：任意 goal + null target
        rules.append(f"goal_term ::= {_quoted_alternatives(goal_terms)}")
        rules.append("entity_option ::= null")
        rules.append(
            'object ::= "{" ws "\\"final_goal\\":" ws goal_term "," ws '
            '"\\"target_entity\\":" ws entity_option "," ws reasoning_field "," ws '
            'confidence_field "," ws factors_field ws "}"'
        )
    else:
        alt = " | ".join(variants)
        rules.append(f"object ::= {alt}")

    rules.append(_shared_field_rules(include_priority=include_priority))

    scalar_rules = """
string ::= "\\"" ([^"\\\\] | "\\\\" (["\\\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]))* "\\""
number ::= "-"? ([0-9] | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [+-]? [0-9]+)?
boolean ::= "true" | "false"
null ::= "null"
ws ::= [ \\t\\n\\r]*
"""
    return "\n".join(rules) + scalar_rules


def _normalize_entities(
    entities: EntitiesByType | None,
    locations: Optional[List[str]] = None,
    persons: Optional[List[str]] = None,
    items: Optional[List[str]] = None,
) -> EntitiesByType:
    if isinstance(entities, EntitiesByType):
        return entities
    return EntitiesByType(
        locations=list(locations or []),
        persons=list(persons or []),
        items=list(items or []),
    )


def generate_synthesis_grammar(
    goal_terms: List[str],
    entities: EntitiesByType | None = None,
    *,
    locations: Optional[List[str]] = None,
    persons: Optional[List[str]] = None,
    items: Optional[List[str]] = None,
    include_priority: bool = False,
) -> str:
    """
    Step-5 synthesis JSON：final_goal 与 target_entity 按类型配对约束。
    - Go to / Explore -> location_entity
    - Gain information / Defeat enemy -> person_entity
    - Collect item 等 -> item_entity
    """
    if not goal_terms:
        return generate_json_grammar()

    typed = _normalize_entities(entities, locations, persons, items)
    body = _build_typed_synthesis(goal_terms, typed, include_priority=include_priority)
    return f"root ::= object\n{body}".strip()


def generate_synthesis_array_grammar(
    goal_terms: List[str],
    entities: EntitiesByType | None = None,
    *,
    locations: Optional[List[str]] = None,
    persons: Optional[List[str]] = None,
    items: Optional[List[str]] = None,
    include_priority: bool = True,
) -> str:
    """Step-5 候选数组：每个元素为类型化 synthesis object。"""
    if not goal_terms:
        return generate_json_array_grammar()

    typed = _normalize_entities(entities, locations, persons, items)
    body = _build_typed_synthesis(goal_terms, typed, include_priority=include_priority)
    header = (
        "root ::= array\n"
        'array ::= "[" ws "]" | "[" ws elements ws "]"\n'
        "elements ::= object | object \",\" ws elements\n"
    )
    return (header + body).strip()


def generate_json_grammar() -> str:
    grammar = """
        root ::= object
        object ::= "{" ws "}" | "{" ws members ws "}"
        members ::= member | member "," ws members
        member ::= ws string ws ":" ws value
        array ::= "[" ws "]" | "[" ws elements ws "]"
        elements ::= value | value "," ws elements
        value ::= string | number | object | array | boolean | null
        string ::= "\\"" ([^"\\\\] | "\\\\" (["\\\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]))* "\\""
        number ::= "-"? ([0-9] | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [+-]? [0-9]+)?
        boolean ::= "true" | "false"
        null ::= "null"
        ws ::= [ \\t\\n\\r]*
        """
    return grammar.strip()


def generate_json_array_grammar() -> str:
    grammar = """
        root ::= array
        object ::= "{" ws "}" | "{" ws members ws "}"
        members ::= member | member "," ws members
        member ::= ws string ws ":" ws value
        array ::= "[" ws "]" | "[" ws elements ws "]"
        elements ::= object | object "," ws elements
        value ::= string | number | object | array | boolean | null
        string ::= "\\"" ([^"\\\\] | "\\\\" (["\\\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]))* "\\""
        number ::= "-"? ([0-9] | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [+-]? [0-9]+)?
        boolean ::= "true" | "false"
        null ::= "null"
        ws ::= [ \\t\\n\\r]*
        """
    return grammar.strip()


# —— Talk 对话 JSON：分字段长度上限（GBNF + 提示词共用）——
DIALOGUE_FIELD_LIMITS = {
    "player_text": 200,
    "npc_text": 220,
    "npc_lines_min": 1,
    "npc_lines_max": 2,
}


def _gbnf_escape_literal(text: str) -> str:
    """转义后放入 GBNF 字面量 \"...\" 内部。"""
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "")
        .replace("\t", "\\t")
    )


def dialogue_limits_prompt_block() -> str:
    """给 LLM 提示词用的字段长度说明（与 GBNF 一致）。"""
    L = DIALOGUE_FIELD_LIMITS
    return (
        "Field length limits (must obey; grammar-enforced):\n"
        f"- lines: only NPC replies, {L['npc_lines_min']}..{L['npc_lines_max']} objects\n"
        f"- lines[].text: {1}..{L['npc_text']} chars each\n"
        "- Do NOT echo the player's utterance in lines\n"
        "- trust_delta: -1 | 0 | 1 | 2"
    )


def generate_dialogue_grammar(
    speaker_a: str,
    speaker_b: str,
    player_message: str = "",
) -> str:
    """
    Talk 响应 JSON 的严格 GBNF（仅 NPC 台词，不回显玩家）:
    {
      "lines":[npc_line, optional npc_line],
      "trust_delta": -1|0|1|2
    }
    speaker_a / player_message 保留参数以兼容旧调用，语法中不再使用。
    """
    del speaker_a, player_message  # unused; kept for call-site compatibility
    L = DIALOGUE_FIELD_LIMITS
    b = _gbnf_escape_literal(speaker_b)

    return f"""
root ::= object
object ::= "{{" ws lines-field "," ws trust-field ws "}}"

lines-field ::= "\\"lines\\":" ws lines-array
lines-array ::= "[" ws npc-line ws "]" | "[" ws npc-line "," ws npc-line ws "]"

npc-line ::= "{{" ws "\\"speaker\\":" ws "\\"{b}\\"" "," ws "\\"text\\":" ws npc-text ws "}}"

npc-text ::= "\\"" char{{1,{L["npc_text"]}}} "\\""

trust-field ::= "\\"trust_delta\\":" ws trust-delta
trust-delta ::= "-1" | "0" | "1" | "2"

char ::= [^"\\\\] | "\\\\" (["\\\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F])
ws ::= [ \\t\\n\\r]*
""".strip()
