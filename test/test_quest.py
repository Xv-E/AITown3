"""任务线（quest/）：校验、PDDL 验证、作者循环。全部不碰数据库，也不调用真的模型。"""

from __future__ import annotations

import copy
import json

import pytest

from compiler import Snapshot
from quest.author import author_questline
from quest.digest import WorldDigest
from quest.spec import KnownWorld, QuestlinePatch, validate
from quest.verify import verify


def _camp() -> Snapshot:
    snap = Snapshot()
    snap.objects.update(
        Camp=("Location", {}),
        Forest=("Location", {}),
        Traveler=("Person", {"hp": 100, "is_player": True, "role": "player"}),
        Mara=("Person", {"hp": 100, "role": "npc"}),
    )
    snap.edges["ADJACENT_TO"] = {("Camp", "Forest"), ("Forest", "Camp")}
    snap.location.update(Traveler="Camp", Mara="Camp")
    return snap


KNOWN = KnownWorld(
    regions={"Camp", "Forest"}, persons={"Traveler", "Mara"}, items=set(),
    player="Traveler", archetypes={"firewood"},
)

GOOD = {
    "title": "The Old Mine",
    "premise": "A stranger has set up camp in the abandoned mine.",
    "hook": "Mara saw smoke rising past the forest.",
    "regions": [{"name": "OldMine", "next_to": "Forest", "description": "A collapsed mine.", "w": 16, "h": 12}],
    "persons": [{"name": "Hollis", "located_at": "OldMine", "personality": "wary", "goal": "hide the ledger"}],
    "items": [
        {"name": "Crowbar", "located_at": "Forest", "item_kind": "tool"},
        {"name": "Lantern", "held_by": "Hollis", "item_kind": "tool"},
        {"name": "Strongbox", "located_at": "OldMine", "item_kind": "box", "portable": False,
         "actions": [{"id": "pry_open", "label": "Pry it open",
                      "requires": [{"op": "has_item", "person": "@actor", "item": "Crowbar"}],
                      "effects": [{"op": "set_prop", "entity": "@target", "prop": "opened", "value": True}]}]},
    ],
    "clues": [{"name": "SmokeRumor", "text": "Someone lives in the old mine.", "known_by": ["Mara"]}],
    "stages": [
        {"id": "s1", "title": "Who is in the mine?", "objective": "Find whoever is in the old mine and talk to them.",
         "giver": "Mara", "complete_when": [{"op": "has_rel", "a": "@player", "b": "Hollis", "rel": "KNOWS"}]},
        {"id": "s2", "title": "The strongbox", "objective": "Open Hollis's strongbox.",
         "giver": "Hollis", "complete_when": [{"op": "prop_eq", "entity": "Strongbox", "prop": "opened", "value": True}],
         "reward": [{"op": "unlink", "a": "Hollis", "a_label": "Person", "b": "Lantern", "b_label": "Item",
                     "rel": "HAS_ITEM"},
                    {"op": "link", "a": "@player", "a_label": "Person", "b": "Lantern", "b_label": "Item",
                     "rel": "HAS_ITEM"}]},
        {"id": "s3", "title": "Bring it back", "objective": "Give the crowbar back to Mara.",
         "giver": "Mara", "complete_when": [{"op": "has_item", "person": "Mara", "item": "Crowbar"}],
         "news": "The mystery of the old mine is solved."},
    ],
}


def _patch(**changes) -> QuestlinePatch:
    data = copy.deepcopy(GOOD)
    data.update(changes)
    return QuestlinePatch.from_dict(data)


def test_a_good_questline_validates():
    assert validate(_patch(), KNOWN) == []


@pytest.mark.parametrize("change, needle", [
    ({"stages": GOOD["stages"][:1]}, "need 2-6 stages"),
    ({"persons": [{"name": "Mara", "located_at": "Camp"}]}, "already exists"),
    ({"regions": [{"name": "OldMine", "next_to": "Atlantis"}]}, "next_to must be an existing region"),
    ({"items": [{"name": "Crowbar", "held_by": "Nobody"}]}, "is not a person"),
    ({"clues": [{"name": "SmokeRumor", "text": "x", "known_by": []}]}, "known_by"),
])
def test_bad_questlines_are_explained(change, needle):
    problems = validate(_patch(**change), KNOWN)
    assert any(needle in p for p in problems), problems


def test_stage_conditions_use_the_shared_rules():
    stages = copy.deepcopy(GOOD["stages"])
    stages[0]["complete_when"] = [{"op": "prop_eq", "entity": "@player", "prop": "location", "value": "OldMine"}]
    assert any("not a property" in p for p in validate(_patch(stages=stages), KNOWN))


def test_the_planner_proves_the_player_can_finish_it():
    result = verify(_camp(), _patch(), "Traveler")
    assert result.ok, result.reason
    steps = [name for stage in result.walkthrough for name, _ in stage]
    assert "talk" in steps and "interact" in steps and "give" in steps


def test_an_unreachable_stage_is_pinpointed():
    items = copy.deepcopy(GOOD["items"])
    items[0]["located_at"] = "OldMine"
    items[0] = {**items[0], "located_at": "", "held_by": "Hollis"}  # the crowbar is out of reach
    result = verify(_camp(), _patch(items=items), "Traveler")
    assert not result.ok and result.failed_stage == 2, result.reason
    assert "can't be completed" in result.reason


def test_a_stage_that_is_already_true_is_rejected():
    stages = copy.deepcopy(GOOD["stages"])
    stages[0]["complete_when"] = [{"op": "person_at", "person": "Mara", "location": "Camp"}]
    result = verify(_camp(), _patch(stages=stages), "Traveler")
    assert not result.ok and result.failed_stage == 1 and "already true" in result.reason


def _digest() -> WorldDigest:
    return WorldDigest(
        player="Traveler",
        regions=[{"name": "Camp", "next_to": ["Forest"]}, {"name": "Forest", "next_to": ["Camp"]}],
        persons=[{"name": "Traveler", "location": "Camp"}, {"name": "Mara", "location": "Camp"}],
        archetypes={"firewood": {"item_kind": "wood", "portable": True}},
    )


def test_the_author_feeds_problems_back_until_it_works():
    bad = copy.deepcopy(GOOD)
    bad["regions"][0]["next_to"] = "Atlantis"
    answers = iter(["not json at all", json.dumps(bad), json.dumps(GOOD)])
    prompts = []

    def fake_llm(system: str, prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    result = author_questline(
        _digest(), "a mystery", llm=fake_llm,
        verify_fn=lambda p: verify(_camp(), p, "Traveler"), max_attempts=3,
    )
    assert result.ok and result.attempts == 3
    assert "not a JSON object" in prompts[1]
    assert "next_to must be an existing region" in prompts[2]
    assert "Theme from the designer: a mystery" in prompts[0]


def test_the_author_gives_up_with_the_last_problems():
    result = author_questline(
        _digest(), "", llm=lambda s, p: "{}",
        verify_fn=lambda p: pytest.fail("never verified"), max_attempts=2,
    )
    assert not result.ok and result.attempts == 2 and result.problems


def test_obvious_slips_are_fixed_not_bounced_back():
    from quest.spec import normalize

    items = copy.deepcopy(GOOD["items"])
    items[0]["archetype"] = "tool"
    items[0]["item_kind"] = ""
    stages = copy.deepcopy(GOOD["stages"])
    stages[0]["giver"] = "Mara (by the fire)"
    patch = _patch(items=items, stages=stages)
    notes = normalize(patch, KNOWN)
    assert patch.items[0].item_kind == "tool" and patch.items[0].archetype == ""
    assert patch.stages[0].giver == "Mara"
    assert len(notes) == 2 and validate(patch, KNOWN) == []


def test_the_player_may_start_holding_something():
    items = copy.deepcopy(GOOD["items"])
    items[0] = {"name": "Crowbar", "held_by": "@player", "item_kind": "tool"}
    patch = _patch(items=items)
    from quest.spec import normalize

    normalize(patch, KNOWN)
    assert patch.items[0].held_by == "Traveler" and validate(patch, KNOWN) == []


def test_a_stage_already_met_by_the_previous_one_is_rejected():
    stages = copy.deepcopy(GOOD["stages"])
    stages[0]["reward"] = [{"op": "spawn_item", "name": "MineKey", "item_kind": "key", "held_by": "@player"}]
    stages[1]["complete_when"] = [
        {"op": "has_item", "person": "@player", "item": "MineKey"},
        {"op": "has_rel", "a": "@player", "b": "Hollis", "rel": "KNOWS"},
    ]
    result = verify(_camp(), _patch(stages=stages), "Traveler")
    assert not result.ok and result.failed_stage == 2 and "completes the moment stage 1 does" in result.reason


def test_a_complete_package_brings_its_own_place_people_and_things():
    from quest.spec import QuestlinePatch, completeness_problems

    bare = QuestlinePatch.from_dict({"title": "Lost Seal", "stages": [{"id": "s1", "title": "Ask", "objective": "x",
                                                                      "complete_when": []}]})
    problems = " | ".join(completeness_problems(bare))
    assert "new region" in problems and "new people" in problems and "items" in problems and "giver" in problems

    full = QuestlinePatch.from_dict({
        "title": "Lost Seal",
        "regions": [{"name": "OldMine", "next_to": "Forest"}],
        "persons": [{"name": "Hollis", "located_at": "OldMine", "routine": [
            {"start": 6, "end": 18, "location": "OldMine", "activity": "dig"},
            {"start": 18, "end": 6, "location": "OldMine", "activity": "rest"}]}],
        "items": [{"name": "MineKey", "held_by": "Hollis"}],
        "stages": [{"id": "s1", "title": "Ask", "objective": "x", "giver": "Hollis", "complete_when": []}],
        "start": {"visit": ["OldMine"]},
    })
    assert completeness_problems(full) == []
    assert full.persons[0].routine[0]["activity"] == "dig", "routines survive parsing"


def test_the_draft_preview_details_every_new_thing():
    from director_view import draft_summary

    body = {"ok": True, "attempts": 2, "patch": {
        "title": "Lost Seal", "premise": "Someone stole the seal.",
        "regions": [{"name": "OldMine", "next_to": "Forest", "description": "A dark shaft."}],
        "persons": [{"name": "Hollis", "located_at": "OldMine", "personality": "gruff", "secret": "He took it.",
                     "routine": [{"start": 6, "end": 18, "location": "OldMine", "activity": "dig"}]}],
        "items": [{"name": "MineKey", "held_by": "Hollis", "item_kind": "key",
                   "actions": []}],
        "stages": [{"title": "Ask around", "giver": "Reed", "objective": "Talk to Hollis",
                    "reward": [{"op": "link", "a": "@player", "b": "MineKey", "rel": "HAS_ITEM"}],
                    "news": "Hollis confesses."}],
    }}
    summary = draft_summary(body)
    sections = {s["title"]: s["lines"] for s in summary["sections"]}
    assert sections["New places"] == ["OldMine (next to Forest): A dark shaft."]
    assert "    Secret: He took it." in sections["New people"]
    assert "    Day: 06-18 dig @ OldMine" in sections["New people"]
    assert sections["New items"] == ["MineKey (key) - held by Hollis."]
    assert sections["Stages"] == ["1. Ask around (from Reed): Talk to Hollis", "    Reward: gets MineKey",
                                  "    Then everyone hears: Hollis confesses."]


def test_nothing_appears_from_nowhere_in_a_questline():
    from quest.spec import conservation_problems

    stages = copy.deepcopy(GOOD['stages'])
    stages[1]['reward'] = [{'op': 'spawn_item', 'name': 'Gold', 'held_by': '@player'}]
    assert any('nothing may appear' in p for p in conservation_problems(_patch(stages=stages)))
    stages[1]['reward'] = [{'op': 'add_prop', 'entity': '@player', 'label': 'Person', 'prop': 'coins', 'delta': 5}]
    assert any('coins can only move' in p for p in conservation_problems(_patch(stages=stages)))
    stages[1]['reward'] += [{'op': 'add_prop', 'entity': 'Hollis', 'label': 'Person', 'prop': 'coins', 'delta': -5}]
    assert conservation_problems(_patch(stages=stages)) == [], "paid out of the giver's purse is fine"
