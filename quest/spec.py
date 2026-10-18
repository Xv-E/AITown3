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


# 时段：start.at 用这些词或 "22-4" 这样的小时区间
TIMES_OF_DAY = {"night": (21, 5), "dawn": (5, 8), "morning": (6, 12), "day": (7, 19),
                "afternoon": (12, 18), "dusk": (18, 21), "evening": (18, 23)}
PATTERN_EVENTS = ("enter", "pick_up", "drop", "talk", "do")


def parse_hours(at: str) -> Optional[tuple]:
    """'night' / '22-4' -> (起, 止) 小时；看不懂返回 None。"""
    at = (at or "").strip().lower()
    if at in TIMES_OF_DAY:
        return TIMES_OF_DAY[at]
    m = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", at)
    if m and 0 <= int(m.group(1)) <= 24 and 0 <= int(m.group(2)) <= 24:
        return int(m.group(1)), int(m.group(2))
    return None


def in_hours(hour: float, span: tuple) -> bool:
    start, end = span
    return start <= hour < end if start <= end else hour >= start or hour < end


@dataclass
class StartSpec:
    """玩家怎么接下这条任务线：任一条途径成立就算接下（之前任务线休眠，不推进阶段）。

    途径：听说 / 拿到 / 走到 / 任意条件 / 一串按顺序发生的事（pattern）。
    at：只有这个时段才算；omen：接下的那一刻发生的异象（一句话 + 改世界的效果）。
    都不写 = 一写进世界就开始（旧行为）。
    """

    hear: List[str] = field(default_factory=list)   # 线索：玩家听说了（KNOWS_FACT / BELIEVES）
    find: List[str] = field(default_factory=list)   # 物品：玩家拿到了（小纸条、信物……）
    visit: List[str] = field(default_factory=list)  # 地点：玩家走到了
    when: List[Dict[str, Any]] = field(default_factory=list)     # 条件（同阶段 complete_when），全部成立
    pattern: List[Dict[str, Any]] = field(default_factory=list)  # 玩家依次做了这些事（见 quest.trail）
    within_hours: float = 0.0  # pattern 首尾不超过这么多游戏小时（0 = 不限）
    at: str = ""               # 时段：night / dawn / dusk / "22-4" ……
    omen: Dict[str, Any] = field(default_factory=dict)  # {"text": "...", "effects": [...]}

    def empty(self) -> bool:
        return not (self.hear or self.find or self.visit or self.when or self.pattern)

    def planner_conditions(self) -> List[Dict[str, Any]]:
        """规划器能证明的那几条（听说靠对话里真的说出口，证明不了）。"""
        return [{"op": "has_item", "person": "@player", "item": i} for i in self.find] + [
            {"op": "person_at", "person": "@player", "location": r} for r in self.visit
        ]


LISTENER_PHASES = ("dormant", "active", "always")
LISTENER_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")


@dataclass
class ListenerSpec:
    """监听器：某种事发生了（on / sequence）、条件也成立（when / at），就做 then 里的事。

    active：什么时候在听 —— dormant（任务线还没被接下）/ active（进行中）/ stage:<阶段 id> / always。
    on：一种事件 {"event", "who", "what", "target", "contains"}，count 次（within_hours 内）才算；
        不写 on 也不写 sequence = 每拍只看 when。
    sequence：几种事件按顺序发生（同 start.pattern，可以带 times）。
    then：{"say": 一句话, "effects": [...], "start": true（接下任务线）, "nudge": {"who", "text"}}。
    once：只触发一次（接下任务线的默认 true）；cooldown_hours：两次之间至少隔多久。
    """

    id: str
    active: str = ""
    on: Dict[str, Any] = field(default_factory=dict)
    sequence: List[Dict[str, Any]] = field(default_factory=list)
    count: int = 1
    within_hours: float = 0.0
    when: List[Dict[str, Any]] = field(default_factory=list)
    at: str = ""
    cooldown_hours: float = 0.0
    once: Optional[bool] = None
    then: Dict[str, Any] = field(default_factory=dict)

    def phase(self) -> str:
        return self.active or ("dormant" if self.then.get("start") else "active")

    def fires_once(self) -> bool:
        return bool(self.then.get("start")) if self.once is None else bool(self.once)


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
    start: StartSpec = field(default_factory=StartSpec)
    listeners: List[ListenerSpec] = field(default_factory=list)

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
            start=_start(data.get("start")),
            listeners=[_listener(x) for x in data.get("listeners") or [] if isinstance(x, dict)],
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


def _listener(raw: Dict[str, Any]) -> ListenerSpec:
    def num(key: str, default: float) -> float:
        try:
            return float(raw.get(key) if raw.get(key) is not None else default)
        except (TypeError, ValueError):
            return -1.0  # 校验时报出来

    then = raw.get("then")
    once = raw.get("once")
    return ListenerSpec(
        id=str(raw.get("id") or "").strip(),
        active=str(raw.get("active") or "").strip(),
        on=dict(raw["on"]) if isinstance(raw.get("on"), dict) else {},
        sequence=[x for x in raw.get("sequence") or [] if isinstance(x, dict)],
        count=int(num("count", 1)),
        within_hours=num("within_hours", 0),
        when=[x for x in raw.get("when") or [] if isinstance(x, dict)],
        at=str(raw.get("at") or "").strip(),
        cooldown_hours=num("cooldown_hours", 0),
        once=None if once is None else bool(once),
        then=dict(then) if isinstance(then, dict) else {},
    )


def _start(raw: Any) -> StartSpec:
    if not isinstance(raw, dict):
        return StartSpec()

    def names(key: str) -> List[str]:
        v = raw.get(key) or []
        return [str(x).strip() for x in ([v] if isinstance(v, str) else v) if str(x).strip()]

    def dicts(key: str) -> List[Dict[str, Any]]:
        return [x for x in raw.get(key) or [] if isinstance(x, dict)]

    try:
        within = max(0.0, float(raw.get("within_hours") or 0))
    except (TypeError, ValueError):
        within = -1.0  # 校验时报出来
    omen = raw.get("omen")
    if isinstance(omen, str):
        omen = {"text": omen}
    return StartSpec(
        hear=names("hear"), find=names("find"), visit=names("visit"),
        when=dicts("when"), pattern=dicts("pattern"), within_hours=within,
        at=str(raw.get("at") or "").strip(), omen=dict(omen) if isinstance(omen, dict) else {},
    )


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

    errors += _start_problems(patch, known, regions, items, persons, all_names)
    errors += _listener_problems(patch, known, _event_names(patch, known, regions, items, persons, all_names))

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


def _start_problems(patch: QuestlinePatch, known: KnownWorld, regions: Set[str], items: Set[str],
                    persons: Set[str], all_names: Set[str]) -> List[str]:
    from ephemeral_action_dsl import OpsChecker

    st = patch.start
    out: List[str] = []
    clues = {c.name: c for c in patch.clues}
    for name in st.hear:
        clue = clues.get(name)
        if clue is None:
            out.append(f"start.hear {name!r} must be one of this questline's clues")
        elif known.player in clue.known_by:
            out.append(f"start.hear {name}: the player already knows it, so the questline would start at once")
        elif not [n for n in clue.known_by if n != known.player]:
            out.append(f"start.hear {name}: someone other than the player must know it to tell them")
    held = {i.name for i in patch.items if i.held_by == known.player}
    for name in st.find:
        if name not in items:
            out.append(f"start.find {name!r} is not an item")
        elif name in held:
            out.append(f"start.find {name}: the player already holds it, so the questline would start at once")
    for name in st.visit:
        if name not in regions:
            out.append(f"start.visit {name!r} is not a region")

    check = OpsChecker(placeholders=("player", "here"), entity_names=all_names, archetypes=known.archetypes)
    if st.when:
        err = check.conditions(st.when, "start.when")
        if err:
            out.append(err)
    names = _event_names(patch, known, regions, items, persons, all_names)
    for i, step in enumerate(st.pattern):
        where = f"start.pattern step {i + 1}"
        if step.get("event") not in PATTERN_EVENTS:
            out.append(f"{where}: event is one of {', '.join(PATTERN_EVENTS)}")
            continue
        out += _event_problems({"who": "@player", **step}, where, names, steps=True)
    if st.within_hours < 0:
        out.append("start.within_hours is a number of game hours (0 = no limit)")
    if st.at and parse_hours(st.at) is None:
        out.append(f"start.at {st.at!r}: use one of {', '.join(TIMES_OF_DAY)} or hours like \"22-4\"")
    if st.omen:
        if not str(st.omen.get("text") or "").strip():
            out.append("start.omen needs a text: what the player notices when it happens")
        err = check.effects(st.omen.get("effects") or [], "start.omen.effects")
        if err:
            out.append(err)
    return out


@dataclass
class _EventNames:
    regions: Set[str]
    items: Set[str]
    persons: Set[str]
    player: str
    clues: Set[str]
    stages: Set[str]
    listeners: Set[str]
    all_names: Set[str]
    actions: Optional[Set[str]]


def _event_names(patch, known, regions, items, persons, all_names) -> _EventNames:
    try:
        from registry import get_registry

        actions: Optional[Set[str]] = set(get_registry().names())
    except Exception:  # noqa: BLE001
        actions = None
    return _EventNames(
        regions=regions, items=items, persons=persons, player=known.player,
        clues={c.name for c in patch.clues} | set(known.other_names),
        stages={st.id for st in patch.stages}, listeners={x.id for x in patch.listeners},
        all_names=all_names, actions=actions,
    )


def _event_problems(spec: Dict[str, Any], where: str, n: _EventNames, *, steps: bool = False) -> List[str]:
    """一条事件描述（监听器的 on / sequence 一步 / start.pattern 一步）写得对不对。"""
    from .trail import EVENTS, PERSON_EVENTS

    out: List[str] = []
    event, what = spec.get("event"), str(spec.get("what") or "")
    if event not in EVENTS:
        return [f"{where}: event is one of {', '.join(EVENTS)}"]
    who = str(spec.get("who") or "anyone")
    if who not in ("@player", "npc", "anyone") and who not in n.persons:
        out.append(f"{where}: who is @player, npc, anyone or a person's name (got {who!r})")
    if event == "learn" and who not in ("@player", "anyone"):
        out.append(f"{where}: only the player's learning is tracked (who @player)")
    npcs = n.persons - {n.player}
    expect = {
        "enter": ("a region", n.regions), "pick_up": ("an item", n.items), "drop": ("an item", n.items),
        "talk": ("an NPC", npcs) if who == "@player" else ("a person", n.persons), "learn": ("a clue", n.clues),
        "time": ("a time of day", set(TIMES_OF_DAY)), "hour": ("an hour 0-23", {str(h) for h in range(24)}),
        "stage_done": ("a stage id", n.stages), "listener": ("a listener id", n.listeners),
    }
    if event == "do":
        if n.actions is not None and what not in n.actions:
            out.append(f"{where}: {what!r} is not an action")
    elif event in expect and (what or event in PERSON_EVENTS) and what not in expect[event][1]:
        out.append(f"{where}: {what!r} is not {expect[event][0]}")
    if spec.get("target") and spec["target"] not in n.all_names | n.stages:
        out.append(f"{where}: target {spec['target']!r} does not exist")
    if steps:
        try:
            ok = 1 <= int(spec.get("times") or 1) <= 5
        except (TypeError, ValueError):
            ok = False
        if not ok:
            out.append(f"{where}: times is 1-5")
    return out


def _listener_problems(patch: QuestlinePatch, known: KnownWorld, names: _EventNames) -> List[str]:
    from ephemeral_action_dsl import OpsChecker

    out: List[str] = []
    check = OpsChecker(placeholders=("player", "here", "actor"), entity_names=names.all_names,
                       archetypes=known.archetypes)
    seen: Set[str] = set()
    for i, ls in enumerate(patch.listeners):
        where = f"listener {ls.id or i + 1}"
        if not LISTENER_ID_RE.match(ls.id):
            out.append(f"{where}: needs an id (letters, digits, _)")
        elif ls.id in seen:
            out.append(f"{where}: id is used twice")
        seen.add(ls.id)
        phase = ls.phase()
        if phase not in LISTENER_PHASES and not (phase.startswith("stage:") and phase[6:] in names.stages):
            out.append(f"{where}: active is dormant, active, always or stage:<stage id>")
        if ls.then.get("start") and phase not in ("dormant", "always"):
            out.append(f"{where}: a listener that starts the questline must listen while it is dormant")
        if ls.on and ls.sequence:
            out.append(f"{where}: give on or sequence, not both")
        if ls.on:
            out += _event_problems(ls.on, f"{where}.on", names)
        for k, step in enumerate(ls.sequence):
            out += _event_problems(step, f"{where}.sequence step {k + 1}", names, steps=True)
        if not ls.on and not ls.sequence and not ls.when:
            out.append(f"{where}: listen for something (on / sequence) or watch conditions (when)")
        if not 1 <= ls.count <= 10:
            out.append(f"{where}: count is 1-10")
        if ls.within_hours < 0 or ls.cooldown_hours < 0:
            out.append(f"{where}: within_hours and cooldown_hours are game hours (0 = no limit)")
        if ls.at and parse_hours(ls.at) is None:
            out.append(f"{where}: at {ls.at!r}: use one of {', '.join(TIMES_OF_DAY)} or hours like \"22-4\"")
        if ls.when:
            err = check.conditions(ls.when, f"{where}.when")
            if err:
                out.append(err)
        then = ls.then
        if not any(then.get(k) for k in ("say", "effects", "start", "nudge")):
            out.append(f"{where}: then needs say, effects, start or nudge")
        err = check.effects(then.get("effects") or [], f"{where}.then.effects")
        if err:
            out.append(err)
        nudge = then.get("nudge")
        if nudge:
            bad = (not isinstance(nudge, dict) or nudge.get("who") not in names.persons - {known.player}
                   or not str(nudge.get("text") or "").strip())
            if bad:
                out.append(f'{where}: nudge is {{"who": <NPC>, "text": what they feel like doing}}')
    return out


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
        if giver.lower() in ("none", "nobody", "n/a", "-", "null"):
            st.giver = ""
            continue
        if giver in ("@player", "player", known.player):
            st.giver = ""
            notes.append(f"stage {st.id}: the player can't give themselves a quest; giver cleared")
            continue
        if giver and giver not in persons:
            head = re.split(r"[\s(,]", giver, maxsplit=1)[0]
            if head in persons:
                notes.append(f"stage {st.id}: giver {giver!r} -> {head}")
                st.giver = head
    notes += _fix_places(patch, known)
    notes += _fix_conditions(patch, known)
    notes += _fix_ops(patch)
    notes += _fix_start_and_listeners(patch, known)
    from content import get_content

    if get_content().conservation:
        notes += _fix_conservation(patch, known)
    return notes


def _fix_places(patch: QuestlinePatch, known: KnownWorld) -> List[str]:
    """写错的地点名：换成最像的那个；一个都不像就放到这条任务线的第一个新地点。"""
    import difflib

    notes: List[str] = []
    regions = sorted(known.regions | {r.name for r in patch.regions})
    fallback = patch.regions[0].name if patch.regions else (regions[0] if regions else "")

    def fix(name: str, where: str) -> str:
        if not name or name in regions or not fallback:
            return name
        close = difflib.get_close_matches(name, regions, n=1, cutoff=0.6)
        new = close[0] if close else fallback
        notes.append(f"{where}: {name!r} is not a region -> {new}")
        return new

    for it in patch.items:
        it.located_at = fix(it.located_at, f"item {it.name}")
    for person in patch.persons:
        person.located_at = fix(person.located_at, f"person {person.name}")
        for slot in person.routine or []:
            if isinstance(slot, dict) and slot.get("location"):
                slot["location"] = fix(str(slot["location"]), f"person {person.name} routine")
    return notes


# 模型常编的「说过话」关系：系统里说过话就是 KNOWS
TALK_RELS = {"TALKED_TO", "TALKED_WITH", "SPOKE_TO", "SPOKE_WITH", "MET", "HAS_MET", "KNOWS_PERSON", "CONVERSED_WITH"}
TIME_WORDS = ("clock", "time", "hour", "night", "day", "dawn", "dusk", "noon", "midnight", "evening", "morning")


def _fix_conditions(patch: QuestlinePatch, known: KnownWorld) -> List[str]:
    """阶段条件里的两种常见笔误：
    - 编出来的「说过话」关系 -> KNOWS；
    - 想查「几点」：阶段条件查不了时辰（世界里没有钟这个东西），去掉，留给第二步的监听器（at）。"""
    notes: List[str] = []
    names = known.regions | known.persons | known.items | known.other_names | set(patch.new_names())

    def fix(conds: List[Dict[str, Any]], where: str) -> List[Dict[str, Any]]:
        out = []
        for c in conds:
            if not isinstance(c, dict):
                out.append(c)
                continue
            if c.get("op") == "any":
                c = {**c, "of": fix(c.get("of") or [], where)}
            if c.get("op") in ("has_rel", "not_has_rel") and str(c.get("rel") or "").upper() in TALK_RELS:
                notes.append(f"{where}: relation {c['rel']} -> KNOWS (talking to someone)")
                c = {**c, "rel": "KNOWS"}
            entity = str(c.get("entity") or "")
            if entity and not entity.startswith("@") and entity not in names                     and any(w in entity.lower() for w in TIME_WORDS):
                notes.append(f"{where}: stages can't check the time of day ({entity}); dropped - "
                             "use a listener with \"at\" instead")
                continue
            out.append(c)
        return out

    for st in patch.stages:
        st.complete_when = fix(st.complete_when, f"stage {st.id}")
    for ls in patch.listeners:
        ls.when = fix(ls.when, f"listener {ls.id}")
    return notes


def _fix_ops(patch: QuestlinePatch) -> List[str]:
    """add_prop 写成了 value：当 delta 用。"""
    notes: List[str] = []
    groups = [st.reward for st in patch.stages] + [(patch.start.omen or {}).get("effects") or []] + [
        ls.then.get("effects") or [] for ls in patch.listeners]
    for ops in groups:
        for op in ops:
            if isinstance(op, dict) and op.get("op") == "add_prop" and "delta" not in op and "value" in op:
                op["delta"] = op.pop("value")
                notes.append(f"add_prop on {op.get('entity')}: used value as delta")
    return notes


def _fix_start_and_listeners(patch: QuestlinePatch, known: KnownWorld) -> List[str]:
    notes: List[str] = []
    clues = {c.name: c for c in patch.clues}
    hear = []
    for h in patch.start.hear:
        if h in clues:
            hear.append(h)
            continue
        # 写成了线索的原文（或原文的一部分）：换成那条线索的名字
        low = h.lower().strip(" .\"'")
        match = next((c.name for c in patch.clues if low and (low in c.text.lower() or c.text.lower() in low)), "")
        if match:
            hear.append(match)
            notes.append(f"start.hear {h!r} -> {match}")
        else:
            notes.append(f"start.hear {h!r} is not a clue; dropped")
    patch.start.hear = list(dict.fromkeys(hear))
    for ls in patch.listeners:
        if ls.on and ls.sequence:
            ls.on = {}
            notes.append(f"listener {ls.id}: had both on and sequence; kept sequence")
    # 异象 / 监听器要挪的东西没列在 items 里：放到第一个新地点「不起眼的地方」
    items = known.items | {i.name for i in patch.items}
    spot = patch.regions[0].name if patch.regions else ""
    groups = [(patch.start.omen or {}).get("effects") or []] + [ls.then.get("effects") or [] for ls in patch.listeners]
    for ops in groups:
        for op in ops:
            name = op.get("entity") if isinstance(op, dict) and op.get("op") == "relocate" else None
            if name and not str(name).startswith("@") and name not in items and spot and NAME_RE.match(str(name)):
                patch.items.append(ItemSpec(name=str(name), located_at=spot, item_kind="sign",
                                            description=f"Something that turns up as the story unfolds ({name})."))
                items.add(name)
                notes.append(f"item {name}: moved by an omen / listener but not listed; added at {spot}")
    return notes


def _fix_conservation(patch: QuestlinePatch, known: KnownWorld) -> List[str]:
    """守恒的两种常见笔误：
    - 奖励里 spawn_item 给玩家：改成委托人本来就拿着、交给玩家（unlink / link）；
    - 奖励给玩家钱：从委托人钱里扣同样多。
    没有委托人的阶段不动（交给校验报错）。"""
    notes: List[str] = []
    items = known.items | {i.name for i in patch.items}
    for st in patch.stages:
        giver = st.giver
        if not giver:
            continue
        out: List[Dict[str, Any]] = []
        for op in st.reward:
            if isinstance(op, dict) and op.get("op") == "spawn_item" and op.get("name") \
                    and op.get("held_by") in (None, "", "@player", known.player):
                name = str(op["name"])
                if name not in items:
                    patch.items.append(ItemSpec(name=name, held_by=giver, item_kind=str(op.get("item_kind") or ""),
                                                archetype=str(op.get("archetype") or ""),
                                                description=str(op.get("description") or "")))
                    items.add(name)
                out += [
                    {"op": "unlink", "a": giver, "a_label": "Person", "b": name, "b_label": "Item", "rel": "HAS_ITEM"},
                    {"op": "link", "a": "@player", "a_label": "Person", "b": name, "b_label": "Item", "rel": "HAS_ITEM"},
                ]
                notes.append(f"stage {st.id}: {name} is now something {giver} has and hands over (no spawn_item)")
                continue
            out.append(op)
        st.reward = out
        paid = 0.0
        for op in st.reward:
            if isinstance(op, dict) and op.get("op") == "add_prop" and op.get("prop") == "coins":
                try:
                    paid += float(op.get("delta") or 0) * float(op.get("scale", 1))
                except (TypeError, ValueError):
                    pass
        if paid > 0:
            st.reward.append({"op": "add_prop", "entity": giver, "label": "Person", "prop": "coins",
                              "delta": -int(paid) if float(paid).is_integer() else -paid})
            notes.append(f"stage {st.id}: {giver} pays the {paid:g} coins")
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


def completeness_problems(patch: QuestlinePatch, *, need_start: bool = True) -> List[str]:
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
    if need_start and patch.start.empty() and not any(ls.then.get("start") for ls in patch.listeners):
        out.append('say how the player comes across this story in "start": a clue they hear, '
                   "a thing they find (a note, a token) or a place they reach")
    return out


def conservation_problems(patch: QuestlinePatch) -> List[str]:
    """守恒：奖励和物品动作不能凭空生成东西；给钱只能从别人的钱里转。"""
    out: List[str] = []
    groups = [(f"stage {st.id} reward", st.reward) for st in patch.stages] + [
        ("start.omen", (patch.start.omen or {}).get("effects") or [])] + [
        (f"listener {ls.id}", ls.then.get("effects") or []) for ls in patch.listeners] + [
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
