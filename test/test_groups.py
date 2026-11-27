"""群体（content/base/rules/groups.pl、group_life.pl）和街头经济（content/streets/rules/street.pl）。

大部分直接喂图形状的事实给规则（不需要 Neo4j）；最后几个在真实世界里跑。
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyswip")

from test_social_rules import answers, node, rel, person  # noqa: E402


def group(name, **props):
    return node("Faction", name, **props)


def member(p, g, **props):
    return rel("MEMBER_OF", p, g, **props)


def at(p, loc):
    return rel("LOCATED_AT", p, loc)


def attitude(facts, me, other):
    got = answers(facts, "[S, R]", f"attitude('{me}', '{other}', S, R)")
    return got[0][0], got[0][1]


def effects(facts, event, now=100.0):
    return answers(facts, "E", f"after({event}, E)", now=now)


def standing_changes(effs):
    return {(e["a"], e["b"]): e["delta"] for e in effs if e["op"] == "add_rel_prop" and e["rel"] == "STANDING"}


def notes(effs):
    return [e["text"] for e in effs if e["op"] == "note"]


def report(effs):
    out = {}
    for e in effs:
        if e["op"] == "report":
            out.update({k: v for k, v in e.items() if k != "op"})
    return out


# ---------------------------------------------------------------------
# 种类只是默认特性
# ---------------------------------------------------------------------

def test_kinds_are_trait_bundles_and_own_traits_add_to_them():
    facts = group("Lau", kind="family") + group("Tigers", kind="gang", traits='[{"id": "militant", "why": "x"}]')
    assert answers(facts, "x", "has('Lau', closed), has('Lau', feud)") == ["x"]
    assert answers(facts, "M", "mult('Tigers', escalate, M)") == [1.25]
    assert answers(facts, "[P, T]", "entry('Tigers', P, T)") == [["sponsor", 4.0]], "no trial item: falls back to sponsor"
    assert answers(group("Odd", kind="whatever"), "[P, T]", "entry('Odd', P, T)") == [["sponsor", 4.0]]
    wolves = group("Wolves", kind="beast")
    assert answers(wolves, "x", "has('Wolves', attacks_on_sight), has('Wolves', roving)") == ["x"]


# ---------------------------------------------------------------------
# 亲疏
# ---------------------------------------------------------------------

def test_family_outweighs_rival_crews():
    facts = (person("Ash") + person("Bram") + group("Lau", kind="family") + group("Reds", kind="crew") + group("Blues", kind="crew")
             + member("Ash", "Lau", loyalty=8) + member("Bram", "Lau", loyalty=8)
             + member("Ash", "Reds", loyalty=6) + member("Bram", "Blues", loyalty=6)
             + rel("FACTION_REL", "Reds", "Blues", stance="rival"))
    score, reasons = attitude(facts, "Ash", "Bram")
    assert score > 1.5 and reasons[0] == "both of Lau (family)"
    assert "your Reds is rival with their Blues" in reasons


def test_rival_gangs_lean_away_and_turf_makes_it_worse():
    facts = (person("Ash") + person("Bram") + group("Reds", kind="gang", scale="small") + group("Blues", kind="gang")
             + member("Ash", "Reds", loyalty=9) + member("Bram", "Blues")
             + rel("FACTION_REL", "Blues", "Reds", stance="hostile"))
    score, _ = attitude(facts, "Ash", "Bram")
    assert score == -3.6
    turf = facts + at("Ash", "Square") + node("Location", "Square", controlled_by="Reds")
    score, reasons = attitude(turf, "Ash", "Bram")
    assert score == -4.6 and reasons[-1] == "they are on your Reds's ground"


def test_a_strangers_standing_counts():
    facts = (person("Ash") + person("Bram") + group("Reds", scale="small") + member("Ash", "Reds", loyalty=10)
             + rel("STANDING", "Bram", "Reds", value=6.0))
    assert attitude(facts, "Ash", "Bram") == (1.8, ["Reds thinks of them as trusted"])


def test_belonging_is_inherited_up_the_tree():
    facts = (person("Ash") + group("Lau", kind="family") + group("Town", kind="town") + rel("PART_OF", "Lau", "Town")
             + member("Ash", "Lau", loyalty=8))
    assert answers(facts, "[G, L, V]", "member('Ash', G, L, V)") == [["Lau", 8, ""], ["Town", 4.0, "Lau"]]


def test_prompt_lines_name_groups_relations_and_the_rule_of_loyalty():
    facts = (person("Ash") + person("Bram") + group("Lau", kind="family", goal="Keep the stall")
             + member("Ash", "Lau", loyalty=8) + member("Bram", "Lau"))
    lines = answers(facts, "T", "context('Ash', 'Bram', T)")
    assert lines[0] == "Your factions:"
    assert lines[1] == "- Lau (family, medium; member, loyalty 8/10) — goal: Keep the stall"
    assert "You and Bram are both of Lau." in lines
    assert lines[-1].startswith("Faction loyalty shapes this talk")
    situation = answers(facts + at("Ash", "Yard") + at("Bram", "Yard"), "T", "context('Ash', T)")
    assert situation[0].startswith("Your factions (their goals are yours too")
    assert any(t.startswith("Bram: you lean friendly — both of Lau") for t in situation)
    assert answers(person("Ash") + person("Bram"), "T", "context('Ash', 'Bram', T)") == [], "no groups, nothing to say"


# ---------------------------------------------------------------------
# 对人做的事传到群体
# ---------------------------------------------------------------------

def _rivals():
    return (person("Ash") + person("Bram") + group("Reds", scale="small") + group("Blues", scale="small")
            + member("Bram", "Blues") + rel("FACTION_REL", "Reds", "Blues", stance="hostile"))


def test_an_attack_angers_the_victims_group_and_pleases_its_enemies():
    got = standing_changes(effects(_rivals(), "attack('Ash', 'Bram', 'Square')"))
    assert got == {("Ash", "Blues"): -3.0, ("Ash", "Reds"): 1.5}


def test_betrayal_inside_your_own_group_hurts_more():
    facts = _rivals() + member("Ash", "Blues")
    assert standing_changes(effects(facts, "attack('Ash', 'Bram', 'Square')"))[("Ash", "Blues")] == -4.5


def test_gifts_talks_and_kept_promises_raise_standing_and_promote_recruits():
    assert standing_changes(effects(_rivals(), "give('Ash', 'Knife', 'Bram', 'Square')"))[("Ash", "Blues")] == 1.0
    assert standing_changes(effects(_rivals(), "deed('Ash', 'Bram', 2.0, 'talk')"))[("Ash", "Blues")] == 1.0
    recruit = _rivals() + member("Ash", "Blues", role="recruit") + rel("STANDING", "Ash", "Blues", value=6.5)
    effs = effects(recruit, "give('Ash', 'Knife', 'Bram', 'Square')")
    assert any(e["op"] == "set_rel_prop" and e["prop"] == "role" and e["value"] == "member" for e in effs)
    assert "Ash is now a full member of Blues" in notes(effs)


def test_pacifists_condemn_any_violence():
    facts = _rivals() + group("Monks", traits='["pacifist"]')
    effs = effects(facts, "attack('Ash', 'Bram', 'Square')")
    assert standing_changes(effs)[("Ash", "Monks")] == -1.0
    assert any(n.startswith("Monks condemns the violence against Bram") for n in notes(effs))


# ---------------------------------------------------------------------
# 入会
# ---------------------------------------------------------------------

def _join(facts, who="Hero", g="Guild", via=""):
    return effects(facts, f"ask_to_join('{who}', '{g}', '{via}')")


def _hall(kind="guild", trust=0, **gprops):
    return (person("Hero", is_player=True) + person("Mira", trust_toward_player=trust) + group("Guild", kind=kind, **gprops)
            + member("Mira", "Guild", role="master", loyalty=8) + at("Hero", "Hall") + at("Mira", "Hall"))


def test_an_open_group_needs_standing_and_a_member_here():
    town = _hall("town")
    assert report(_join(town))["notes"] == ["standing +0.0 is below the +1.0 they require"]
    ok = _join(town + rel("STANDING", "Hero", "Guild", value=2.0))
    assert report(ok)["outcome"] == "joined"
    link = next(e for e in ok if e["op"] == "link" and e["rel"] == "MEMBER_OF")
    assert link["props"] == {"role": "recruit", "loyalty": 5.0, "sponsor": ""}
    away = person("Hero", is_player=True) + person("Mira") + group("Guild", kind="town") + member("Mira", "Guild") \
        + rel("STANDING", "Hero", "Guild", value=2.0)
    assert report(_join(away))["notes"] == ["no member of Guild is here to ask"]


def test_a_family_is_closed():
    assert "Guild is a family; outsiders cannot simply join" in report(_join(_hall("family")))["notes"]


def test_a_sponsor_must_like_you_and_rivals_raise_the_bar():
    hall = _hall() + rel("STANDING", "Hero", "Guild", value=5.0)
    assert report(_join(hall))["notes"] == ["nobody here is willing to vouch for you"]
    liked = _hall(trust=3) + rel("STANDING", "Hero", "Guild", value=5.0)
    got = _join(liked)
    assert report(got)["outcome"] == "joined" and "Hero joined Guild as a recruit (sponsored by Mira)" in notes(got)
    rival = liked + group("Blues") + member("Hero", "Blues") + rel("FACTION_REL", "Guild", "Blues", stance="rival")
    assert report(_join(rival))["check"]["threshold"] == 6.0


def test_a_trial_starts_a_quest_and_handing_it_over_admits():
    gang = _hall("gang", trial='{"item": "cash", "text": "Bring a red packet."}') + rel("STANDING", "Hero", "Guild", value=5.0)
    got = _join(gang)
    assert report(got)["outcome"] == "trial_started" and report(got)["giver"] == "Mira"
    assert {(e["op"], e.get("rel")) for e in got} >= {("merge_node", None), ("link", "GIVES_QUEST"), ("link", "ACCEPTED_QUEST")}
    quest = (node("Quest", "TrialGuild", kind="trial", faction="Guild", helper="Hero", item="cash", state="active")
             + rel("GIVES_QUEST", "Mira", "TrialGuild") + node("Item", "Cash1", item_kind="cash"))
    done = effects(gang + quest, "give('Hero', 'Cash1', 'Mira', 'Hall')")
    assert any(e["op"] == "set_prop" and e.get("value") == "completed" for e in done)
    assert any(e["op"] == "link" and e["rel"] == "MEMBER_OF" and e["b"] == "Guild" for e in done)
    assert "your trial is already under way" in report(_join(gang + quest))["notes"]


def test_joining_shifts_how_allies_and_rivals_see_you_and_leaving_costs():
    town = _hall("town") + rel("STANDING", "Hero", "Guild", value=2.0) + group("Blues") \
        + rel("FACTION_REL", "Guild", "Blues", stance="rival")
    assert standing_changes(_join(town))[("Hero", "Blues")] == -1.0
    left = effects(town + member("Hero", "Guild"), "leave_group('Hero', 'Guild')")
    assert standing_changes(left) == {("Hero", "Guild"): -3.0} and report(left)["ok"] is True
    assert report(effects(town, "leave_group('Hero', 'Guild')"))["ok"] is False


# ---------------------------------------------------------------------
# 群体演化（每 6 个游戏小时；默认一天 300 模拟秒 → 一轮 75 秒）
# ---------------------------------------------------------------------

def phase(facts, name, prev=70.0, now=80.0):
    from prolog_engine import fact

    return answers(facts + [fact("g_tick", prev, now)], "E", f"tick({name}, E)", now=now)


def strengths(effs):
    return {(e["a"], e["b"]): e["value"] for e in effs if e["op"] == "set_rel_prop" and e["rel"] == "CONTROLS"}


def test_presence_strengthens_ground_and_absence_lets_it_slip():
    facts = (person("Ash") + group("Reds", kind="gang") + member("Ash", "Reds", loyalty=10) + at("Ash", "Square")
             + node("Location", "Square") + node("Location", "Market") + rel("CONTROLS", "Reds", "Market", strength=2.0))
    assert strengths(phase(facts, "territory")) == {("Reds", "Square"): 0.6, ("Reds", "Market"): 1.7}
    assert phase(facts, "territory", 80.0, 90.0) == [], "only once every six game hours"
    fast = [f for f in facts if "g_node('Faction'" not in f] + group("Reds", kind="gang", traits='["expansionist"]')
    assert strengths(phase(fast, "territory"))[("Reds", "Square")] == 0.9
    flat = facts + node("Location", "Flat", tags="home", parent="Square")
    moved = [f for f in flat if f != at("Ash", "Square")[0]] + at("Ash", "Flat")
    assert ("Reds", "Flat") not in strengths(phase(moved, "territory")), "a flat someone sleeps in is not turf"


def test_rivals_wear_each_other_down_and_control_changes_hands():
    facts = (person("Ash") + person("Bram") + group("Reds") + group("Blues") + node("Location", "Square")
             + member("Ash", "Reds", loyalty=10) + member("Bram", "Blues", loyalty=10) + at("Ash", "Square") + at("Bram", "Square")
             + rel("CONTROLS", "Reds", "Square", strength=5.0) + rel("CONTROLS", "Blues", "Square", strength=5.0)
             + rel("FACTION_REL", "Reds", "Blues", stance="hostile"))
    assert strengths(phase(facts, "territory")) == {("Reds", "Square"): 4.6, ("Blues", "Square"): 4.6}
    held = (group("Reds") + group("Blues") + node("Location", "Square", controlled_by="Blues")
            + rel("CONTROLS", "Reds", "Square", strength=4.0) + rel("CONTROLS", "Blues", "Square", strength=2.0))
    effs = phase(held, "control")
    assert any(e["op"] == "set_prop" and e["value"] == "Reds" for e in effs)
    assert {"op": "announce", "text": "Reds drove Blues out of Square.", "source": "factions"} in effs


def test_shared_ground_turns_rivals_hostile_unless_they_are_kin():
    base = (group("Reds") + group("Blues") + rel("CONTROLS", "Reds", "Square", strength=3.0)
            + rel("CONTROLS", "Blues", "Square", strength=3.0))
    rivals = base + rel("FACTION_REL", "Reds", "Blues", stance="rival", tension=5.0)
    effs = phase(rivals, "tension")
    assert any(e["op"] == "link" and e["props"]["stance"] == "hostile" for e in effs)
    calm = [f for f in rivals if "g_node('Faction', 'Reds')" not in f] + group("Reds", traits='["pacifist"]')
    assert [e["value"] for e in phase(calm, "tension")] == [5.75], "pacifists escalate slower"
    nested = rivals + rel("PART_OF", "Reds", "Blues")
    assert phase(nested, "tension") == [], "a family inside its town never builds tension"


def test_loyalty_grows_at_home_and_fades_on_hostile_ground():
    facts = (person("Ash") + person("Bram") + group("Reds") + group("Blues") + member("Ash", "Reds") + member("Bram", "Reds")
             + at("Ash", "Square") + at("Bram", "Market") + rel("FACTION_REL", "Reds", "Blues", stance="hostile")
             + node("Location", "Square", controlled_by="Reds") + node("Location", "Market", controlled_by="Blues")
             + rel("CONTROLS", "Reds", "Square", strength=5.0))
    got = {e["a"]: e["value"] for e in phase(facts, "loyalty")}
    assert got == {"Ash": 6.15, "Bram": 5.7}


def test_the_disloyal_splinter_and_friends_from_both_sides_band_together():
    facts = (person("Ash") + person("Bram") + person("Cole") + group("Reds", kind="gang")
             + member("Ash", "Reds", loyalty=2) + member("Bram", "Reds", loyalty=4) + member("Cole", "Reds", loyalty=9)
             + rel("FRIENDS_WITH", "Ash", "Bram") + at("Ash", "Square"))
    ext = [e for e in phase(facts, "emerge") if e["op"] == "extend"]
    assert ext and ext[0]["ctx"]["type"] == "splinter" and ext[0]["ctx"]["founders"][0] == "Ash"
    cooled = facts + node("WorldMeta", "WorldMeta", group_last_emerge_at=10.0)
    assert phase(cooled, "emerge") == [], "a new group only every few rounds"
    pact = (person("Ash") + person("Bram") + group("Reds") + group("Blues") + member("Ash", "Reds", loyalty=4)
            + member("Bram", "Blues", loyalty=4) + at("Ash", "Square") + at("Bram", "Square") + rel("FRIENDS_WITH", "Bram", "Ash"))
    ctx = [e for e in phase(pact, "emerge") if e["op"] == "extend"][0]["ctx"]
    assert ctx["type"] == "coalition" and ctx["from"] == ["Blues", "Reds"]


def test_the_rule_written_story_ties_traits_to_what_happened():
    got = answers(group("Reds"), "S", "story(_{type: splinter, parent: 'Reds', parent_kind: gang, founders: ['Ash', 'Bram'], "
                                      "location: 'Square'}, S)")[0]
    assert got["name"] == "FreeReds" and got["kind"] == "gang"
    assert got["traits"][0] == {"id": "vengeful", "why": "born from Ash's grudge against Reds"}


def test_a_controlling_group_recruits_the_drifting_and_grows():
    facts = (person("Cole") + group("Reds", kind="gang") + at("Cole", "Square") + node("Location", "Square", controlled_by="Reds"))
    effs = phase(facts, "recruit")
    assert effs[0]["op"] == "link" and effs[0]["a"] == "Cole" and effs[0]["b"] == "Reds"
    three = group("Reds", kind="gang", scale="small") + sum((person(p) + member(p, "Reds") for p in ("A", "B", "C")), [])
    assert any(e.get("value") == "medium" for e in phase(three, "rise"))


# ---------------------------------------------------------------------
# 街头经济（streets 包）
# ---------------------------------------------------------------------

def cash(who, n, start=0):
    return sum((node("Item", f"{who}Cash{i}", item_kind="cash") + rel("HAS_ITEM", who, f"{who}Cash{i}")
                for i in range(start, start + n)), [])


def _streets(lau_cash=6, dues=2.5):
    return (person("MrsLau") + person("AhKit") + person("DragonLeung") + person("MadDogWai")
            + group("SilverDragons", kind="gang") + group("BlackTigers", kind="gang")
            + member("DragonLeung", "SilverDragons", role="boss", loyalty=9) + member("AhKit", "SilverDragons", role="runner")
            + member("MadDogWai", "BlackTigers")
            + node("Item", "LauNoodleStall", owner="MrsLau", protection_fee=2.0, stock=4)
            + rel("LOCATED_AT", "LauNoodleStall", "NoodleAlley") + node("Location", "NoodleAlley", controlled_by="SilverDragons")
            + rel("EXTORTS", "SilverDragons", "LauNoodleStall", dues=dues)
            + at("AhKit", "NoodleAlley") + at("MadDogWai", "NoodleAlley") + cash("MrsLau", lau_cash))


def transfers(effs):
    return [(e["from"], e["to"], e["count"]) for e in effs if e["op"] == "transfer"]


def test_collecting_your_own_racket_sends_a_cut_up_to_the_boss():
    effs = effects(_streets(), "collect_dues('AhKit')")
    assert transfers(effs) == [("MrsLau", "AhKit", 2), ("AhKit", "DragonLeung", 1)]
    assert {"op": "set_rel_prop", "a": "SilverDragons", "a_label": "Faction", "b": "LauNoodleStall", "b_label": "Item",
            "rel": "EXTORTS", "prop": "dues", "value": 0.5} in effs
    assert report(effs)["collected"] == 2 and "MrsLau paid $2 ($1 goes up to DragonLeung)" in notes(effs)
    assert any(e["rel"] == "CONTROLS" and e["delta"] == 0.5 for e in effs if e["op"] == "add_rel_prop")


def test_an_owner_who_cannot_pay_and_a_civilian_who_cannot_collect():
    assert notes(effects(_streets(lau_cash=0), "collect_dues('AhKit')")) == ["MrsLau can't pay right now (owes 2.5)"]
    assert notes(effects(_streets() + at("MrsLau", "NoodleAlley"), "collect_dues('MrsLau')")) == \
        ["MrsLau has no gang to collect for"]


def test_muscling_in_takes_a_rivals_money_and_starts_trouble():
    effs = effects(_streets(), "collect_dues('MadDogWai')")
    assert transfers(effs) == [("MrsLau", "MadDogWai", 2)]
    assert standing_changes(effs) == {("MadDogWai", "SilverDragons"): -3.0}
    assert any(e["op"] == "link" and e["rel"] == "FACTION_REL" and e["props"]["tension"] == 2.0 for e in effs)


def test_police_on_the_spot_block_collection():
    cops = _streets() + person("SergeantTam") + group("DistrictPolice", kind="authority") \
        + member("SergeantTam", "DistrictPolice") + at("SergeantTam", "NoodleAlley")
    effs = effects(cops, "collect_dues('AhKit')")
    assert transfers(effs) == [] and standing_changes(effs) == {("AhKit", "DistrictPolice"): -1.0}


def test_dues_build_up_for_whoever_holds_the_ground_and_unpaid_ones_get_smashed():
    effs = phase(_streets(), "dues")
    assert [e["value"] for e in effs if e.get("prop") == "dues"] == [3.0], "six hours of a $2/day fee"
    lost = [f for f in _streets() if "controlled_by" not in f] + node("Location", "NoodleAlley", controlled_by="BlackTigers")
    assert any(e["op"] == "unlink" and e["a"] == "SilverDragons" for e in phase(lost, "dues")), "no ground, no dues"
    behind = _streets(dues=6.5)
    smash = phase(behind, "overdue", 290.0, 310.0)
    assert any(e["op"] == "set_prop" and e["prop"] == "stock" and e["value"] == 0 for e in smash)
    assert any(e["op"] == "add_prop" and e["entity"] == "MrsLau" and e["delta"] == -10 for e in smash)
    assert phase(behind, "overdue") == [], "only once a day"


def test_wages_run_out_with_the_bosss_money_and_rent_falls_behind():
    gang = (person("Boss") + person("A") + person("B") + group("Crew", wage=1) + member("Boss", "Crew", role="boss")
            + member("A", "Crew") + member("B", "Crew") + cash("Boss", 1))
    effs = phase(gang, "wages", 290.0, 310.0)
    assert transfers(effs) == [("Boss", "A", 1)] and "Boss couldn't pay B (Crew)" in notes(effs)
    book = (person("Lord") + person("T1") + person("T2") + cash("T1", 2)
            + node("Item", "Book", item_kind="rent_book", landlord="Lord", rent=1, tenants=["T1", "T2"], arrears='{"T1": 2}'))
    effs = phase(book, "rent", 290.0, 310.0)
    assert transfers(effs) == [("T1", "Lord", 1)]
    import json

    assert json.loads(next(e["value"] for e in effs if e.get("prop") == "arrears")) == {"T2": 1}


def test_trouble_is_story_material():
    pleas = answers(_streets(dues=5.0), "P", "plea(P)")
    assert pleas[0]["who"] == "MrsLau" and pleas[0]["need"] == "cash" and pleas[0]["title"] == "MrsLau owes SilverDragons"


# ---------------------------------------------------------------------
# 真实世界
# ---------------------------------------------------------------------

@pytest.fixture
def streets(world):
    from director import build_from_blueprint, load_world

    import content

    content.reload_content(["base", "rpg", "streets"])
    build_from_blueprint(world, load_world("triad_streets"), clear=True)
    yield world
    content.reload_content()


def _money(world):
    return world.run_query("MATCH (c:Item {item_kind: 'cash'}) RETURN count(c) AS n")[0]["n"]


def test_the_streets_run_for_days_and_money_never_appears_or_vanishes(streets):
    from rulebook import happen, tick

    ctl = streets.run_query("MATCH (l:Location {name: 'NoodleAlley'}) RETURN l.controlled_by AS c")[0]["c"]
    assert ctl == "SilverDragons", "the builder lets the rules write who holds what"
    before = _money(streets)
    out = {"events": []}
    for day in range(3):
        for k in range(4):
            got = tick(streets, day * 300.0 + k * 75.0 + 1.0, day * 300.0 + (k + 1) * 75.0 + 1.0)
            out["events"] += got.get("events") or []
    holders = streets.run_query(
        "MATCH (:Person)-[:HAS_ITEM]->(c:Item {item_kind: 'cash'}) RETURN count(c) AS n")[0]["n"]
    assert _money(streets) == before == holders, "every note is still in somebody's pocket"
    collector = streets.run_query(
        "MATCH (p:Person)-[:MEMBER_OF]->(:Faction {name: 'SilverDragons'}) WHERE p.name <> 'DragonLeung' "
        "RETURN p.name AS p LIMIT 1")[0]["p"]
    happen(streets, "collect_dues", collector)
    assert _money(streets) == before


def test_the_ui_gets_cards_and_a_money_panel(streets):
    from group_views import cards, summary
    from rulebook import ask

    got = {c["name"]: c for c in cards(streets, "DragonLeung")}
    assert got["SilverDragons"]["member"] is True and got["SilverDragons"]["territory"]
    assert got["BlackTigers"]["join"]["can_join"] is False
    assert "SilverDragons (gang, boss)" in summary(streets, "DragonLeung")
    panel = ask(streets, "C", "panel('DragonLeung', economy, C)")[0]
    assert panel["money_supply"] == _money(streets) and panel["businesses"]


def test_person_factions_that_are_not_groups_become_groups(world):
    from director import build_from_blueprint, load_world
    from combat import hostility

    build_from_blueprint(world, load_world("riverford"), clear=True)
    beast = world.run_query("MATCH (p:Person {faction: 'beast'})-[:MEMBER_OF]->(f:Faction) RETURN p.name AS p, f.kind AS k")
    assert beast and beast[0]["k"] == "beast"
    someone = world.run_query("MATCH (p:Person) WHERE coalesce(p.faction, '') = '' AND NOT coalesce(p.is_player, false) "
                              "RETURN p.name AS p LIMIT 1")[0]["p"]
    assert hostility(world, beast[0]["p"], someone) == "faction"


# ---------------------------------------------------------------------
# 世界数据里的群体
# ---------------------------------------------------------------------

def test_template_world_groups_load():
    from director.blueprint import EventBlueprint
    from director.world_loader import load_world

    bp = load_world("town_daily_life")
    by = {f.name: f for f in bp.factions}
    assert set(by) == {"EmberCamp", "Axemen", "BerryCircle", "OldCampKin"}
    assert by["Axemen"].kind == "gang" and by["Axemen"].parent == "EmberCamp"
    assert by["Axemen"].relations[0]["stance"] == "rival"
    again = EventBlueprint.from_dict({k: v for k, v in bp.to_dict().items() if k != "counts"})
    assert len(again.factions) == 4


def test_normalize_drops_dangling_refs_and_keeps_any_kind():
    from director.blueprint import EventBlueprint

    bp = EventBlueprint.from_dict({
        "locations": [{"name": "Camp"}],
        "persons": [{"name": "Ash", "located_at": "Camp"}],
        "factions": [
            {"name": "Kin", "kind": "family", "parent": "Nowhere",
             "members": [{"name": "Ash", "loyalty": 99}, {"name": "Ghost"}],
             "relations": [{"with": "Nobody", "stance": "hostile"}, {"with": "Pack", "stance": "weird"}]},
            {"name": "Pack", "kind": "Wolf Pack"},
        ],
    })
    kin = next(f for f in bp.factions if f.name == "Kin")
    assert kin.scale == "small" and kin.parent is None
    assert [m["name"] for m in kin.members] == ["Ash"] and kin.members[0]["loyalty"] == 10
    assert kin.relations == [{"with": "Pack", "stance": "neutral", "reason": ""}]
    assert next(f for f in bp.factions if f.name == "Pack").kind == "wolf_pack", "kinds come from content, not a fixed list"
