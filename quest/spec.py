"""
任务线补丁：往现有世界里加的东西（新区域 / 人 / 物品 / 线索）+ 分阶段的任务。

纯数据 + 校验，不碰数据库。完成条件 / 奖励用和内容包动作同一套原语，
@player 指玩家；校验规则与物品动作共用（ephemeral_action_dsl.OpsChecker）。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set

NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9]{1,31}$")
MIN_STAGES, MAX_STAGES = 2, 6
REGION_SIZE = (8, 30)


@dataclass
class RegionSpec:
    name: str
    next_to: str
    description: str = ""
    tags: str = ""
    w: int = 20
    h: int = 14


@dataclass
class PersonSpec:
    name: str
    located_at: str
    personality: str = ""
    background: str = ""
    goal: str = ""
    mood: str = "calm"
    secret: str = ""
    speech_style: str = ""
    role: str = "npc"
    # 每日日程（同 routine.py 的槽位：start / end 小时、location、activity）；没有就只靠自己决策
    routine: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class PersonUpdate:
    """给已有的人加动机 / 秘密（不改名字、位置）。"""

    name: str
    goal: str = ""
    secret: str = ""
    background: str = ""


@dataclass
class ItemSpec:
    name: str
    located_at: str = ""
    held_by: str = ""
    item_kind: str = ""
    description: str = ""
    portable: bool = True
    value: int = 1
    archetype: str = ""
    actions: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ClueSpec:
    name: str
    text: str
    known_by: List[str] = field(default_factory=list)
    kind: str = "fact"  # fact（真的） | rumor（有人相信，未必真）


@dataclass
class StageSpec:
    id: str
    title: str
    objective: str
    complete_when: List[Dict[str, Any]]
    giver: str = ""
    reward: List[Dict[str, Any]] = field(default_factory=list)
    news: str = ""


@dataclass
class QuestlinePatch:
    title: str
    premise: str = ""
    hook: str = ""
    regions: List[RegionSpec] = field(default_factory=list)
    persons: List[PersonSpec] = field(default_factory=list)
    person_updates: List[PersonUpdate] = field(default_factory=list)
    items: List[ItemSpec] = field(default_factory=list)
    clues: List[ClueSpec] = field(default_factory=list)
    stages: List[StageSpec] = field(default_factory=list)

    # ----- (反)序列化 -----

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "QuestlinePatch":
        def many(key: str, cls, fields: Set[str]) -> List[Any]:
            out = []
            for raw in data.get(key) or []:
                if isinstance(raw, dict):
                    out.append(cls(**{k: v for k, v in raw.items() if k in fields}))
            return out

        return QuestlinePatch(
            title=str(data.get("title") or "").strip(),
            premise=str(data.get("premise") or "").strip(),
            hook=str(data.get("hook") or "").strip(),
            regions=many("regions", RegionSpec, _fields(RegionSpec)),
            persons=many("persons", PersonSpec, _fields(PersonSpec)),
            person_updates=many("person_updates", PersonUpdate, _fields(PersonUpdate)),
            items=many("items", ItemSpec, _fields(ItemSpec)),
            clues=many("clues", ClueSpec, _fields(ClueSpec)),
            stages=many("stages", StageSpec, _fields(StageSpec)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def quest_name(self) -> str:
        words = re.findall(r"[A-Za-z0-9]+", self.title) or ["Questline"]
        return "".join(w[:1].upper() + w[1:] for w in words)[:40]

    def new_names(self) -> List[str]:
        return [x.name for group in (self.regions, self.persons, self.items, self.clues) for x in group]


def _fields(cls) -> Set[str]:
    return set(cls.__dataclass_fields__)


# ----- 物品属性：写进快照（验证）和写进数据库（应用）共用 -----

def item_props(item: ItemSpec) -> Dict[str, Any]:
    """和 director 建世界时一致：先套原型，再写作者给的字段；动作放进 script.actions。"""
    props: Dict[str, Any] = {}
    if item.archetype:
        from content import apply_archetype

        spec = apply_archetype({"archetype": item.archetype})
        for k in ("item_kind", "portable", "value", "description"):
            if spec.get(k) is not None:
                props[k] = spec[k]
        for k in ("effects_on", "accepts_from"):
            if spec.get(k) is not None:
                props[k] = json.dumps(spec[k], ensure_ascii=False)
        script = spec.get("script") if isinstance(spec.get("script"), dict) else {}
        for k, v in (script.get("initial_state") or {}).items():
            if isinstance(v, (str, int, float, bool)):
                props.setdefault(k, v)
        props["archetype"] = item.archetype
    if item.item_kind:
        props["item_kind"] = item.item_kind
    if item.description:
        props["description"] = item.description
    props["portable"] = bool(item.portable)
    props.setdefault("value", int(item.value or 0))
    if item.actions:
        props["script"] = json.dumps({"interactive": True, "actions": item.actions}, ensure_ascii=False)
    return props


# ----- 校验 -----

@dataclass
class KnownWorld:
    """校验需要知道的现有世界（名字层面）。"""

    regions: Set[str]
    persons: Set[str]
    items: Set[str]
    player: str
    archetypes: Set[str]
    other_names: Set[str] = field(default_factory=set)  # 事实 / 传闻 / 任务等，也不许重名


def validate(patch: QuestlinePatch, known: KnownWorld) -> List[str]:
    """返回所有问题（写给 LLM 看的说明）；空列表 = 通过。"""
    from ephemeral_action_dsl import OpsChecker, validate_action_spec

    errors: List[str] = []
    if not patch.title:
        errors.append("title is required")
    if not (MIN_STAGES <= len(patch.stages) <= MAX_STAGES):
        errors.append(f"need {MIN_STAGES}-{MAX_STAGES} stages (got {len(patch.stages)})")

    existing = known.regions | known.persons | known.items | known.other_names
    seen: Set[str] = set()
    for name in patch.new_names():
        if not NAME_RE.match(name or ""):
            errors.append(f"name {name!r} must be CamelCase letters/digits, e.g. OldMine")
        elif name in existing:
            errors.append(f"{name} already exists in the world; new things need new names")
        elif name in seen:
            errors.append(f"{name} is used twice")
        seen.add(name)
    if patch.quest_name() in existing:
        errors.append(f"title gives quest name {patch.quest_name()} which already exists")

    regions = known.regions | {r.name for r in patch.regions}
    persons = known.persons | {p.name for p in patch.persons}
    items = known.items | {i.name for i in patch.items}
    all_names = regions | persons | items

    placed = set(known.regions)
    for r in patch.regions:
        if r.next_to not in placed:
            errors.append(f"region {r.name}: next_to must be an existing region (or one listed before it)")
        placed.add(r.name)
        if not (REGION_SIZE[0] <= int(r.w) <= REGION_SIZE[1] and REGION_SIZE[0] <= int(r.h) <= REGION_SIZE[1]):
            errors.append(f"region {r.name}: w/h must be {REGION_SIZE[0]}-{REGION_SIZE[1]}")
    for p in patch.persons:
        if p.located_at not in regions:
            errors.append(f"person {p.name}: located_at {p.located_at!r} is not a region")
        for i, slot in enumerate(p.routine or []):
            where = f"person {p.name}: routine slot {i + 1}"
            if not isinstance(slot, dict):
                errors.append(f"{where} must be an object")
                continue
            if slot.get("location") not in regions:
                errors.append(f"{where}: location {slot.get('location')!r} is not a region")
            try:
                ok = 0 <= float(slot.get("start")) <= 24 and 0 <= float(slot.get("end")) <= 24
            except (TypeError, ValueError):
                ok = False
            if not ok:
                errors.append(f"{where}: start and end are hours 0-24")
        if p.role != "npc":
            errors.append(f"person {p.name}: new people are NPCs (role npc); the player already exists")
    for u in patch.person_updates:
        if u.name not in known.persons or u.name == known.player:
            errors.append(f"person_updates: {u.name!r} must be an existing NPC")

    for it in patch.items:
        where = f"item {it.name}"
        if bool(it.located_at) == bool(it.held_by):
            errors.append(f"{where}: give exactly one of located_at (region) or held_by (person)")
        if it.located_at and it.located_at not in regions:
            errors.append(f"{where}: located_at {it.located_at!r} is not a region")
        if it.held_by and it.held_by not in persons:
            errors.append(f"{where}: held_by {it.held_by!r} is not a person")
        if it.held_by and not it.portable:
            errors.append(f"{where}: a fixture (portable false) can't be held")
        if it.archetype and it.archetype.lower() not in known.archetypes:
            errors.append(f"{where}: unknown archetype {it.archetype!r}")
        for spec in it.actions:
            action, err = validate_action_spec(
                spec, host_item=it.name, entity_names=all_names, archetypes=known.archetypes
            )
            if action is None:
                errors.append(f"{where}: {err}")

    from content import get_content

    if get_content().conservation:
        errors += conservation_problems(patch)

    for c in patch.clues:
        if c.kind not in ("fact", "rumor"):
            errors.append(f"clue {c.name}: kind is fact or rumor")
        bad = [n for n in c.known_by if n not in persons]
        if not c.known_by or bad:
            errors.append(f"clue {c.name}: known_by must list people who know it (bad: {bad or 'none given'})")

    check = OpsChecker(placeholders=("player",), entity_names=all_names, archetypes=known.archetypes)
    ids: Set[str] = set()
    for i, st in enumerate(patch.stages):
        where = f"stage {i + 1} ({st.id})"
        if not st.id or st.id in ids:
            errors.append(f"{where}: needs a unique id")
        ids.add(st.id)
        if not st.objective:
            errors.append(f"{where}: objective (what the player is told to do) is required")
        if st.giver and st.giver not in persons:
            errors.append(f"{where}: giver {st.giver!r} is not a person")
        if not st.complete_when:
            errors.append(f"{where}: complete_when needs at least one condition")
        err = check.conditions(st.complete_when, f"{where}.complete_when") or check.effects(
            st.reward, f"{where}.reward"
        )
        if err:
            errors.append(err)
    return errors


def normalize(patch: QuestlinePatch, known: KnownWorld) -> List[str]:
    """就地修掉一看就懂的笔误（不改意图），返回改了什么。校验之前调用。"""
    notes: List[str] = []
    persons = known.persons | {p.name for p in patch.persons}
    for it in patch.items:
        if it.archetype and it.archetype.lower() not in known.archetypes:
            if not it.item_kind:
                it.item_kind = it.archetype
                notes.append(f"item {it.name}: archetype {it.archetype!r} is not one; used it as item_kind")
            else:
                notes.append(f"item {it.name}: dropped unknown archetype {it.archetype!r}")
            it.archetype = ""
        if it.held_by in ("@player", "player"):
            it.held_by = known.player
    # 没写在哪、但奖励 / 物品动作会按这个名字生成出来的：它本来就是「后来才出现」的，不用列
    spawned = {
        op.get("name")
        for ops in [st.reward for st in patch.stages] + [a.get("effects") or [] for it in patch.items for a in it.actions]
        for op in ops
        if isinstance(op, dict) and op.get("op") == "spawn_item"
    }
    for it in list(patch.items):
        if not it.located_at and not it.held_by and it.name in spawned:
            patch.items.remove(it)
            notes.append(f"item {it.name}: created later by spawn_item, removed from the starting items")
    for c in patch.clues:
        c.known_by = [known.player if n in ("@player", "player") else n for n in c.known_by]
    for st in patch.stages:
        giver = st.giver.strip()
        if giver in ("@player", "player", known.player):
            st.giver = ""
            notes.append(f"stage {st.id}: the player can't give themselves a quest; giver cleared")
            continue
        if giver and giver not in persons:
            head = re.split(r"[\s(,]", giver, maxsplit=1)[0]
            if head in persons:
                notes.append(f"stage {st.id}: giver {giver!r} -> {head}")
                st.giver = head
    return notes


def substitute(value: Any, placeholder: str, replacement: str) -> Any:
    """把条件 / 效果里的 @placeholder（含 @placeholder.prop）换成别的，递归处理 any / when。"""
    token = f"@{placeholder}"
    if isinstance(value, str):
        if value == token or value.startswith(token + "."):
            return replacement + value[len(token):]
        return value
    if isinstance(value, list):
        return [substitute(v, placeholder, replacement) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, placeholder, replacement) for k, v in value.items()}
    return value


# 「完整任务包」：手动让导演写任务时，要它自带新的地点、人物、物品（不只是借用现有的）
MIN_NEW_PEOPLE, MAX_NEW_PEOPLE = 1, 3


def completeness_problems(patch: QuestlinePatch) -> List[str]:
    out: List[str] = []
    if not patch.regions:
        out.append("add at least one new region (a place this story happens in)")
    if not (MIN_NEW_PEOPLE <= len(patch.persons) <= MAX_NEW_PEOPLE):
        out.append(f"add {MIN_NEW_PEOPLE}-{MAX_NEW_PEOPLE} new people who belong to this story")
    if not patch.items:
        out.append("add the items this story needs (at least one)")
    for p in patch.persons:
        if len(p.routine or []) < 2:
            out.append(f"person {p.name}: give a daily routine of 2-4 slots (start, end, location, activity)")
    if patch.stages and not patch.stages[0].giver:
        out.append("the first stage needs a giver (who asks the player for help)")
    return out


def conservation_problems(patch: QuestlinePatch) -> List[str]:
    """守恒：奖励和物品动作不能凭空生成东西；给钱只能从别人的钱里转。"""
    out: List[str] = []
    groups = [(f"stage {st.id} reward", st.reward) for st in patch.stages] + [
        (f"item {it.name} action {a.get('id')}", a.get("effects") or [])
        for it in patch.items for a in it.actions if isinstance(a, dict)
    ]
    for where, ops in groups:
        coins = 0.0
        for op in ops or []:
            if not isinstance(op, dict):
                continue
            if op.get("op") == "spawn_item":
                out.append(f"{where}: nothing may appear from nowhere (no spawn_item). Put the item in the world "
                           "(located_at a region or held_by someone) and hand it over with unlink/link, "
                           "or let the player make it")
            if op.get("op") == "add_prop" and op.get("prop") == "coins":
                try:
                    coins += float(op.get("delta") or 0) * float(op.get("scale", 1))
                except (TypeError, ValueError):
                    pass
        if coins > 0:
            out.append(f"{where}: coins can only move between people - take as many from the giver as the player gets")
    return out
