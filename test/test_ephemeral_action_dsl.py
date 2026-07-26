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
