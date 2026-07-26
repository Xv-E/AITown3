"""
导演事件蓝图 — 规划产物的结构化表示。

导演先产出 EventBlueprint，再由 builder 物化到 Neo4j。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _clean_name(raw: str, fallback: str = "Entity") -> str:
    """实体名：去空白，保留字母数字下划线，首字母大写风格。"""
    text = (raw or "").strip()
    if not text:
        return fallback
    # 允许中文与字母数字，空格转下划线
    cleaned = []
    for ch in text.replace(" ", "_").replace("-", "_"):
        if ch.isalnum() or ch == "_" or "\u4e00" <= ch <= "\u9fff":
            cleaned.append(ch)
    name = "".join(cleaned) or fallback
    return name[:48]


@dataclass
class LocationSpec:
    name: str
    description: str = ""
    tags: str = ""

    def normalized(self) -> "LocationSpec":
        return LocationSpec(
            name=_clean_name(self.name, "Place"),
            description=(self.description or "").strip(),
            tags=(self.tags or "").strip(),
        )


@dataclass
class AdjacencySpec:
    """地点邻接；locked=True 时需持钥匙 use_item 解锁后才可通行。"""

    a: str
    b: str
    locked: bool = False
    key: str = ""

    def normalized(self) -> "AdjacencySpec":
        a_n, b_n = _clean_name(self.a), _clean_name(self.b)
        key_n = _clean_name(self.key) if self.key else ""
        locked = bool(self.locked) and bool(key_n)
        if a_n > b_n:
            a_n, b_n = b_n, a_n
        return AdjacencySpec(a=a_n, b=b_n, locked=locked, key=key_n if locked else "")


@dataclass
class PersonSpec:
    name: str
    located_at: str
    personality: str = ""
    background: str = ""
    parent_goal: str = ""  # 兼容旧字段；不变动机请写 background
    goal: str = ""  # 当前目标（世界里给初值，决策后可改）
    values: str = ""
    mood: str = "neutral"
    hp: int = 100
    power_level: int = 10
    works_at: Optional[str] = None
    role: str = "npc"  # npc | player | antagonist
    speech_style: str = ""
    secret_policy: str = "share_gossip"
    trust_toward_player: int = 0
    accepts_from: List[str] = field(default_factory=list)  # 可被哪些工具 use_on
    # 日常日程槽位：[{start,end,location,activity,goal,social,target_entity}, ...]
    routine: List[Dict[str, Any]] = field(default_factory=list)

    def normalized(self) -> "PersonSpec":
        routine_out: List[Dict[str, Any]] = []
        for slot in self.routine or []:
            if not isinstance(slot, dict):
                continue
            loc = _clean_name(str(slot.get("location") or ""))
            if not loc:
                continue
            entry: Dict[str, Any] = {
                "start": float(slot.get("start", 0) or 0),
                "end": float(slot.get("end", 24) or 24),
                "location": loc,
                "activity": str(slot.get("activity") or "").strip(),
                "goal": str(slot.get("goal") or "Go to location").strip() or "Go to location",
                "social": bool(slot.get("social", False)),
            }
            te = str(slot.get("target_entity") or "").strip()
            if te:
                entry["target_entity"] = _clean_name(te)
            routine_out.append(entry)
        return PersonSpec(
            name=_clean_name(self.name, "Someone"),
            located_at=_clean_name(self.located_at, "TownSquare"),
            personality=(self.personality or "").strip(),
            background=(self.background or "").strip(),
            parent_goal=(self.parent_goal or "").strip(),
            goal=(self.goal or "").strip(),
            values=(self.values or "").strip(),
            mood=(self.mood or "neutral").strip(),
            hp=int(self.hp or 100),
            power_level=int(self.power_level or 10),
            works_at=_clean_name(self.works_at) if self.works_at else None,
            role=(self.role or "npc").strip().lower(),
            speech_style=(self.speech_style or "").strip(),
            secret_policy=(self.secret_policy or "share_gossip").strip(),
            trust_toward_player=int(self.trust_toward_player or 0),
            accepts_from=[_clean_name(str(x)) for x in (self.accepts_from or []) if x],
            routine=routine_out,
        )


@dataclass
class ItemSpec:
    name: str
    value: int = 10
    located_at: Optional[str] = None
    held_by: Optional[str] = None
    owned_by: Optional[str] = None
    quest_flag: str = ""
    description: str = ""
    item_kind: str = ""  # "" | door | key | ...（仅分类/UI）
    locked: bool = False
    key_item: str = ""  # 兼容旧数据；优先 accepts_from
    connects_to: str = ""  # 兼容旧数据；连通应写在 effects_on.ops
    portable: Optional[bool] = None  # None → 门默认 False，其它默认 True
    effects_on: List[dict] = field(default_factory=list)
    accepts_from: List[str] = field(default_factory=list)
    stamped: bool = False  # 文书等通用布尔状态
    fuel: Optional[int] = None  # 可选状态字段（也可只写在 script 里）
    lit: Optional[bool] = None
    respawn_at: str = ""
    # 行为脚本：lore 给 LLM；on_tick/alerts 给通用解释器
    script: Optional[Dict[str, Any]] = None

    def normalized(self) -> "ItemSpec":
        kind = (self.item_kind or "").strip().lower()
        key_n = _clean_name(self.key_item) if self.key_item else ""
        connects = _clean_name(self.connects_to) if self.connects_to else ""
        if kind == "door":
            portable = False
            locked = bool(self.locked)
        elif kind in ("campfire", "source", "fixture"):
            portable = False if self.portable is None else bool(self.portable)
            locked = False
        else:
            portable = True if self.portable is None else bool(self.portable)
            locked = bool(self.locked) if kind else False

        effects: List[dict] = []
        for eff in self.effects_on or []:
            if not isinstance(eff, dict):
                continue
            e = dict(eff)
            if e.get("target"):
                e["target"] = _clean_name(str(e["target"]))
            if "consume" in e and e["consume"] is not None:
                e["consume"] = bool(e["consume"])
            effects.append(e)

        accepts = []
        for x in self.accepts_from or []:
            s = str(x).strip()
            if not s:
                continue
            if s.lower().startswith("kind:"):
                accepts.append(s.lower())
            else:
                accepts.append(_clean_name(s))
        if kind == "door" and key_n and key_n not in accepts:
            accepts.append(key_n)

        fuel = None if self.fuel is None else int(self.fuel)
        lit = None if self.lit is None else bool(self.lit)
        script = dict(self.script) if isinstance(self.script, dict) else None

        return ItemSpec(
            name=_clean_name(self.name, "Thing"),
            value=int(self.value or 10),
            located_at=_clean_name(self.located_at) if self.located_at else None,
            held_by=_clean_name(self.held_by) if self.held_by else None,
            owned_by=_clean_name(self.owned_by) if self.owned_by else None,
            quest_flag=(self.quest_flag or "").strip(),
            description=(self.description or "").strip(),
            item_kind=kind,
            locked=locked,
            key_item=key_n if kind == "door" else "",
            connects_to=connects if kind == "door" else "",
            portable=portable,
            effects_on=effects,
            accepts_from=accepts,
            stamped=bool(self.stamped),
            fuel=fuel,
            lit=lit,
            respawn_at=_clean_name(self.respawn_at) if self.respawn_at else "",
            script=script,
        )


@dataclass
class QuestSpec:
    name: str
    description: str = ""
    stage: str = "intro"
    state: str = "active"
    involves: List[str] = field(default_factory=list)
    giver: Optional[str] = None
    objective: str = ""

    def normalized(self) -> "QuestSpec":
        return QuestSpec(
            name=_clean_name(self.name, "Quest"),
            description=(self.description or "").strip(),
            stage=(self.stage or "intro").strip(),
            state=(self.state or "active").strip(),
            involves=[_clean_name(x) for x in (self.involves or []) if x],
            giver=_clean_name(self.giver) if self.giver else None,
            objective=(self.objective or "").strip(),
        )


@dataclass
class FactSpec:
    name: str
    text: str
    secret_level: int = 0
    known_by: List[str] = field(default_factory=list)

    def normalized(self) -> "FactSpec":
        return FactSpec(
            name=_clean_name(self.name, "Fact"),
            text=(self.text or "").strip(),
            secret_level=int(self.secret_level or 0),
            known_by=[_clean_name(x) for x in (self.known_by or []) if x],
        )


@dataclass
class RumorSpec:
    name: str
    text: str
    about: Optional[str] = None
    believed_by: List[str] = field(default_factory=list)

    def normalized(self) -> "RumorSpec":
        return RumorSpec(
            name=_clean_name(self.name, "Rumor"),
            text=(self.text or "").strip(),
            about=_clean_name(self.about) if self.about else None,
            believed_by=[_clean_name(x) for x in (self.believed_by or []) if x],
        )


# (person_a, relation, person_or_item_b)  relation in KNOWS|FRIENDS_WITH|LIKES
SocialLink = Tuple[str, str, str]

# 仅匹配复合 CamelCase 实体名（StolenLedger），避免把 "The Missing Key" 当物品
_ENTITY_IDENT_RE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-zA-Z0-9]*)+\b")
_QUEST_VERB_PARTS = frozenset({
    "Find", "Recover", "Get", "Seek", "Hunt", "Return", "Steal", "Missing",
    "Quest", "The", "And", "For", "With", "From", "Into", "About", "Discover",
})


def _blob(*parts: Any) -> str:
    return " ".join(str(p or "") for p in parts)


def _entity_idents(text: str) -> List[str]:
    """从文本中抽出像实体 ID 的复合 CamelCase，不扫普通英文标题词。"""
    return _ENTITY_IDENT_RE.findall(text or "")


def _noun_parts_from_camel(name: str) -> List[str]:
    parts = re.findall(r"[A-Z][a-z]+|[A-Z]+(?![a-z])", name or "")
    return [p for p in parts if p not in _QUEST_VERB_PARTS and len(p) > 2]


def _pick_holder(persons: List[PersonSpec]) -> Optional[str]:
    for role in ("antagonist", "npc"):
        for p in persons:
            if (p.role or "").lower() == role:
                return p.name
    for p in persons:
        if (p.role or "").lower() != "player":
            return p.name
    return None


def _pick_owner(persons: List[PersonSpec], holder: Optional[str]) -> Optional[str]:
    for p in persons:
        if p.name != holder and (p.role or "").lower() not in ("player", "antagonist"):
            return p.name
    for p in persons:
        if p.name != holder:
            return p.name
    return None


def _ensure_item(
    items: List[ItemSpec],
    item_names: set,
    *,
    name: str,
    persons: List[PersonSpec],
    default_loc: str,
    quest_flag: str,
    description: str,
) -> None:
    if not name:
        return
    if name in item_names:
        if quest_flag == "main_macguffin":
            for i, it in enumerate(items):
                if it.name == name and not (it.quest_flag or "").strip():
                    items[i] = ItemSpec(
                        name=it.name,
                        value=it.value,
                        located_at=it.located_at,
                        held_by=it.held_by,
                        owned_by=it.owned_by,
                        quest_flag="main_macguffin",
                        description=it.description or description,
                        item_kind=it.item_kind,
                        locked=it.locked,
                        key_item=it.key_item,
                        connects_to=it.connects_to,
                        portable=it.portable,
                        effects_on=list(it.effects_on or []),
                        accepts_from=list(it.accepts_from or []),
                        stamped=bool(it.stamped),
                        fuel=it.fuel,
                        lit=it.lit,
                        respawn_at=getattr(it, "respawn_at", "") or "",
                        script=dict(it.script) if isinstance(getattr(it, "script", None), dict) else None,
                    )
        return
    holder = _pick_holder(persons)
    owner = _pick_owner(persons, holder)
    items.append(
        ItemSpec(
            name=name,
            value=100 if quest_flag == "main_macguffin" else 50,
            held_by=holder if quest_flag == "main_macguffin" else None,
            owned_by=owner if quest_flag == "main_macguffin" else None,
            located_at=None if (quest_flag == "main_macguffin" and holder) else default_loc,
            quest_flag=quest_flag,
            description=description,
        )
    )
    item_names.add(name)


def ensure_story_consistency(bp: "EventBlueprint") -> "EventBlueprint":
    """
    通用蓝图修补（不绑定具体剧情主题）：
    1) quest.involves 引用了不存在的实体 -> 物化为 Item
    2) 任务文案中的英文标识符若既不是人也不是地点 -> 物化为 Item
    3) 有任务却没有任何物品 -> 从任务名/目标推导一件主线道具
    """
    locs = list(bp.locations)
    persons = list(bp.persons)
    items = list(bp.items)
    quests = list(bp.quests)
    facts = list(bp.facts)
    rumors = list(bp.rumors)
    person_names = {p.name for p in persons}
    loc_names = {x.name for x in locs}
    item_names = {i.name for i in items}
    default_loc = locs[0].name if locs else "TownSquare"

    quest_names = {q.name for q in quests}

    def _is_new_object(name: str) -> bool:
        return (
            bool(name)
            and name not in person_names
            and name not in loc_names
            and name not in quest_names
        )

    # 1) dangling involves -> Item
    for q in quests:
        for inv in q.involves or []:
            if not _is_new_object(inv) or inv in item_names:
                continue
            _ensure_item(
                items,
                item_names,
                name=inv,
                persons=persons,
                default_loc=default_loc,
                quest_flag="quest_item",
                description=f"Object referenced by quest {q.name}.",
            )

    # 2) 复合 CamelCase 出现在目标/描述中且尚未是人/地点/任务名 -> Item
    #    不扫 title（易混入普通英文标题词）；任务名本身不当物品
    story_texts = [_blob(bp.premise)]
    for q in quests:
        story_texts.append(_blob(q.description, q.objective))
    macguffin_set = any((i.quest_flag or "").lower() == "main_macguffin" for i in items)
    for text in story_texts:
        for tok in _entity_idents(text):
            if not _is_new_object(tok) or tok in item_names:
                continue
            flag = "main_macguffin" if not macguffin_set else "quest_item"
            if flag == "main_macguffin":
                macguffin_set = True
            _ensure_item(
                items,
                item_names,
                name=tok,
                persons=persons,
                default_loc=default_loc,
                quest_flag=flag,
                description=f"Plot object mentioned in scenario text ({tok}).",
            )

    # 3) quests but no items -> derive macguffin from quest identifiers
    if quests and not items:
        q0 = quests[0]
        candidates: List[str] = []
        for inv in q0.involves or []:
            if _is_new_object(inv):
                candidates.append(inv)
        candidates.extend(_entity_idents(_blob(q0.objective, q0.description)))
        # 任务名里的名词片（FindMoonRelic -> MoonRelic 优先整段实体；否则 Moon/Relic）
        candidates.extend(_entity_idents(q0.name))
        nouns = _noun_parts_from_camel(q0.name)
        if len(nouns) >= 2:
            candidates.append("".join(nouns))
        candidates.extend(nouns)
        obj_name = None
        for c in candidates:
            c2 = _clean_name(c)
            if _is_new_object(c2) and c2 not in quest_names:
                obj_name = c2
                break
        if obj_name is None:
            obj_name = _clean_name("".join(nouns) or q0.name, "QuestObject")
            if obj_name in quest_names:
                obj_name = f"{obj_name}Token"
        _ensure_item(
            items,
            item_names,
            name=obj_name,
            persons=persons,
            default_loc=default_loc,
            quest_flag="main_macguffin",
            description=(q0.objective or q0.description or f"Central object for {q0.name}.").strip(),
        )
        involves = list(q0.involves or [])
        if obj_name not in involves:
            involves.insert(0, obj_name)
        quests[0] = QuestSpec(
            name=q0.name,
            description=q0.description,
            stage=q0.stage,
            state=q0.state,
            involves=involves,
            giver=q0.giver,
            objective=q0.objective or f"Resolve matter of {obj_name}.",
        )

    macguffins = [i.name for i in items if (i.quest_flag or "").lower() == "main_macguffin"]
    if quests and macguffins:
        q0 = quests[0]
        involves = list(q0.involves or [])
        changed = False
        for m in macguffins:
            if m not in involves:
                involves.insert(0, m)
                changed = True
        if changed:
            quests[0] = QuestSpec(
                name=q0.name,
                description=q0.description,
                stage=q0.stage,
                state=q0.state,
                involves=involves,
                giver=q0.giver,
                objective=q0.objective,
            )

    return EventBlueprint(
        title=bp.title,
        premise=bp.premise,
        locations=locs,
        adjacencies=list(bp.adjacencies),
        persons=persons,
        items=items,
        quests=quests,
        facts=facts,
        rumors=rumors,
        social=list(bp.social),
        source=bp.source,
    )


def _as_adjacency(edge: Any) -> Optional[AdjacencySpec]:
    if isinstance(edge, AdjacencySpec):
        return edge
    if isinstance(edge, dict):
        return AdjacencySpec(
            a=str(edge.get("a", "")),
            b=str(edge.get("b", "")),
            locked=bool(edge.get("locked", False)),
            key=str(edge.get("key", "") or ""),
        )
    if isinstance(edge, (list, tuple)) and len(edge) >= 2:
        locked = bool(edge[2]) if len(edge) >= 3 else False
        key = str(edge[3]) if len(edge) >= 4 else ""
        return AdjacencySpec(a=str(edge[0]), b=str(edge[1]), locked=locked, key=key)
    return None


@dataclass
class EventBlueprint:
    """一次事件/世界的完整规划稿。"""

    title: str
    premise: str
    locations: List[LocationSpec] = field(default_factory=list)
    adjacencies: List[AdjacencySpec] = field(default_factory=list)
    persons: List[PersonSpec] = field(default_factory=list)
    items: List[ItemSpec] = field(default_factory=list)
    quests: List[QuestSpec] = field(default_factory=list)
    facts: List[FactSpec] = field(default_factory=list)
    rumors: List[RumorSpec] = field(default_factory=list)
    social: List[SocialLink] = field(default_factory=list)
    source: str = "mock"  # mock | llm | world | api

    def normalized(self) -> "EventBlueprint":
        locs = [x.normalized() for x in self.locations]
        loc_names = {x.name for x in locs}
        persons = [x.normalized() for x in self.persons]
        # 确保 located_at / works_at 存在
        if not loc_names and persons:
            locs = [LocationSpec(name="TownSquare", description="Town center.")]
            loc_names = {"TownSquare"}
        default_loc = locs[0].name if locs else "TownSquare"
        fixed_persons: List[PersonSpec] = []
        for p in persons:
            located = p.located_at if p.located_at in loc_names else default_loc
            works = p.works_at if p.works_at in loc_names else None
            # 日程地点必须存在，否则丢掉无效槽
            routine_fixed: List[Dict[str, Any]] = []
            for slot in getattr(p, "routine", None) or []:
                if not isinstance(slot, dict):
                    continue
                if slot.get("location") not in loc_names:
                    continue
                routine_fixed.append(dict(slot))
            fixed_persons.append(
                PersonSpec(
                    name=p.name,
                    located_at=located,
                    personality=p.personality,
                    background=p.background,
                    parent_goal=p.parent_goal,
                    goal=getattr(p, "goal", "") or "",
                    values=p.values,
                    mood=p.mood,
                    hp=p.hp,
                    power_level=p.power_level,
                    works_at=works,
                    role=p.role,
                    speech_style=p.speech_style,
                    secret_policy=p.secret_policy,
                    trust_toward_player=p.trust_toward_player,
                    accepts_from=list(p.accepts_from or []),
                    routine=routine_fixed,
                )
            )
        person_names = {p.name for p in fixed_persons}
        items = [x.normalized() for x in self.items]
        fixed_items: List[ItemSpec] = []
        for it in items:
            located = it.located_at if it.located_at in loc_names else None
            held = it.held_by if it.held_by in person_names else None
            owned = it.owned_by if it.owned_by in person_names else None
            if not held and not located:
                located = default_loc
            connects = it.connects_to if it.connects_to in loc_names else ""
            # 门 / 固定物不可被持有
            if it.item_kind in ("door", "campfire", "source", "fixture"):
                held = None
                if not located:
                    located = default_loc
            respawn = it.respawn_at if getattr(it, "respawn_at", "") in loc_names else ""
            fixed_items.append(
                ItemSpec(
                    name=it.name,
                    value=it.value,
                    located_at=None if held else located,
                    held_by=held,
                    owned_by=owned,
                    quest_flag=it.quest_flag,
                    description=it.description,
                    item_kind=it.item_kind,
                    locked=it.locked,
                    key_item=it.key_item,
                    connects_to=connects,
                    portable=it.portable,
                    effects_on=list(it.effects_on or []),
                    accepts_from=list(it.accepts_from or []),
                    stamped=bool(it.stamped),
                    fuel=it.fuel,
                    lit=it.lit,
                    respawn_at=respawn,
                    script=dict(it.script) if isinstance(getattr(it, "script", None), dict) else None,
                )
            )
        item_names_set = {i.name for i in fixed_items}
        adj_map: Dict[Tuple[str, str], AdjacencySpec] = {}
        for raw in self.adjacencies:
            edge = _as_adjacency(raw)
            if edge is None:
                continue
            edge = edge.normalized()
            if edge.a not in loc_names or edge.b not in loc_names or edge.a == edge.b:
                continue
            if edge.locked and edge.key and edge.key not in item_names_set:
                # 钥匙不存在则当作普通邻接，避免卡死
                edge = AdjacencySpec(a=edge.a, b=edge.b, locked=False, key="")
            pair = (edge.a, edge.b)
            prev = adj_map.get(pair)
            if prev is None or (edge.locked and not prev.locked):
                adj_map[pair] = edge
        adjacencies = list(adj_map.values())
        # 若无任何邻接，按列表顺序串成链，保证可走通
        if locs and not adjacencies:
            names = [x.name for x in locs]
            for i in range(len(names) - 1):
                a_n, b_n = sorted((names[i], names[i + 1]))
                adjacencies.append(AdjacencySpec(a=a_n, b=b_n))

        quests = [x.normalized() for x in self.quests]
        facts = [x.normalized() for x in self.facts]
        rumors = [x.normalized() for x in self.rumors]
        social: List[SocialLink] = []
        allowed_rel = {"KNOWS", "FRIENDS_WITH", "LIKES"}
        for link in self.social:
            if len(link) != 3:
                continue
            a, rel, b = link
            a_n, b_n = _clean_name(a), _clean_name(b)
            rel_u = (rel or "KNOWS").strip().upper()
            if rel_u not in allowed_rel:
                continue
            if a_n in person_names and (b_n in person_names or b_n in item_names_set):
                social.append((a_n, rel_u, b_n))

        bp = EventBlueprint(
            title=(self.title or "Untitled Event").strip(),
            premise=(self.premise or "").strip(),
            locations=locs,
            adjacencies=adjacencies,
            persons=fixed_persons,
            items=fixed_items,
            quests=quests,
            facts=facts,
            rumors=rumors,
            social=social,
            source=self.source,
        )
        # 故事一致性：悬空 involves / 文案中的剧情物件等
        return ensure_story_consistency(bp)

    def to_dict(self) -> Dict[str, Any]:
        bp = self.normalized()
        adj_out = []
        for edge in bp.adjacencies:
            d: Dict[str, Any] = {"a": edge.a, "b": edge.b}
            if edge.locked:
                d["locked"] = True
                if edge.key:
                    d["key"] = edge.key
            adj_out.append(d)
        return {
            "title": bp.title,
            "premise": bp.premise,
            "source": bp.source,
            "locations": [asdict(x) for x in bp.locations],
            "adjacencies": adj_out,
            "persons": [asdict(x) for x in bp.persons],
            "items": [asdict(x) for x in bp.items],
            "quests": [asdict(x) for x in bp.quests],
            "facts": [asdict(x) for x in bp.facts],
            "rumors": [asdict(x) for x in bp.rumors],
            "social": [{"a": a, "rel": r, "b": b} for a, r, b in bp.social],
            "counts": {
                "locations": len(bp.locations),
                "persons": len(bp.persons),
                "items": len(bp.items),
                "quests": len(bp.quests),
                "facts": len(bp.facts),
                "rumors": len(bp.rumors),
            },
        }

    @staticmethod
    def from_dict(data: Dict[str, Any], *, source: str = "dict") -> "EventBlueprint":
        locs = [
            LocationSpec(
                name=str(x.get("name", "")),
                description=str(x.get("description", "")),
                tags=str(x.get("tags", "")),
            )
            for x in (data.get("locations") or [])
            if isinstance(x, dict)
        ]
        persons = [
            PersonSpec(
                name=str(x.get("name", "")),
                located_at=str(x.get("located_at", "") or x.get("location", "")),
                personality=str(x.get("personality", "")),
                background=str(x.get("background", "")),
                parent_goal=str(x.get("parent_goal", "")),
                goal=str(x.get("goal") or x.get("initial_goal") or ""),
                values=str(x.get("values", "")),
                mood=str(x.get("mood", "neutral")),
                hp=int(x.get("hp", 100) or 100),
                power_level=int(x.get("power_level", 10) or 10),
                works_at=x.get("works_at"),
                role=str(x.get("role", "npc")),
                speech_style=str(x.get("speech_style", "")),
                secret_policy=str(x.get("secret_policy", "share_gossip")),
                trust_toward_player=int(x.get("trust_toward_player", 0) or 0),
                accepts_from=list(x.get("accepts_from") or []),
                routine=[dict(s) for s in (x.get("routine") or []) if isinstance(s, dict)],
            )
            for x in (data.get("persons") or [])
            if isinstance(x, dict)
        ]
        items = [
            ItemSpec(
                name=str(x.get("name", "")),
                value=int(x.get("value", 10) or 10),
                located_at=x.get("located_at"),
                held_by=x.get("held_by"),
                owned_by=x.get("owned_by"),
                quest_flag=str(x.get("quest_flag", "")),
                description=str(x.get("description", "")),
                item_kind=str(x.get("item_kind", "") or ""),
                locked=bool(x.get("locked", False)),
                key_item=str(x.get("key_item", "") or ""),
                connects_to=str(x.get("connects_to", "") or ""),
                portable=x.get("portable"),
                effects_on=list(x.get("effects_on") or []),
                accepts_from=list(x.get("accepts_from") or []),
                stamped=bool(x.get("stamped", False)),
                fuel=x.get("fuel"),
                lit=x.get("lit"),
                respawn_at=str(x.get("respawn_at") or ""),
                script=dict(x["script"]) if isinstance(x.get("script"), dict) else None,
            )
            for x in (data.get("items") or [])
            if isinstance(x, dict)
        ]
        quests = [
            QuestSpec(
                name=str(x.get("name", "")),
                description=str(x.get("description", "")),
                stage=str(x.get("stage", "intro")),
                state=str(x.get("state", "active")),
                involves=list(x.get("involves") or []),
                giver=x.get("giver"),
                objective=str(x.get("objective", "")),
            )
            for x in (data.get("quests") or [])
            if isinstance(x, dict)
        ]
        facts = [
            FactSpec(
                name=str(x.get("name", "")),
                text=str(x.get("text", "")),
                secret_level=int(x.get("secret_level", 0) or 0),
                known_by=list(x.get("known_by") or []),
            )
            for x in (data.get("facts") or [])
            if isinstance(x, dict)
        ]
        rumors = [
            RumorSpec(
                name=str(x.get("name", "")),
                text=str(x.get("text", "")),
                about=x.get("about"),
                believed_by=list(x.get("believed_by") or []),
            )
            for x in (data.get("rumors") or [])
            if isinstance(x, dict)
        ]
        adjacencies: List[AdjacencySpec] = []
        for edge in data.get("adjacencies") or []:
            parsed = _as_adjacency(edge)
            if parsed is not None:
                adjacencies.append(parsed)
        social: List[SocialLink] = []
        for link in data.get("social") or []:
            if isinstance(link, dict):
                social.append(
                    (
                        str(link.get("a", "")),
                        str(link.get("rel", "KNOWS")),
                        str(link.get("b", "")),
                    )
                )
            elif isinstance(link, (list, tuple)) and len(link) >= 3:
                social.append((str(link[0]), str(link[1]), str(link[2])))

        return EventBlueprint(
            title=str(data.get("title", "Untitled Event")),
            premise=str(data.get("premise", "")),
            locations=locs,
            adjacencies=adjacencies,
            persons=persons,
            items=items,
            quests=quests,
            facts=facts,
            rumors=rumors,
            social=social,
            source=str(data.get("source") or source),
        ).normalized()
