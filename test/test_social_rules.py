"""社会规则（rules/social.pl）：敌对的原因与优先级、偷窃、拿东西之前推演后果。

前半部分只用手写的事实（不需要 Neo4j）；后半部分在真实世界里比对「推演」和「真做之后」。
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyswip")


def _solve(facts, goal):
    from prolog_engine import solve
    from social_rules import _params
    from prolog_engine import fact

    return solve(list(facts) + [fact("now", 100.0)] + [fact("param", k, v) for k, v in _params().items()], goal)


def _people(*names, **flags):
    from prolog_engine import fact

    out = []
    for n in names:
        out += [fact("person", n), fact("alive", n), fact("faction", n, flags.get(n, ""))]
    return out


def _why(facts, a, b):
    rows = _solve(facts, f"hostility('{a}', '{b}', W), W =.. [K|_]")
    return str(rows[0]["K"]) if rows else ""


def test_reasons_come_in_priority_order():
    from prolog_engine import fact

    base = _people("Ash", "Bram") + [fact("feels", "Ash", "Bram", -9)]
    assert _why(base, "Ash", "Bram") == "hatred"
    hit = base + [fact("attacked_by", "Ash", "Bram", 95.0)]
    assert _why(hit, "Ash", "Bram") == "attacked_me", "being attacked outranks hatred"
    stale = base + [fact("attacked_by", "Ash", "Bram", 10.0)]
    assert _why(stale, "Ash", "Bram") == "hatred", "an old attack is forgotten"
    assert _why(base, "Bram", "Ash") == "", "hostility is one-way"


def test_monsters_fight_everyone_and_the_dead_fight_nobody():
    from prolog_engine import fact

    facts = _people("Wolf", "Sage", Wolf="monster")
    assert _why(facts, "Wolf", "Sage") == "faction" and _why(facts, "Sage", "Wolf") == "faction"
    dead = [f for f in facts if f != fact("alive", "Wolf")]
    assert _why(dead, "Sage", "Wolf") == ""


def test_npcs_feel_about_the_player_through_trust():
    from prolog_engine import fact

    facts = _people("Sage", "Hero") + [fact("player", "Hero"), fact("trust", "Sage", -7),
                                        fact("feels", "Sage", "Hero", 5)]
    assert _why(facts, "Sage", "Hero") == "hatred", "trust, not the FEELS edge, is how an NPC feels about the player"


def test_alliances_at_war():
    from prolog_engine import fact

    facts = _people("Ash", "Bram") + [fact("in_alliance", "Ash", "Reds"), fact("in_alliance", "Bram", "Blues"),
                                       fact("stance", "Reds", "Blues", -6)]
    assert _why(facts, "Ash", "Bram") == "war"


def test_theft_needs_a_living_owner_who_is_not_your_ally():
    from prolog_engine import fact

    facts = _people("Sage", "Ash") + [fact("owns", "Sage", "Knife")]
    goal = "theft('Ash', 'Knife', O)"
    assert _solve(facts, goal)[0]["O"] == "Sage"
    assert not _solve(facts + [fact("ally", "Sage", "Ash")], goal)
    assert not _solve([f for f in facts if f != fact("alive", "Sage")], goal)
    assert not _solve(facts, "theft('Sage', 'Knife', O)"), "your own things"


def test_foreseeing_a_theft_names_who_would_turn_hostile_and_why():
    from prolog_engine import fact

    facts = _people("Ash", "Sage", "Rowan", "Hero") + [
        fact("owns", "Sage", "Knife"), fact("guard", "Rowan"), fact("player", "Hero"),
        fact("feels", "Sage", "Ash", -2),
        fact("sees", "Rowan", "Ash"), fact("sees", "Sage", "Ash"), fact("sees", "Hero", "Ash"),
    ]
    rows = _solve(facts, "findall([W, K], (consequence(take('Ash', 'Knife'), turns_hostile(W, Why)), "
                         "Why =.. [K|_]), L)")
    got = {(str(w), str(k)) for w, k in rows[0]["L"]}
    # 守卫看见了就当罪犯；失主 -2 再 -5 = -7，跌破仇恨线；玩家的感受不替他推演
    assert got == {("Rowan", "criminal"), ("Sage", "hatred")}
    assert _solve(facts, "\\+ hyp(_)"), "the assumptions are gone afterwards"


# ---------------------------------------------------------------------
# 真实世界：推演 == 真做之后
# ---------------------------------------------------------------------

@pytest.fixture
def camp(world):
    from director import build_from_blueprint, load_world

    build_from_blueprint(world, load_world("town_daily_life"), clear=True)
    return world


def test_the_forecast_matches_what_happens(camp):
    from combat import hostility
    from ownership import set_owner, witnesses
    from relations import consequences
    from registry import get_registry
    from social_rules import foresee_take
    from test_combat import _stand

    camp.run_query("MATCH (p:Person {name: 'Rowan'}) SET p.guard = true")
    set_owner(camp, "Berry3", "Sage")
    me = _stand(camp, "Observer", "BerryGrove")
    _stand(camp, "Rowan", "BerryGrove", near=me)
    watchers = witnesses(camp, "Observer")
    assert "Rowan" in watchers

    forecast = foresee_take(camp, "Observer", "Berry3", watchers=watchers)
    assert forecast["theft"] and forecast["victim"] == "Sage"
    predicted = {h["who"]: h["label"] for h in forecast["turns_hostile"]}
    assert predicted.get("Rowan") == "criminal" and "Rowan would turn hostile" in forecast["turns_hostile"][0]["text"]

    params = {"c": "Observer", "i": "Berry3", "l": "BerryGrove"}
    ok, why = get_registry().execute(camp, "pick_up", **params)
    assert ok, why
    consequences(camp, "Observer", "pick_up", params, at=0.0)
    actual = {w: hostility(camp, w, "Observer", at=0.0) for w in watchers}
    assert {w: l for w, l in actual.items() if l} == predicted


def test_an_allys_things_are_not_stolen_and_reasons_read_well(camp):
    from ownership import set_owner
    from relations import _link_allies
    from social_rules import foresee_take, why_hostile

    set_owner(camp, "Berry3", "Sage")
    _link_allies(camp, "Sage", "Observer", 0.0)
    assert foresee_take(camp, "Observer", "Berry3", watchers=[])["theft"] is False

    camp.run_query("MATCH (p:Person {name: 'Rowan'}) SET p.guard = true")
    camp.run_query(
        "MATCH (o:Person {name: 'Rowan'}), (c:Person {name: 'Observer'}) "
        "CREATE (o)-[:WITNESSED {crime: 'theft', victim: 'Reed', item: 'Fish1', at: 0.0}]->(c)"
    )
    why = why_hostile(camp, "Rowan", "Observer", at=0.0)
    assert why["label"] == "criminal"
    assert why["text"] == "Rowan is a guard and saw Observer commit theft (Reed's Fish1)"
