"""引擎给规则的通用能力（agent/rulebook.py、agent/rules_core.pl）：用一个临时内容包验证，不涉及任何玩法。"""

from __future__ import annotations

import pytest

pytest.importorskip("pyswip")


RULES = r"""
reads(label('Person')).
reads(rel('HAS_ITEM')).

tick_period(10.0).
tick(_{op: add_prop, entity: 'Ash', label: 'Person', prop: ticks, delta: 1}) :- every(10.0).

after(paid(A, B, N), _{op: transfer, from: A, to: B, kind: coin, count: N}).
after(paid(A, B, N), _{op: announce, text: T}) :- format(atom(T), "~w paid ~w", [A, B]).
after(paid(_, B, _), _{op: remember, who: B, text: "I got paid", type: money}).
after(paid(A, _, _), _{op: extend, name: shout, who: A}).

want(P, _{goal: 'Collect item', target: 'kind:coin', reason: broke, priority: 0.5, source: test}) :-
    node('Person', P), \+ rel('HAS_ITEM', P, _).
context(P, "You are a test subject.") :- node('Person', P).
context(P, O, T) :- node('Person', P), node('Person', O), format(atom(T), "~w is here.", [O]).
"""


@pytest.fixture
def lab(world, tmp_path, monkeypatch):
    import content
    import rulebook

    pack = tmp_path / "lab"
    (pack / "rules").mkdir(parents=True)
    (pack / "pack.json").write_text('{"name": "lab"}')
    (pack / "rules" / "lab.pl").write_text(RULES)
    for src in ("base", "rpg", "streets", "village"):
        (tmp_path / src).symlink_to(content.CONTENT_ROOT / src)
    monkeypatch.setattr(content, "CONTENT_ROOT", tmp_path)
    content.reload_content(["lab"])
    world.clear_all()
    for name in ("Ash", "Bram"):
        world.add_entity(name, entity_type="Person", hp=10)
    for i in range(3):
        world.add_entity(f"Coin{i}", entity_type="Item", item_kind="coin")
        world.link_entities("Ash", "Person", f"Coin{i}", "Item", "HAS_ITEM")
    yield world
    rulebook.EXTENSIONS.pop("shout", None)
    rulebook.use_memory(None)
    monkeypatch.undo()
    content.reload_content()


class Memory:
    def __init__(self):
        self.added = []

    def add(self, who, text, **meta):
        self.added.append((who, text, meta))


def _coins(world, who):
    rows = world.run_query("MATCH (:Person {name: $n})-[:HAS_ITEM]->(i:Item) RETURN count(i) AS n", n=who)
    return rows[0]["n"]


def test_an_event_can_move_things_announce_remember_and_call_an_extension(lab):
    import rulebook

    memory = Memory()
    rulebook.use_memory(memory)
    seen = []
    rulebook.extension("shout")(lambda world, e: seen.append(e["who"]) or
                                [{"op": "note", "text": f"{e['who']} shouts"}])
    out = rulebook.happen(lab, "paid", "Ash", "Bram", 2)
    assert (_coins(lab, "Ash"), _coins(lab, "Bram")) == (1, 2)
    assert out["events"] == [{"type": "announce", "text": "Ash paid Bram", "source": "rules"}]
    assert memory.added == [("Bram", "I got paid", {"type": "money"})]
    assert seen == ["Ash"] and out["notes"] == ["Ash shouts"], "an extension may answer with more effects"
    rulebook.happen(lab, "paid", "Ash", "Bram", 5)
    assert _coins(lab, "Ash") == 0, "you cannot hand over more than you have"


def test_a_time_jump_is_caught_up_one_period_at_a_time(lab):
    import rulebook

    def ticks():
        return lab.run_query("MATCH (p:Person {name: 'Ash'}) RETURN coalesce(p.ticks, 0) AS n")[0]["n"]

    rulebook.tick(lab, 1.0, 5.0)
    assert ticks() == 0, "no period boundary crossed"
    rulebook.tick(lab, 5.0, 12.0)
    assert ticks() == 1
    rulebook.tick(lab, 12.0, 45.0)
    assert ticks() == 4, "20, 30, 40 each count"
    rulebook.tick(lab, 45.0, 1000.0)
    assert ticks() == 4 + rulebook.MAX_CATCHUP, "a long jump only catches up so far"


def test_wants_and_prompt_lines_come_from_rules(lab):
    import rulebook

    assert rulebook.want(lab, "Ash") == []
    goals = rulebook.want(lab, "Bram")
    assert goals and goals[0]["goal"] == "Collect item" and goals[0]["target_entity"] == "kind:coin"
    assert rulebook.context(lab, "Ash") == ["You are a test subject."]
    assert rulebook.context(lab, "Ash", "Bram") == ["Bram is here."]


def test_chance_is_repeatable_and_roughly_right():
    import json

    from prolog_engine import fact, solve
    from rulebook import rule_files

    def hits(now):
        rows = solve(rule_files(), [fact("g_now", now)],
                     "answers_json(K, (between(1, 2000, K), chance(K, 0.25)), J)")
        return json.loads(rows[0]["J"])

    assert hits(10.0) == hits(10.0), "the same moment gives the same answer"
    assert hits(10.0) != hits(20.0)
    assert 400 < len(hits(10.0)) < 600


def test_days_follow_the_engine_clock():
    import json

    from prolog_engine import fact, solve
    from rulebook import rule_files

    rows = solve(rule_files(), [fact("g_day_length", 100.0)], "answers_json(D, day(250.0, D), J)")
    assert json.loads(rows[0]["J"]) == [2]
