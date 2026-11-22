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
             + [fact("g_sees", w, "Ash") for w in ("Rowan", "Sage", "Hero")])
    lines = answers(facts, "T", "forecast(take('Ash', 'Knife', 'Grove'), T)")
    assert lines[0] == "taking it is stealing"
    # 守卫看见了就当罪犯；失主 -2 再 -5 = -7，跌破仇恨线；玩家的感受不替他推演
    assert any(t.startswith("then Rowan would turn hostile (Rowan is a guard and saw Ash commit theft") for t in lines)
    assert any(t.startswith("then Sage would turn hostile (Sage hates Ash (feeling -7))") for t in lines)
    assert not any("Hero" in t for t in lines)
    assert answers(facts, "x", "\\+ hyp(_)") == ["x"], "the assumptions are gone afterwards"


def test_after_a_theft_the_effects_are_plain_graph_ops():
    from prolog_engine import fact

    facts = person("Ash") + person("Sage") + rel("OWNS", "Sage", "Knife") + [fact("g_sees", "Sage", "Ash")]
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
    from rulebook import ask

    return ask(world, "T", f"forecast(take('{me}', '{item}', '{loc}'), T)")


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
    set_owner(camp, "Berry3", "Sage")
    camp.run_query("MATCH (a:Person {name: 'Sage'}), (b:Person {name: 'Observer'}) MERGE (a)-[:ALLY]->(b) MERGE (b)-[:ALLY]->(a)")
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
             + rel("ALLY", "Bram", "Cole") + [fact("g_sees", "Dana", "Ash")])
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


# ---------------------------------------------------------------------
# 时间过去（content/base/rules/time.pl）
# ---------------------------------------------------------------------

def _tick(facts, prev, now):
    from prolog_engine import fact

    return answers(facts + [fact("g_tick", prev, now)], "E", "tick(E)", now=now)


def test_feelings_drift_back_but_allies_keep_a_floor():
    facts = (person("Ash") + person("Bram") + person("Cole")
             + rel("FEELS", "Ash", "Bram", value=5) + rel("FEELS", "Bram", "Ash", value=-4)
             + rel("FEELS", "Ash", "Cole", value=3) + rel("ALLY", "Ash", "Cole")
             + rel("TENSION", "Bram", "Cole", level=2))
    eff = _tick(facts, 20.0, 26.0)
    got = sorted((e["rel"], e["a"], e["b"], e["delta"]) for e in eff if e["op"] == "add_rel_prop")
    assert got == [("FEELS", "Ash", "Bram", -1), ("FEELS", "Bram", "Ash", 1), ("TENSION", "Bram", "Cole", -1)]
    assert _tick(facts, 26.0, 30.0) == [], "once per period"


def test_people_heal_unless_they_were_just_hit():
    facts = (person("Ash", hp=40, max_hp=100) + person("Bram", hp=40, attacked_at=24.0)
             + person("Cole", hp=100, max_hp=100))
    heal = [(e["entity"], e["delta"], e["max"]) for e in _tick(facts, 20.0, 26.0) if e["op"] == "add_prop"]
    assert heal == [("Ash", 8, 100)]


# ---------------------------------------------------------------------
# 盟友与联盟（content/base/rules/alliances.pl，content/rpg/rules/alliance.pl）
# ---------------------------------------------------------------------

def _phase(facts, phase, prev=20.0, now=26.0):
    from prolog_engine import fact

    return answers(facts + [fact("g_tick", prev, now)], "E", f"tick({phase}, E)", now=now)


def _ops(effects):
    return [(e["op"], e.get("a") or e.get("entity") or e.get("name"), e.get("b"), e.get("value")) for e in effects]


def test_mutual_liking_makes_allies_and_a_band():
    facts = person("Ash") + person("Bram") + rel("FEELS", "Ash", "Bram", value=7) + rel("FEELS", "Bram", "Ash", value=6)
    assert _ops(_phase(facts, "bonds")) == [
        ("link", "Ash", "Bram", None), ("link", "Bram", "Ash", None),
        ("merge_node", "Ash's Band", None, None), ("link", "Ash", "Ash's Band", None), ("link", "Bram", "Ash's Band", None)]
    assert _phase(facts, "bonds", 26.0, 30.0) == [], "only every two game hours"


def test_joining_an_existing_band_or_bringing_two_bands_closer():
    base = person("Ash") + person("Bram") + rel("FEELS", "Ash", "Bram", value=7) + rel("FEELS", "Bram", "Ash", value=7)
    one = base + node("Alliance", "Reds") + rel("MEMBER_OF", "Ash", "Reds")
    assert _ops(_phase(one, "bonds"))[2:] == [("link", "Bram", "Reds", None)]
    two = one + node("Alliance", "Blues") + rel("MEMBER_OF", "Bram", "Blues") + rel("STANCE", "Reds", "Blues", value=1)
    assert _ops(_phase(two, "bonds"))[2:] == [("set_rel_prop", "Reds", "Blues", 4), ("set_rel_prop", "Blues", "Reds", 4)]


def test_falling_out_breaks_the_bond_and_empties_the_band():
    facts = (person("Ash") + person("Bram") + person("Cole") + node("Alliance", "Reds")
             + rel("ALLY", "Ash", "Bram") + rel("FEELS", "Ash", "Bram", value=-1)
             + rel("MEMBER_OF", "Ash", "Reds") + rel("MEMBER_OF", "Bram", "Reds") + rel("FEELS", "Bram", "Ash", value=2))
    assert _ops(_phase(facts, "bonds")) == [("unlink", "Ash", "Bram", None)]
    assert _ops(_phase(facts, "leave")) == [("unlink", "Ash", "Reds", None)], "Ash no longer likes the band"
    lonely = person("Ash") + node("Alliance", "Reds") + rel("MEMBER_OF", "Ash", "Reds")
    assert _ops(_phase(lonely, "dissolve")) == [("delete_entity", "Reds", None, None)]


def test_bands_feel_about_each_other_like_their_members_do():
    facts = (person("Ash") + person("Bram") + node("Alliance", "Reds") + node("Alliance", "Blues")
             + rel("MEMBER_OF", "Ash", "Reds") + rel("MEMBER_OF", "Bram", "Blues")
             + rel("FEELS", "Ash", "Bram", value=-7) + rel("FEELS", "Bram", "Ash", value=-6))
    assert _ops(_phase(facts, "stance")) == [("set_rel_prop", "Blues", "Reds", -7), ("set_rel_prop", "Reds", "Blues", -7)]
    at_war = facts + rel("STANCE", "Reds", "Blues", value=-7)
    assert answers(at_war, "x", "alliances_at_war('Ash', 'Bram')") == ["x"]
    assert answers(at_war, "x", "alliances_at_war('Ash', 'Ash')") == []


def test_proposing_an_alliance_is_just_the_content_action(camp):
    from relations import are_allies, feeling, propose_alliance

    camp.run_query("MATCH (p:Person) WHERE p.name IN ['Sage', 'Reed'] SET p.trust_toward_player = 0")
    sage_loc = camp.run_query("MATCH (:Person {name: 'Sage'})-[:LOCATED_AT]->(l) RETURN l.name AS l")[0]["l"]
    camp.run_query(
        "MATCH (p:Person {name: 'Reed'}), (l:Location {name: $l}) OPTIONAL MATCH (p)-[r:LOCATED_AT]->() "
        "DELETE r MERGE (p)-[:LOCATED_AT]->(l)", l=sage_loc)
    ok, msg = propose_alliance(camp, "Reed", "Sage")
    assert not ok and "doesn't trust you enough" in msg
    camp.run_query("MATCH (a:Person {name: 'Sage'}), (b:Person {name: 'Reed'}) MERGE (a)-[r:FEELS]->(b) SET r.value = 5")
    ok, msg = propose_alliance(camp, "Reed", "Sage")
    assert ok and are_allies(camp, "Reed", "Sage") and are_allies(camp, "Sage", "Reed")
    assert feeling(camp, "Sage", "Reed") == 6
    band = camp.run_query("MATCH (p:Person)-[:MEMBER_OF]->(g:Alliance) RETURN g.name AS g, collect(p.name) AS who")
    assert band and band[0]["g"] == "Reed's Band" and sorted(band[0]["who"]) == ["Reed", "Sage"]


def test_time_turns_friends_into_a_band_in_the_real_world(camp):
    from rulebook import const, tick

    camp.run_query(
        "MATCH (a:Person {name: 'Sage'}), (b:Person {name: 'Reed'}) "
        "MERGE (a)-[r:FEELS]->(b) SET r.value = 8 MERGE (b)-[s:FEELS]->(a) SET s.value = 8")
    p = const("decay_every")
    tick(camp, p - 1.0, p + 1.0)
    rows = camp.run_query("MATCH (:Person {name: 'Sage'})-[:MEMBER_OF]->(g:Alliance)<-[:MEMBER_OF]-(:Person {name: 'Reed'}) "
                          "RETURN g.name AS g")
    assert rows, "decayed to 7, still mutual ≥ 6: allies in one band"


# ---------------------------------------------------------------------
# NPC 看到敌人时（content/base/rules/react.pl）
# ---------------------------------------------------------------------

def _react(facts, me, **extra):
    from prolog_engine import fact

    more = [fact("busy_since", me, extra["busy_since"])] if "busy_since" in extra else []
    got = answers(facts + more, "C", f"once(react('{me}', C))")
    return got[0] if got else None


def _arena(**ash):
    from prolog_engine import fact

    return (person("Ash", **ash) + person("Bram", hp=50)
            + rel("LOCATED_AT", "Ash", "Camp") + rel("LOCATED_AT", "Bram", "Camp")
            + rel("ADJACENT_TO", "Camp", "Woods") + [fact("g_sees", "Bram", "Ash")])


def test_hit_back_when_attacked():
    c = _react(_arena(hp=50, attacked_by="Bram", attacked_at=99.0), "Ash")
    assert c["goal"] == "Defeat enemy" and c["target"] == "Bram" and c["why"] == "attacked me"
    assert c["plan"] == [["attack", ["Ash", "Bram", "Camp"]]] and c["text"] == "Fights Bram (attacked me)"


def test_mere_hatred_only_fights_when_clearly_stronger():
    hate = rel("FEELS", "Ash", "Bram", value=-8)
    assert _react(_arena(hp=50) + hate, "Ash") is None, "as strong as Bram: keeps away"
    assert _react(_arena(hp=50, power_level=5) + hate, "Ash")["goal"] == "Defeat enemy"


def test_run_when_losing_and_hurt():
    c = _react(_arena(hp=10, max_hp=100, attacked_by="Bram", attacked_at=99.0), "Ash")
    assert c["goal"] == "Flee" and c["target"] == "Woods" and c["plan"] == [["travel", ["Ash", "Camp", "Woods"]]]


def test_busy_people_only_drop_things_when_hit_after_starting():
    facts = _arena(hp=50, attacked_by="Bram", attacked_at=99.0)
    assert _react(facts, "Ash", busy_since=99.5) is None
    assert _react(facts, "Ash", busy_since=98.0)["goal"] == "Defeat enemy"
