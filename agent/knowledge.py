"""
人物知识：知道哪些地点、以为哪些物品在哪。

图里两种关系（只是信念，可能过时）：
  (:Person)-[:KNOWS_PLACE]->(:Location)
  (:Person)-[:SAW {loc, holder, t, source}]->(:Item)   最后一次看到 / 听说它在 loc 的地上或 holder 手里

规划时用信念代替真相（filter_view / filter_snapshot）：
  - 只用知道的地点；物品只用有信念的，位置取信念里的位置；
  - 自己背包里的东西总是知道的。
信念过时（东西被拿走了）要到现场才会发现：observe 会删掉不再成立的信念，执行失败后重新规划。

planning.json 的 "knowledge": "belief"（默认）| "omniscient"；Person.knows_all = true 的人全知。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

Belief = Tuple[Optional[str], Optional[str]]  # (地点, 持有人)，二者只有一个非空


@dataclass
class Knowledge:
    places: Set[str] = field(default_factory=set)
    items: Dict[str, Belief] = field(default_factory=dict)
    kinds: Dict[str, str] = field(default_factory=dict)
    times: Dict[str, float] = field(default_factory=dict)
    omniscient: bool = False


def knowledge_mode() -> str:
    try:
        from content import get_content

        return str(get_content().planning.get("knowledge") or "belief")
    except Exception:
        return "belief"


# ---------- 纯逻辑 ----------

def diff_sighting(
    loc: str,
    seen: Dict[str, Optional[str]],
    believed: Dict[str, Belief],
    present: Set[str],
) -> Tuple[Dict[str, Optional[str]], List[str]]:
    """seen: 这里看到的物品 -> 持有人（None 为地上）；believed: 相关的旧信念。

    返回 (需要写入 / 更新的, 不再成立的)。只有亲眼能验证的旧信念才会被否定：
    地上的东西必须在这里；别人手里的，那人必须在场。
    """
    new = {i: h for i, h in seen.items() if believed.get(i) != (None if h else loc, h)}
    gone = [
        i for i, (l, h) in believed.items()
        if i not in seen and ((h is None and l == loc) or (h is not None and h in present))
    ]
    return new, sorted(gone)


def filter_view(view: Any, me: str, k: Knowledge) -> Any:
    """item_chain.WorldView → 只含 me 知道的地点和物品，物品位置取信念。"""
    if k.omniscient:
        return view
    places = set(k.places)
    here = view.person_loc.get(me)
    if here:
        places |= {here, *view.adjacency.get(here, [])}
    items = {}
    for name, info in view.items.items():
        if info.holder == me:
            items[name] = info
        elif name in k.items:
            loc, holder = k.items[name]
            items[name] = replace(info, location=None if holder else loc, holder=holder)
    return replace(
        view,
        adjacency={a: [b for b in nbrs if b in places] for a, nbrs in view.adjacency.items() if a in places},
        items=items,
    )


def filter_snapshot(snap: Any, me: str, k: Knowledge) -> Any:
    """planner.compiler.Snapshot → 同上。"""
    if k.omniscient:
        return snap
    places = set(k.places)
    here = snap.location.get(me)
    if here:
        places.add(here)
        places |= {b for a, b in snap.edges.get("ADJACENT_TO", ()) if a == here}
    out = type(snap)()
    for name, (label, props) in snap.objects.items():
        if label == "Location" and name in places:
            out.objects[name] = (label, props)
        elif label == "Person" and (name == me or snap.location.get(name) in places):
            out.objects[name] = (label, props)
            if name in snap.location:
                out.location[name] = snap.location[name]
    for name, (label, props) in snap.objects.items():
        if label != "Item":
            continue
        if snap.holder.get(name) == me:
            out.objects[name] = (label, props)
            out.holder[name] = me
        elif name in k.items:
            loc, holder = k.items[name]
            if holder and holder in out.objects:
                out.holder[name] = holder
            elif not holder and loc in places:
                out.location[name] = loc
            else:
                continue
            out.objects[name] = (label, props)
    for rel, pairs in snap.edges.items():
        kept = {(a, b) for a, b in pairs if a in out.objects and b in out.objects}
        if kept:
            out.edges[rel] = kept
    return out


def _mentions(text: str, word: str, plural: bool = False) -> bool:
    if not word:
        return False
    pat = re.escape(word.lower()) + (r"(?:e?s)?" if plural else "")
    return re.search(rf"(?<![a-z0-9_]){pat}(?![a-z0-9_])", text) is not None


def mentioned_beliefs(text: str, k: Knowledge) -> Dict[str, Belief]:
    """话里同时提到了物品（名字或类别）和它在哪（地点或持有人）的信念。"""
    low = (text or "").lower()
    out: Dict[str, Belief] = {}
    for item, (loc, holder) in k.items.items():
        named = _mentions(low, item) or _mentions(low, k.kinds.get(item, ""), plural=True)
        where = _mentions(low, loc or "") or _mentions(low, holder or "")
        if named and where:
            out[item] = (loc, holder)
    return out


def describe_sighting(loc: str, new: Dict[str, Optional[str]], gone: Iterable[str], kinds: Dict[str, str]) -> str:
    def label(i: str) -> str:
        return f"{i} ({kinds[i]})" if kinds.get(i) else i

    parts = []
    ground = [label(i) for i, h in new.items() if not h]
    if ground:
        parts.append(f"Saw at {loc}: {', '.join(ground)}.")
    for i, h in new.items():
        if h:
            parts.append(f"Saw {h} holding {label(i)} at {loc}.")
    gone = list(gone)
    if gone:
        parts.append(f"No longer at {loc}: {', '.join(label(i) for i in gone)}.")
    return " ".join(parts)


# ---------- 图读写 ----------

def current_location(world: Any, me: str) -> Optional[str]:
    rows = world.run_query(
        "MATCH (:Person {name: $me})-[:LOCATED_AT]->(l:Location) RETURN l.name AS l", me=me
    ) or []
    return rows[0]["l"] if rows else None


def load_knowledge(world: Any, me: str, *, _seed_missing: bool = True) -> Knowledge:
    """没有任何地点知识的人（旧存档）先按开局规则补种。"""
    if knowledge_mode() == "omniscient":
        return Knowledge(omniscient=True)
    rows = world.run_query(
        """
        MATCH (p:Person {name: $me})
        OPTIONAL MATCH (p)-[:KNOWS_PLACE]->(l:Location)
        WITH p, collect(l.name) AS places
        OPTIONAL MATCH (p)-[s:SAW]->(i:Item)
        RETURN coalesce(p.knows_all, false) AS all, places,
               collect({item: i.name, loc: s.loc, holder: s.holder, t: s.t, kind: i.item_kind}) AS seen
        """,
        me=me,
    ) or []
    if not rows:
        return Knowledge(omniscient=True)
    row = rows[0]
    if row.get("all"):
        return Knowledge(omniscient=True)
    k = Knowledge(places=set(row.get("places") or []))
    for s in row.get("seen") or []:
        if not s.get("item"):
            continue
        k.items[s["item"]] = (s.get("loc") or None, s.get("holder") or None)
        k.kinds[s["item"]] = str(s.get("kind") or "").lower()
        k.times[s["item"]] = float(s.get("t") or 0.0)
    if not k.places and _seed_missing:
        seed(world, me)
        return load_knowledge(world, me, _seed_missing=False)
    return k


def know_places(world: Any, me: str, places: Iterable[str], *, with_neighbors: bool = False) -> None:
    names = {p for p in places if p}
    if not names:
        return
    if with_neighbors:
        rows = world.run_query(
            """
            UNWIND $names AS n
            MATCH (:Location {name: n})-[:ADJACENT_TO|LOCKED_ADJACENT_TO]-(m:Location)
            RETURN DISTINCT m.name AS name
            """,
            names=sorted(names),
        )
        names |= {r["name"] for r in rows if r.get("name")}
    # 只写新认识的地点：每拍都会调用，无变化时不写库（写会让 tick 读缓存失效）
    known = {
        r["name"]
        for r in world.run_query(
            "MATCH (:Person {name: $me})-[:KNOWS_PLACE]->(l:Location) RETURN l.name AS name",
            me=me,
        )
    }
    missing = sorted(names - known)
    if not missing:
        return
    world.run_query(
        """
        MATCH (p:Person {name: $me})
        UNWIND $names AS n
        MATCH (l:Location {name: n})
        MERGE (p)-[:KNOWS_PLACE]->(l)
        """,
        me=me, names=missing,
    )


def learn(world: Any, me: str, beliefs: Dict[str, Belief], *, t: float = 0.0,
          source: str = "seen", times: Optional[Dict[str, float]] = None) -> None:
    if not beliefs:
        return
    rows = [
        {"item": i, "loc": loc, "holder": holder, "t": float((times or {}).get(i, t))}
        for i, (loc, holder) in beliefs.items()
    ]
    world.run_query(
        """
        MATCH (p:Person {name: $me})
        UNWIND $rows AS r
        MATCH (i:Item {name: r.item})
        MERGE (p)-[s:SAW]->(i)
        SET s.loc = coalesce(r.loc, ''), s.holder = coalesce(r.holder, ''), s.t = r.t, s.source = $source
        """,
        me=me, rows=rows, source=source,
    )
    know_places(world, me, {loc for loc, _ in beliefs.values() if loc})


def forget(world: Any, me: str, items: Iterable[str]) -> None:
    names = list(items)
    if names:
        world.run_query(
            "MATCH (:Person {name: $me})-[s:SAW]->(i:Item) WHERE i.name IN $names DELETE s",
            me=me, names=names,
        )


def observe(world: Any, me: str, loc: Optional[str], *, t: float = 0.0, places: bool = False
            ) -> Tuple[Dict[str, Optional[str]], List[str], Dict[str, str]]:
    """看一眼当前地点：更新信念。返回 (新看到的, 不再成立的（不含自己拿走的）, 类别)。"""
    if not loc:
        return {}, [], {}
    if places:
        know_places(world, me, [loc], with_neighbors=True)
    rows = world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(:Location {name: $loc})
        RETURN i.name AS item, null AS holder, i.item_kind AS kind
        UNION ALL
        MATCH (p:Person)-[:LOCATED_AT]->(:Location {name: $loc})
        OPTIONAL MATCH (p)-[:HAS_ITEM]->(i:Item)
        RETURN i.name AS item, p.name AS holder, i.item_kind AS kind
        """,
        loc=loc,
    ) or []
    seen: Dict[str, Optional[str]] = {}
    present: Set[str] = set()
    kinds: Dict[str, str] = {}
    mine: Set[str] = set()
    for r in rows:
        if r.get("holder"):
            present.add(r["holder"])
        if r.get("holder") == me and r.get("item"):
            mine.add(r["item"])
        if not r.get("item") or r.get("holder") == me:
            continue
        seen[r["item"]] = r.get("holder")
        kinds[r["item"]] = str(r.get("kind") or "").lower()
    believed: Dict[str, Belief] = {}
    for r in world.run_query(
        """
        MATCH (:Person {name: $me})-[s:SAW]->(i:Item)
        WHERE s.loc = $loc OR s.holder IN $present OR i.name IN $seen
        RETURN i.name AS item, s.loc AS loc, s.holder AS holder, i.item_kind AS kind
        """,
        me=me, loc=loc, present=sorted(present), seen=sorted(seen),
    ) or []:
        believed[r["item"]] = (r.get("loc") or None, r.get("holder") or None)
        kinds.setdefault(r["item"], str(r.get("kind") or "").lower())
    new, gone = diff_sighting(loc, seen, believed, present)
    learn(world, me, {i: (None if h else loc, h) for i, h in new.items()}, t=t)
    forget(world, me, gone)
    return new, [i for i in gone if i not in mine], kinds


def seed(world: Any, me: str, spec: Optional[Dict[str, Any]] = None) -> None:
    """开局知识：起始地点及相邻地点里的东西；spec 可额外指定 places / items，或 all。"""
    spec = spec or {}
    world.run_query(
        "MATCH (p:Person {name: $me}) SET p.knows_all = $all", me=me, all=bool(spec.get("all"))
    )
    if spec.get("all"):
        return
    rows = world.run_query(
        """
        MATCH (p:Person {name: $me})-[:LOCATED_AT]->(l:Location)
        OPTIONAL MATCH (l)-[:ADJACENT_TO]-(n:Location)
        RETURN l.name AS home, collect(DISTINCT n.name) AS near
        """,
        me=me,
    ) or []
    looked: List[str] = []
    if rows and rows[0].get("home"):
        looked = [rows[0]["home"], *(rows[0].get("near") or [])]
    looked += [str(p) for p in spec.get("places") or []]
    for loc in dict.fromkeys(looked):
        observe(world, me, loc, places=True)
    names = [str(i) for i in spec.get("items") or []]
    if names:
        beliefs: Dict[str, Belief] = {}
        for r in world.run_query(
            """
            MATCH (i:Item) WHERE i.name IN $names
            OPTIONAL MATCH (i)-[:LOCATED_AT]->(l:Location)
            OPTIONAL MATCH (h:Person)-[:HAS_ITEM]->(i)
            RETURN i.name AS item, l.name AS loc, h.name AS holder
            """,
            names=names,
        ) or []:
            if r.get("holder") != me and (r.get("loc") or r.get("holder")):
                beliefs[r["item"]] = (None if r.get("holder") else r.get("loc"), r.get("holder"))
        learn(world, me, beliefs, source="told")


def share_from_dialogue(world: Any, lines: List[Dict[str, str]], a: str, b: str) -> Dict[str, List[str]]:
    """对话里说出口的「X 在 Y」传给听者。返回 听者 -> 学到的描述。"""
    if knowledge_mode() == "omniscient":
        return {}
    gained: Dict[str, List[str]] = {}
    cache: Dict[str, Knowledge] = {}

    def k_of(name: str) -> Knowledge:
        if name not in cache:
            cache[name] = load_knowledge(world, name)
        return cache[name]

    for line in lines:
        speaker, text = line.get("speaker", ""), line.get("text", "")
        listener = b if speaker == a else a
        if not speaker or not text or listener == speaker:
            continue
        said = mentioned_beliefs(text, k_of(speaker))
        mine = k_of(listener)
        if mine.omniscient:
            continue
        fresh = {
            i: bel for i, bel in said.items()
            if mine.items.get(i) != bel and mine.times.get(i, -1.0) <= k_of(speaker).times.get(i, 0.0)
        }
        places = [p for p in k_of(speaker).places if p not in mine.places and _mentions(text.lower(), p)]
        if fresh:
            learn(world, listener, fresh, source=f"told:{speaker}", times=k_of(speaker).times)
        if places:
            know_places(world, listener, places)
        if fresh or places:
            cache.pop(listener, None)
            notes = [f"{i} is {'with ' + h if h else 'at ' + str(l)}" for i, (l, h) in fresh.items()]
            notes += [f"{p} exists" for p in places]
            gained.setdefault(listener, []).extend(notes)
    return gained
