"""临时 Action = Cypher 解析测试（不依赖 Neo4j）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from ephemeral_action_dsl import parse_action_script, validate_cypher  # noqa: E402


CODE = '''
action chop(actor, target):
    """Chop for wood"""
    MATCH (p:Person {name: $actor})-[:LOCATED_AT]->(loc:Location)<-[:LOCATED_AT]-(t:Item {name: $target})
    CREATE (w:Item {name: 'Wood', item_kind: 'wood', value: 8, portable: true})
    CREATE (p)-[:HAS_ITEM]->(w)
    RETURN $actor + ' chops ' + $target AS narrative

action examine(actor, target):
    """Examine"""
    MATCH (t:Item {name: $target})
    RETURN $actor + ' looks at ' + $target AS narrative
'''


def test_parse_cypher_actions():
    actions = parse_action_script(CODE, host_item="OakTree")
    assert len(actions) == 2
    assert actions[0].id == "chop"
    assert "Chop for wood" in actions[0].label
    assert "CREATE (w:Item" in actions[0].cypher
    assert "$actor" in actions[0].cypher
    ok, _ = validate_cypher(actions[0].cypher)
    assert ok
    bad_ok, _ = validate_cypher("MATCH (n) DETACH DELETE n")
    assert not bad_ok


if __name__ == "__main__":
    test_parse_cypher_actions()
    print("ok")


# ----- 声明式临时动作 -----

from ephemeral_action_dsl import parse_action_json, validate_action_spec  # noqa: E402

NAMES = {"Forest", "OakTree", "Rowan"}
ARCH = {"firewood"}


def _validate(spec):
    return validate_action_spec(spec, host_item="OakTree", entity_names=NAMES, archetypes=ARCH)


def test_valid_declarative_action():
    action, err = _validate({
        "id": "chop", "label": "Chop",
        "requires": [{"op": "prop_gte", "entity": "@target", "prop": "logs", "value": 1}],
        "effects": [{"op": "spawn_item", "archetype": "firewood", "held_by": "@actor"},
                    {"op": "add_prop", "entity": "@target", "prop": "logs", "delta": -1}],
        "narrative": "@actor chops @target",
    })
    assert err == "" and action.declarative and action.host_item == "OakTree"
    assert action.to_dict()["effects"][0]["archetype"] == "firewood"


def test_rejections_explain_what_to_fix():
    cases = {
        "location": {"id": "a", "requires": [{"op": "prop_eq", "entity": "@actor", "prop": "location", "value": "Forest"}]},
        "no expressions": {"id": "a", "effects": [{"op": "set_prop", "entity": "@target", "prop": "steps", "value": {"op": "add"}}]},
        "unsupported effect": {"id": "a", "effects": [{"op": "run_cypher", "query": "MATCH (n) DELETE n"}]},
        "unknown placeholder": {"id": "a", "effects": [{"op": "delete_entity", "entity": "@victim"}]},
        "unknown archetype": {"id": "a", "effects": [{"op": "spawn_item", "archetype": "dragon", "held_by": "@actor"}]},
        "held_by or located_at": {"id": "a", "effects": [{"op": "spawn_item", "archetype": "firewood"}]},
        "unknown entity": {"id": "a", "requires": [{"op": "person_at", "person": "@actor", "location": "Atlantis"}]},
        "bad id": {"id": "Chop Tree!"},
        "node type": {"id": "a", "effects": [{"op": "add_prop", "entity": "@target", "label": "OakTree", "prop": "steps", "delta": 1}]},
        "work for anyone": {"id": "a", "requires": [{"op": "prop_eq", "entity": "@actor", "label": "Person", "prop": "mood", "value": "focused"}]},
    }
    for needle, spec in cases.items():
        action, err = _validate(spec)
        assert action is None and needle in err, (needle, err)


def test_parse_keeps_good_actions_and_reports_bad_ones():
    raw = """```json
{"actions": [
  {"id": "chop", "effects": [{"op": "spawn_item", "archetype": "firewood", "held_by": "@actor"}]},
  {"id": "teleport", "effects": [{"op": "spawn_location", "name": "Moon"}]},
  {"id": "examine", "effects": []}
]}
```"""
    actions, errors = parse_action_json(raw, host_item="OakTree", entity_names=NAMES, archetypes=ARCH)
    assert [a.id for a in actions] == ["chop", "examine"]
    assert len(errors) == 1 and "teleport" in errors[0]
