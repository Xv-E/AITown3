"""内容包规则（content/base/rules/social.pl）经引擎核心（agent/rules_core.pl）运行。

前半部分直接喂图形状的事实（g_node / g_prop / g_rel，不需要 Neo4j）；
后半部分在真实世界里：推演 == 真做之后；以及「只加一个 .pl 文件就能加规则」。
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyswip")


def node(label, name, **props):
    from prolog_engine import atom, fact

    return [fact("g_node", label, name)] + [f"g_prop({atom(name)}, {atom(k)}, {atom(v)})" for k, v in props.items()]


def rel(t, a, b, **props):
    from prolog_engine import atom

    pairs = ", ".join(f"{atom(k)}-{atom(v)}" for k, v in props.items())
    return [f"g_rel({atom(t)}, {atom(a)}, {atom(b)}, [{pairs}])"]


def person(name, **props):
    return node("Person", name, hp=props.pop("hp", 50), **props)


def answers(facts, template, goal, now=100.0):
    import json

    from prolog_engine import fact, solve
    from rulebook import rule_files

    rows = solve(rule_files(), list(facts) + [fact("g_now", now)], f"answers_json({template}, ({goal}), J)")
    return json.loads(rows[0]["J"])


def why(facts, a, b):
    got = answers(facts, "L", f"hostility('{a}', '{b}', W), label(W, L)")
    return got[0] if got else ""


def test_reasons_come_in_priority_order():
    base = person("Ash") + person("Bram") + rel("FEELS", "Ash", "Bram", value=-9)
    assert why(base, "Ash", "Bram") == "hatred"
    hit = person("Ash", attacked_by="Bram", attacked_at=95.0) + person("Bram") + rel("FEELS", "Ash", "Bram", value=-9)
    assert why(hit, "Ash", "Bram") == "attacked me", "being attacked outranks hatred"
    stale = person("Ash", attacked_by="Bram", attacked_at=10.0) + person("Bram") + rel("FEELS", "Ash", "Bram", value=-9)
    assert why(stale, "Ash", "Bram") == "hatred", "an old attack is forgotten"
    assert why(base, "Bram", "Ash") == "", "hostility is one-way"


def test_monsters_fight_everyone_and_the_dead_fight_nobody():
    facts = person("Wolf", faction="monster") + person("Sage")
    assert why(facts, "Wolf", "Sage") == "faction" and why(facts, "Sage", "Wolf") == "faction"
    assert why(person("Wolf", faction="monster", dead=True) + person("Sage"), "Sage", "Wolf") == ""


def test_npcs_feel_about_the_player_through_trust():
    facts = person("Sage", trust_toward_player=-7) + person("Hero", is_player=True) + rel("FEELS", "Sage", "Hero", value=5)
    assert why(facts, "Sage", "Hero") == "hatred", "trust, not the FEELS edge, is how an NPC feels about the player"


def test_alliances_at_war():
    facts = (person("Ash") + person("Bram") + node("Alliance", "Reds") + node("Alliance", "Blues")
             + rel("MEMBER_OF", "Ash", "Reds") + rel("MEMBER_OF", "Bram", "Blues") + rel("STANCE", "Reds", "Blues", value=-6))
    assert why(facts, "Ash", "Bram") == "war"


def test_theft_needs_a_living_owner_who_is_not_your_ally():
    facts = person("Sage") + person("Ash") + rel("OWNS", "Sage", "Knife")
    goal = "theft('Ash', 'Knife', O)"
    assert answers(facts, "O", goal) == ["Sage"]
    assert answers(facts + rel("ALLY", "Sage", "Ash"), "O", goal) == []
    assert answers(person("Sage", dead=True) + person("Ash") + rel("OWNS", "Sage", "Knife"), "O", goal) == []
    assert answers(facts, "O", "theft('Sage', 'Knife', O)") == [], "your own things"


def test_forecast_names_who_would_turn_hostile_and_leaves_no_trace():
    from prolog_engine import fact

    facts = (person("Ash") + person("Sage") + person("Rowan", guard=True) + person("Hero", is_player=True)
             + rel("OWNS", "Sage", "Knife") + rel("FEELS", "Sage", "Ash", value=-2)
             + [fact("sees", w, "Ash") for w in ("Rowan", "Sage", "Hero")])
    lines = answers(facts, "T", "forecast(take('Ash', 'Knife', 'Grove'), T)")
    assert lines[0] == "taking it is stealing"
    # 守卫看见了就当罪犯；失主 -2 再 -5 = -7，跌破仇恨线；玩家的感受不替他推演
    assert any(t.startswith("then Rowan would turn hostile (Rowan is a guard and saw Ash commit theft") for t in lines)
    assert any(t.startswith("then Sage would turn hostile (Sage hates Ash (feeling -7))") for t in lines)
    assert not any("Hero" in t for t in lines)
    assert answers(facts, "x", "\\+ hyp(_)") == ["x"], "the assumptions are gone afterwards"


def test_after_a_theft_the_effects_are_plain_graph_ops():
    from prolog_engine import fact

    facts = person("Ash") + person("Sage") + rel("OWNS", "Sage", "Knife") + [fact("sees", "Sage", "Ash")]
    effects = answers(facts, "E", "after(take('Ash', 'Knife', 'Grove'), E)")
    ops = sorted(e["op"] for e in effects)
    assert ops == ["add_prop", "feel", "link", "note", "report"]
    link = next(e for e in effects if e["op"] == "link")
    assert link["rel"] == "WITNESSED" and link["props"] == {"crime": "theft", "victim": "Sage", "item": "Knife", "at": 100.0}


# ---------------------------------------------------------------------
# 真实世界
# ---------------------------------------------------------------------

@pytest.fixture
def camp(world):
    from director import build_from_blueprint, load_world

    build_from_blueprint(world, load_world("town_daily_life"), clear=True)
    return world


def _forecast(world, me, item, loc):
    from rulebook import ask, watched_by

    return ask(world, "T", f"forecast(take('{me}', '{item}', '{loc}'), T)", sees=watched_by(world, me))


def test_the_forecast_matches_what_happens(camp):
    from combat import hostility
    from ownership import set_owner, witnesses
    from registry import get_registry
    from relations import consequences
    from test_combat import _stand

    camp.run_query("MATCH (p:Person {name: 'Rowan'}) SET p.guard = true")
    set_owner(camp, "Berry3", "Sage")
    me = _stand(camp, "Observer", "BerryGrove")
    _stand(camp, "Rowan", "BerryGrove", near=me)
    watchers = witnesses(camp, "Observer")
    assert "Rowan" in watchers

    lines = _forecast(camp, "Observer", "Berry3", "BerryGrove")
    assert lines[0] == "taking it is stealing"
    predicted = sorted(t.split()[1] for t in lines if t.startswith("then "))
    assert "Rowan" in predicted

    params = {"c": "Observer", "i": "Berry3", "l": "BerryGrove"}
    ok, why = get_registry().execute(camp, "pick_up", **params)
    assert ok, why
    out = consequences(camp, "Observer", "pick_up", params, at=0.0)
    assert out["theft"] and out["victim"] == "Sage" and out["notes"][0].startswith("you stole from Sage; seen by")
    actual = sorted(w for w in watchers if hostility(camp, w, "Observer", at=0.0))
    assert actual == predicted


def test_an_allys_things_are_not_stolen_and_reasons_read_well(camp):
    from combat import why_hostile
    from ownership import set_owner
    from relations import _link_allies

    set_owner(camp, "Berry3", "Sage")
    _link_allies(camp, "Sage", "Observer", 0.0)
    assert _forecast(camp, "Observer", "Berry3", "BerryGrove") == ["you may take it (not stealing)"]

    camp.run_query("MATCH (p:Person {name: 'Rowan'}) SET p.guard = true")
    camp.run_query(
        "MATCH (o:Person {name: 'Rowan'}), (c:Person {name: 'Observer'}) "
        "CREATE (o)-[:WITNESSED {crime: 'theft', victim: 'Reed', item: 'Fish1', at: 0.0}]->(c)"
    )
    assert why_hostile(camp, "Rowan", "Observer", at=0.0) == {
        "label": "criminal", "text": "Rowan is a guard and saw Observer commit theft (Reed's Fish1)"}


def test_a_new_rule_is_just_a_file(camp, tmp_path, monkeypatch):
    """新规则只写 .pl：一个新包规定「夜里拿走无主的东西也算偷（偷的是镇上的）」，引擎一行不改。"""
    import content
    from ownership import crimes_seen
    from test_combat import _stand

    pack = tmp_path / "night_law"
    (pack / "rules").mkdir(parents=True)
    (pack / "pack.json").write_text('{"name": "night_law"}')
    (pack / "rules" / "night.pl").write_text(
        "night :- now(T), T > 50.\n"
        "after(take(A, I, _), _{op: link, a: W, a_label: 'Person', b: A, b_label: 'Person', rel: 'WITNESSED',\n"
        "                      props: _{crime: night_taking, victim: town, item: I}}) :-\n"
        "    night, \\+ owner(I, _), sees(W, A).\n"
    )
    for src in ("base", "rpg", "streets", "village"):
        (tmp_path / src).symlink_to(content.CONTENT_ROOT / src)
    monkeypatch.setattr(content, "CONTENT_ROOT", tmp_path)
    content.reload_content(["base", "night_law"])
    try:
        from registry import get_registry
        from relations import consequences

        me = _stand(camp, "Observer", "BerryGrove")
        _stand(camp, "Sage", "BerryGrove", near=me)
        params = {"c": "Observer", "i": "Berry2", "l": "BerryGrove"}
        assert get_registry().execute(camp, "pick_up", **params)[0]
        consequences(camp, "Observer", "pick_up", params, at=60.0)
        assert crimes_seen(camp, "Sage", "Observer")[0]["crime"] == "night_taking"
    finally:
        monkeypatch.undo()
        content.reload_content()


# ---------------------------------------------------------------------
# 东西易手、送礼、买卖、打人之后（content/base/rules/ownership.pl、combat.pl）
# ---------------------------------------------------------------------

def _effects(facts, event):
    return answers(facts, "E", f"after({event}, E)")


def _feels(effects):
    return sorted((e["a"], e["b"], e["delta"]) for e in effects if e["op"] == "feel")


def test_what_you_hold_and_nobody_owns_becomes_yours():
    facts = person("Ash") + rel("HAS_ITEM", "Ash", "Axe") + rel("HAS_ITEM", "Ash", "Knife") + rel("OWNS", "Sage", "Knife")
    owns = [e for e in _effects(facts, "wait('Ash')") if e["op"] == "link"]
    assert [(e["a"], e["b"], e["rel"]) for e in owns] == [("Ash", "Axe", "OWNS")]


def test_gifts_hand_over_what_is_yours_and_warm_the_receiver():
    facts = person("Ash") + person("Sage") + rel("OWNS", "Ash", "Pie")
    eff = _effects(facts, "give('Ash', 'Pie', 'Sage', 'Camp')")
    assert [(e["op"], e["a"]) for e in eff if e["op"] in ("link", "unlink")] == [("unlink", "Ash"), ("link", "Sage")]
    assert _feels(eff) == [("Sage", "Ash", 3)]
    # 偷来的东西转手还是原主的；还给失主再加一笔
    back = _effects(person("Ash") + person("Sage") + rel("OWNS", "Sage", "Pie"), "give('Ash', 'Pie', 'Sage', 'Camp')")
    assert not [e for e in back if e["op"] in ("link", "unlink")]
    assert _feels(back) == [("Sage", "Ash", 3), ("Sage", "Ash", 3)]


def test_a_players_gift_is_trust_and_says_so():
    facts = person("Hero", is_player=True) + person("Sage", trust_toward_player=1)
    eff = _effects(facts, "give('Hero', 'Pie', 'Sage', 'Camp')")
    assert _feels(eff) == [("Sage", "Hero", 2)]
    assert [e["text"] for e in eff if e["op"] == "note"] == ["Sage trust → 3"]


def test_a_deal_moves_the_thing_and_both_sides_like_it():
    facts = person("Ash") + person("Reed") + rel("OWNS", "Reed", "Axe")
    eff = _effects(facts, "buy('Ash', 'Axe', 'Reed', 'Market')")
    assert [(e["op"], e["a"], e["b"]) for e in eff if e["op"] in ("link", "unlink")] == [
        ("unlink", "Reed", "Axe"), ("link", "Ash", "Axe")]
    assert _feels(eff) == [("Ash", "Reed", 1), ("Reed", "Ash", 1)]


def test_an_unprovoked_attack_is_seen_and_remembered():
    from prolog_engine import fact

    facts = (person("Ash") + person("Bram", hp=30) + person("Cole") + person("Dana")
             + rel("ALLY", "Bram", "Cole") + [fact("sees", "Dana", "Ash")])
    eff = _effects(facts, "attack('Ash', 'Bram', 'Camp')")
    assert _feels(eff) == [("Bram", "Ash", -4), ("Cole", "Ash", -3), ("Dana", "Ash", -2)]
    assert sorted(e["a"] for e in eff if e["op"] == "link") == ["Bram", "Dana"]
    report = next(e for e in eff if e["op"] == "report")
    assert report == {"op": "report", "crime": True, "witnesses": ["Dana"]}
    assert [e["text"] for e in eff if e["op"] == "note"] == [
        "Bram has 30 HP left", "an unprovoked attack: witnesses will remember"]
    # 被惹了才还手：不算犯罪
    provoked = person("Ash", strike_provoked="attacked me") + person("Bram", hp=30)
    eff = _effects(provoked, "attack('Ash', 'Bram', 'Camp')")
    assert next(e for e in eff if e["op"] == "report")["crime"] is False
    assert not [e for e in eff if e["op"] == "link"]
