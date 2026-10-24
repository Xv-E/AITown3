"""
势力演化 — 像群星一样自己发展：抢地盘、关系恶化或缓和、忠诚度起落、分裂出新势力、外来者进驻、衰亡。

每 FACTION_TICK_HOURS 游戏小时结算一次（maybe_tick 由世界 tick 调用，跳时间也会补算）：
  1) 地盘   有成员在场的地点控制力上升（忠诚越高越快），无人看守的慢慢流失；
            竞争 / 敌对的势力同处一地会互相削弱。每处的控制者 = 控制力最高且 ≥ 1 的势力。
  2) 张力   共享同一地盘的竞争 / 中立 / 敌对势力积累张力，满了立场就恶化一级；
            同盟 / 友好共处则张力下降，降到底立场改善一级。
  3) 忠诚   站在自家地盘上忠诚上升，自家丢地盘、身处敌对地盘、与首领不和则下降。
  4) 新势力 （有冷却）分裂：忠诚跌破底线的成员带着同样不满的人出走；
            结盟：两个对各自势力都不太忠诚、彼此投缘的人另立门户；
            外来者：一伙新人进驻无人控制的地点。
            新势力的名字、目标、来历、特性由 LLM 按剧情写（特性必须有剧情理由），没有 LLM 时按规则生成。
  5) 招募   有地盘的势力吸收站在其地盘上、对原势力不再忠诚的人。
  6) 规模   成员 / 地盘多了升格（small → medium → large）；没人也没地盘的势力解散。
每件事写进编年史（:FactionEvent），并记入相关成员的记忆，NPC 对话和决策会提到。

特性（TRAITS）既是剧情也是机制：影响扩张、交战、关系恶化速度、声望波及、入会门槛、忠诚。
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from factions import (
    STANCE_SCORE,
    _rows,
    adjust_standing,
    leader_of,
    pair_view,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 特性
# ---------------------------------------------------------------------------

TRAITS: Dict[str, Dict[str, Any]] = {
    "expansionist": {"text": "always reaching for new ground", "claim": 1.5},
    "insular": {"text": "keeps to its own and its own ground", "claim": 0.7, "join": 2.0},
    "welcoming": {"text": "opens its doors to newcomers", "join": -2.0, "pos_ripple": 1.25, "recruit": 2.0},
    "militant": {"text": "settles things by force", "contest": 1.5, "escalate": 1.25},
    "pacifist": {"text": "abhors violence", "escalate": 0.5, "attack_penalty": 1.0},
    "vengeful": {"text": "never forgets a wrong", "neg_ripple": 1.5, "escalate": 1.5},
    "mercantile": {"text": "values trade, gifts and fair dealing", "pos_ripple": 1.5, "escalate": 0.75},
    "zealous": {"text": "burns with conviction", "loyalty": 0.2, "escalate": 1.25},
    "secretive": {"text": "guards its business closely", "join": 1.0},
}


def faction_traits(world, faction: str) -> List[Dict[str, str]]:
    rows = _rows(world, "MATCH (f:Faction {name: $f}) RETURN f.traits AS t LIMIT 1", f=faction)
    raw = rows[0].get("t") if rows else None
    if isinstance(raw, str) and raw.strip().startswith("["):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    out = []
    for t in raw or []:
        if isinstance(t, str):
            t = {"id": t}
        if isinstance(t, dict) and t.get("id") in TRAITS:
            out.append({"id": t["id"], "why": str(t.get("why") or "")})
    return out


def trait_mult(world, faction: str, key: str) -> float:
    m = 1.0
    for t in faction_traits(world, faction):
        m *= float(TRAITS[t["id"]].get(key, 1.0))
    return m


def trait_sum(world, faction: str, key: str) -> float:
    return sum(float(TRAITS[t["id"]].get(key, 0.0)) for t in faction_traits(world, faction))


def traits_text(world, faction: str) -> str:
    bits = []
    for t in faction_traits(world, faction):
        why = f" ({t['why']})" if t.get("why") else ""
        bits.append(f"{t['id']}{why}")
    return "; ".join(bits)


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


TICK_HOURS = _env_float("FACTION_TICK_HOURS", 6.0)
MAX_TICKS_PER_CALL = 4
CLAIM_RATE = 0.6
DECAY = 0.3
CONTEST_RATE = 0.5
CONTROL_MIN = 1.0
STRENGTH_MAX = 10.0
TENSION_STEP = 1.0
TENSION_RELAX = 0.5
TENSION_DECAY = 0.25
ESCALATE_AT = 6.0  # 约 1.5 个游戏日的摩擦才改一级立场
SPLINTER_LOYALTY = 3.0
COALITION_LOYALTY = 5.0
RECRUIT_LOYALTY = 4.0
EMERGE_COOLDOWN = 4
MAX_ACTIVE = int(_env_float("FACTION_MAX_ACTIVE", 12))
STANCE_LADDER = ["hostile", "rival", "neutral", "friendly", "allied"]
SCALE_BY_SIZE = ((6, "large"), (3, "medium"), (0, "small"))


def _day_length() -> float:
    try:
        from routine import DAY_LENGTH_SECONDS

        return float(DAY_LENGTH_SECONDS)
    except Exception:
        return 300.0


def tick_period() -> float:
    """一次结算间隔（sim 秒）。"""
    return max(1.0, TICK_HOURS) * _day_length() / 24.0


def day_of(sim_time: float) -> int:
    return int(sim_time // _day_length()) + 1


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def active_factions(world) -> List[Dict[str, Any]]:
    rows = _rows(
        world,
        """
        MATCH (f:Faction)
        WHERE coalesce(f.status, 'active') = 'active'
        RETURN f.name AS name, coalesce(f.kind, 'other') AS kind, coalesce(f.scale, 'medium') AS scale
        ORDER BY name
        """,
    )
    return [dict(r) for r in rows if r.get("name")]


def control_rows(world) -> List[Dict[str, Any]]:
    return [dict(r) for r in _rows(
        world,
        """
        MATCH (f:Faction)-[c:CONTROLS]->(l:Location)
        WHERE coalesce(f.status, 'active') = 'active'
        RETURN f.name AS faction, l.name AS location, coalesce(c.strength, 0.0) AS strength
        """,
    )]


def territory_of(world, faction: str) -> Dict[str, float]:
    rows = _rows(
        world,
        """
        MATCH (:Faction {name: $f})-[c:CONTROLS]->(l:Location)
        RETURN l.name AS l, coalesce(c.strength, 0.0) AS s ORDER BY s DESC
        """,
        f=faction,
    )
    return {r["l"]: float(r["s"]) for r in rows if r.get("l")}


def controller_of(world, location: str) -> Optional[str]:
    rows = _rows(world, "MATCH (l:Location {name: $l}) RETURN l.controlled_by AS c LIMIT 1", l=location)
    return (rows[0].get("c") or None) if rows else None


def presence_rows(world) -> List[Dict[str, Any]]:
    """活着的直接成员在哪儿。"""
    return [dict(r) for r in _rows(
        world,
        """
        MATCH (p:Person)-[m:MEMBER_OF]->(f:Faction)
        WHERE coalesce(f.status, 'active') = 'active' AND coalesce(p.hp, 100) > 0
        OPTIONAL MATCH (p)-[:LOCATED_AT]->(l:Location)
        RETURN p.name AS person, f.name AS faction, coalesce(m.loyalty, 6.0) AS loyalty,
               coalesce(m.role, 'member') AS role, l.name AS location,
               coalesce(p.is_player, false) OR coalesce(p.role, '') = 'player' AS is_player
        """,
    )]


def relation_rows(world) -> List[Dict[str, Any]]:
    return [dict(r) for r in _rows(
        world,
        """
        MATCH (a:Faction)-[r:FACTION_REL]->(b:Faction)
        RETURN a.name AS a, b.name AS b, coalesce(r.stance, 'neutral') AS stance,
               coalesce(r.tension, 0.0) AS tension, coalesce(r.reason, '') AS reason
        """,
    )]


def _stance_map(world) -> Dict[frozenset, Dict[str, Any]]:
    out: Dict[frozenset, Dict[str, Any]] = {}
    for r in relation_rows(world):
        if r["a"] != r["b"]:
            out.setdefault(frozenset((r["a"], r["b"])), r)
    return out


def recent_events(world, faction: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
    if faction:
        rows = _rows(
            world,
            """
            MATCH (:Faction {name: $f})-[:IN_EVENT]->(e:FactionEvent)
            RETURN e.day AS day, e.kind AS kind, e.text AS text, e.t AS t
            ORDER BY e.t DESC LIMIT $n
            """,
            f=faction,
            n=limit,
        )
    else:
        rows = _rows(
            world,
            """
            MATCH (e:FactionEvent)
            RETURN e.day AS day, e.kind AS kind, e.text AS text, e.t AS t, e.factions AS factions
            ORDER BY e.t DESC LIMIT $n
            """,
            n=limit,
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def set_strength(world, faction: str, location: str, strength: float) -> None:
    if strength <= 0.05:
        _rows(world, "MATCH (:Faction {name: $f})-[c:CONTROLS]->(:Location {name: $l}) DELETE c",
              f=faction, l=location)
        return
    _rows(
        world,
        """
        MATCH (f:Faction {name: $f}), (l:Location {name: $l})
        MERGE (f)-[c:CONTROLS]->(l)
        SET c.strength = $s
        """,
        f=faction,
        l=location,
        s=round(min(STRENGTH_MAX, strength), 2),
    )


def refresh_control(world) -> Dict[str, Optional[str]]:
    """写 Location.controlled_by，返回 {地点: 控制者或 None}。"""
    best: Dict[str, Tuple[str, float]] = {}
    for r in control_rows(world):
        loc, s = r["location"], float(r["strength"])
        if s >= CONTROL_MIN and (loc not in best or s > best[loc][1]):
            best[loc] = (r["faction"], s)
    locs = [r["l"] for r in _rows(world, "MATCH (l:Location) RETURN l.name AS l") if r.get("l")]
    out: Dict[str, Optional[str]] = {}
    for loc in locs:
        ctl = best.get(loc, (None, 0.0))[0]
        out[loc] = ctl
        _rows(world, "MATCH (l:Location {name: $l}) SET l.controlled_by = $c", l=loc, c=ctl)
    return out


def set_stance(world, a: str, b: str, stance: str, reason: str, tension: float = 0.0) -> None:
    _rows(
        world,
        """
        MATCH (x:Faction {name: $a}), (y:Faction {name: $b})
        OPTIONAL MATCH (x)-[old:FACTION_REL]-(y)
        WITH x, y, collect(old) AS olds
        FOREACH (o IN olds | DELETE o)
        CREATE (x)-[:FACTION_REL {stance: $s, reason: $r, tension: $t}]->(y)
        """,
        a=a,
        b=b,
        s=stance,
        r=reason,
        t=float(tension),
    )


def set_tension(world, a: str, b: str, tension: float) -> None:
    _rows(
        world,
        "MATCH (:Faction {name: $a})-[r:FACTION_REL]-(:Faction {name: $b}) SET r.tension = $t",
        a=a,
        b=b,
        t=round(float(tension), 2),
    )


def set_loyalty(world, person: str, faction: str, loyalty: float) -> None:
    _rows(
        world,
        "MATCH (:Person {name: $p})-[m:MEMBER_OF]->(:Faction {name: $f}) SET m.loyalty = $l",
        p=person,
        f=faction,
        l=round(max(0.0, min(10.0, loyalty)), 2),
    )


def record_event(
    world, now: float, kind: str, text: str, factions: List[str], *, location: str = "", memory=None
) -> Dict[str, Any]:
    """编年史 + 相关成员的记忆。"""
    n = _meta(world).get("faction_event_n", 0) + 1
    _set_meta(world, faction_event_n=n)
    name = f"FactionEvent{n}"
    _rows(
        world,
        """
        MERGE (e:FactionEvent {name: $n})
        SET e.t = $t, e.day = $d, e.kind = $k, e.text = $text, e.factions = $fs, e.location = $loc
        WITH e
        UNWIND $fs AS fname
        MATCH (f:Faction {name: fname})
        MERGE (f)-[:IN_EVENT]->(e)
        """,
        n=name, t=float(now), d=day_of(now), k=kind, text=text, fs=list(factions), loc=location,
    )
    if memory is not None and factions:
        people = _rows(
            world,
            """
            MATCH (p:Person)-[:MEMBER_OF]->(f:Faction) WHERE f.name IN $fs
            RETURN DISTINCT p.name AS p
            """,
            fs=list(factions),
        )
        for r in people:
            try:
                memory.add(r["p"], f"[Faction news, day {day_of(now)}] {text}", type="faction_event",
                           location=location)
            except Exception:
                pass
    return {"day": day_of(now), "kind": kind, "text": text, "factions": list(factions), "location": location}


def _meta(world) -> Dict[str, Any]:
    rows = _rows(
        world,
        """
        MATCH (m:WorldMeta {name: 'WorldMeta'})
        RETURN m.faction_tick_t AS t, coalesce(m.faction_tick_n, 0) AS n,
               coalesce(m.faction_last_emerge_n, -999) AS e, coalesce(m.faction_event_n, 0) AS ev
        LIMIT 1
        """,
    )
    if not rows:
        return {}
    r = rows[0]
    return {"faction_tick_t": r.get("t"), "faction_tick_n": int(r.get("n") or 0),
            "faction_last_emerge_n": int(r.get("e") if r.get("e") is not None else -999),
            "faction_event_n": int(r.get("ev") or 0)}


def _set_meta(world, **props) -> None:
    sets = ", ".join(f"m.{k} = ${k}" for k in props)
    _rows(world, f"MERGE (m:WorldMeta {{name: 'WorldMeta'}}) SET {sets}", **props)


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------

def maybe_tick(world, sim_time: float, *, memory=None) -> Dict[str, Any]:
    """世界 tick 调用；到点就结算（跳时间时一次最多补 MAX_TICKS_PER_CALL 次）。"""
    if world is None or os.getenv("FACTION_SIM", "1").strip().lower() in ("0", "false", "no"):
        return {}
    if not active_factions(world):
        return {}
    meta = _meta(world)
    last = meta.get("faction_tick_t")
    if last is None:
        _set_meta(world, faction_tick_t=float(sim_time), faction_tick_n=0)
        return {}
    period = tick_period()
    due = int((float(sim_time) - float(last)) // period)
    if due <= 0:
        return {}
    events: List[Dict[str, Any]] = []
    n = int(meta.get("faction_tick_n") or 0)
    ran = min(due, MAX_TICKS_PER_CALL)
    for i in range(ran):
        n += 1
        now = float(last) + period * (i + 1)
        events += tick_once(world, now, n, memory=memory)
    # 补算不完的部分直接跳过，免得一次长跳卡住模拟
    _set_meta(world, faction_tick_t=float(last) + period * due, faction_tick_n=n)
    return {"ticks": ran, "events": events}


def tick_once(world, now: float, tick_no: int, *, memory=None, rng: Optional[random.Random] = None) -> List[Dict[str, Any]]:
    rng = rng or random.Random(f"{tick_no}:{int(now)}")
    events: List[Dict[str, Any]] = []
    before = {r["l"]: r.get("c") for r in _rows(world, "MATCH (l:Location) RETURN l.name AS l, l.controlled_by AS c")}
    shared, lost_by = _tick_territory(world)
    after = refresh_control(world)
    for loc, ctl in sorted(after.items()):
        old = before.get(loc)
        if ctl == old:
            continue
        if ctl and old:
            text = f"{ctl} drove {old} out of {loc}."
        elif ctl:
            text = f"{ctl} took hold of {loc}."
        else:
            text = f"{old} lost its hold on {loc}; nobody controls it now."
        events.append(record_event(world, now, "territory", text, [x for x in (ctl, old) if x],
                                   location=loc, memory=memory))
    events += _tick_tension(world, now, shared, memory=memory)
    _tick_loyalty(world, after, lost_by)
    meta = _meta(world)
    if tick_no - int(meta.get("faction_last_emerge_n", -999)) >= EMERGE_COOLDOWN \
            and len(active_factions(world)) < MAX_ACTIVE:
        ev = _try_emerge(world, now, after, rng, memory=memory)
        if ev:
            _set_meta(world, faction_last_emerge_n=tick_no)
            events += ev
    events += _tick_recruit(world, now, after, memory=memory)
    events += _tick_scale_and_dissolve(world, now, memory=memory)
    return events


def private_homes(world) -> Dict[str, str]:
    """只是住人的地方（标签只有 home 的室内地点，比如唐楼里的一户户）→ 楼外那块地。
    这些地方不算地盘：成员睡在自己家不等于势力占了它，外来势力也不会住进别人家。"""
    rows = _rows(world, "MATCH (l:Location) RETURN l.name AS l, coalesce(l.tags, '') AS t, coalesce(l.parent, '') AS p")
    return {
        r["l"]: r["p"] for r in rows
        if r.get("l") and {t.strip().lower() for t in str(r["t"]).split(",") if t.strip()} == {"home"}
    }


def _public(world, loc: str, homes: Optional[Dict[str, str]] = None) -> str:
    """在别人家里发生的事算到楼外那块地上。"""
    homes = private_homes(world) if homes is None else homes
    return homes.get(loc, loc) if loc in homes else loc


def _tick_territory(world) -> Tuple[Dict[frozenset, str], Dict[str, List[str]]]:
    """地盘结算。返回（共处一地的势力对 → 地点，{势力: 本轮丢掉的地点}）。"""
    strength: Dict[Tuple[str, str], float] = {
        (r["faction"], r["location"]): float(r["strength"]) for r in control_rows(world)
    }
    presence: Dict[Tuple[str, str], float] = {}
    homes = private_homes(world)
    for r in presence_rows(world):
        if r.get("location") and r["location"] not in homes:
            key = (r["faction"], r["location"])
            weight = 0.5 if r.get("role") == "recruit" else 1.0
            presence[key] = presence.get(key, 0.0) + weight * float(r["loyalty"]) / 10.0
    # 外来势力有一群没名字的人守着自己的地盘，直到被赶走
    for r in _rows(
        world,
        """
        MATCH (f:Faction)-[c:CONTROLS]->(l:Location)
        WHERE coalesce(f.status, 'active') = 'active' AND coalesce(f.offscreen, 0.0) > 0 AND c.strength > 0
        RETURN f.name AS f, l.name AS l, f.offscreen AS o
        """,
    ):
        key = (r["f"], r["l"])
        presence[key] = presence.get(key, 0.0) + float(r["o"])
    old_strength = dict(strength)
    for key in set(strength) | set(presence):
        f, _loc = key
        p = presence.get(key, 0.0)
        s = strength.get(key, 0.0)
        s = s + p * CLAIM_RATE * trait_mult(world, f, "claim") if p > 0 else s - DECAY
        strength[key] = s
    stances = _stance_map(world)
    by_loc: Dict[str, List[str]] = {}
    for (f, loc), s in strength.items():
        if s > 0:
            by_loc.setdefault(loc, []).append(f)
    shared: Dict[frozenset, str] = {}
    hit: Dict[Tuple[str, str], float] = {}
    for loc, fs in by_loc.items():
        for i, a in enumerate(fs):
            for b in fs[i + 1:]:
                pair = frozenset((a, b))
                shared[pair] = loc
                stance = (stances.get(pair) or {}).get("stance", "neutral")
                if stance in ("rival", "hostile"):
                    k = CONTEST_RATE * (2.0 if stance == "hostile" else 1.0)
                    hit[(a, loc)] = hit.get((a, loc), 0.0) + k * presence.get((b, loc), 0.0) * trait_mult(world, b, "contest")
                    hit[(b, loc)] = hit.get((b, loc), 0.0) + k * presence.get((a, loc), 0.0) * trait_mult(world, a, "contest")
    lost_by: Dict[str, List[str]] = {}
    for key, s in strength.items():
        s = max(0.0, min(STRENGTH_MAX, s - hit.get(key, 0.0)))
        if abs(s - old_strength.get(key, 0.0)) > 1e-6:
            set_strength(world, key[0], key[1], s)
        if old_strength.get(key, 0.0) >= CONTROL_MIN > s:
            lost_by.setdefault(key[0], []).append(key[1])
    return shared, lost_by


def _tick_tension(world, now: float, shared: Dict[frozenset, str], *, memory=None) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    stances = _stance_map(world)
    nested = _nested_pairs(world)
    pairs = (set(stances) | set(shared)) - nested
    for pair in sorted(pairs, key=lambda p: tuple(sorted(p))):
        a, b = sorted(pair)
        rel = stances.get(pair)
        stance = (rel or {}).get("stance", "neutral")
        tension = float((rel or {}).get("tension") or 0.0)
        loc = shared.get(pair)
        if loc:
            if stance in ("allied", "friendly"):
                tension -= TENSION_RELAX
            else:
                esc = (trait_mult(world, a, "escalate") + trait_mult(world, b, "escalate")) / 2.0
                tension += TENSION_STEP * esc
        else:
            tension = tension - TENSION_DECAY if tension > 0 else min(0.0, tension + TENSION_DECAY)
        if rel is None and not loc:
            continue
        idx = STANCE_LADDER.index(stance) if stance in STANCE_LADDER else 2
        if tension >= ESCALATE_AT and idx > 0:
            new = STANCE_LADDER[idx - 1]
            reason = f"clashing over {loc}" if loc else (rel or {}).get("reason", "")
            set_stance(world, a, b, new, reason)
            events.append(record_event(world, now, "stance", f"{a} and {b} went from {stance} to {new}, {reason}.",
                                       [a, b], location=loc or "", memory=memory))
        elif tension <= -ESCALATE_AT and idx < len(STANCE_LADDER) - 1:
            new = STANCE_LADDER[idx + 1]
            reason = f"standing side by side at {loc}" if loc else (rel or {}).get("reason", "")
            set_stance(world, a, b, new, reason)
            events.append(record_event(world, now, "stance", f"{a} and {b} grew closer: {stance} → {new}, {reason}.",
                                       [a, b], location=loc or "", memory=memory))
        elif rel is None:
            set_stance(world, a, b, "neutral", f"both claim {loc}", tension)
        else:
            set_tension(world, a, b, tension)
    return events


def _nested_pairs(world) -> set:
    """上下级势力（家 ⊂ 镇）共享地盘是常态，不算摩擦。"""
    rows = _rows(world, "MATCH (a:Faction)-[:PART_OF*1..3]->(b:Faction) RETURN a.name AS a, b.name AS b")
    return {frozenset((r["a"], r["b"])) for r in rows if r.get("a") and r.get("b")}


# 家与城镇的归属不靠地盘：离开它们的地方不会让人变心
GROUNDLESS_KINDS = ("town", "family")


def _tick_loyalty(world, control: Dict[str, Optional[str]], lost_by: Dict[str, List[str]]) -> None:
    stances = _stance_map(world)
    leaders: Dict[str, Optional[str]] = {}
    kinds = {f["name"]: f["kind"] for f in active_factions(world)}
    holds = {r["faction"] for r in control_rows(world) if float(r["strength"]) >= CONTROL_MIN}
    for r in presence_rows(world):
        if r.get("is_player"):
            continue
        f, person, loc = r["faction"], r["person"], r.get("location")
        delta = trait_sum(world, f, "loyalty")
        ctl = control.get(loc) if loc else None
        if ctl == f:
            delta += 0.15
        elif kinds.get(f) not in GROUNDLESS_KINDS and f in holds:
            # 远离自家地盘，心慢慢就散了
            delta -= 0.1
            if ctl and (stances.get(frozenset((f, ctl))) or {}).get("stance") == "hostile":
                delta -= 0.2
        if lost_by.get(f):
            delta -= 0.5
        if f not in leaders:
            leaders[f] = leader_of(world, f)
        lead = leaders[f]
        if lead and lead != person and pair_view(world, person, lead).score < 0:
            delta -= 0.2
        if abs(delta) > 1e-6:
            set_loyalty(world, person, f, float(r["loyalty"]) + delta)


# ---------------------------------------------------------------------------
# 新势力
# ---------------------------------------------------------------------------

def _profile(world, person: str) -> Dict[str, Any]:
    rows = _rows(
        world,
        """
        MATCH (p:Person {name: $p})
        OPTIONAL MATCH (p)-[:LOCATED_AT]->(l:Location)
        RETURN coalesce(p.personality, '') AS personality, coalesce(p.background, '') AS background,
               coalesce(p.desire, '') AS desire, coalesce(p.fear, '') AS fear, l.name AS location
        LIMIT 1
        """,
        p=person,
    )
    return dict(rows[0]) if rows else {}


def _try_emerge(world, now: float, control: Dict[str, Optional[str]], rng: random.Random, *, memory=None) -> List[Dict[str, Any]]:
    rows = [r for r in presence_rows(world) if not r.get("is_player")]
    kinds = {f["name"]: f for f in active_factions(world)}

    # 分裂：忠诚跌破底线的成员带着同样不满的人出走（城镇这种大范围势力不分裂）
    cands = sorted(
        (r for r in rows if r.get("role") != "recruit" and float(r["loyalty"]) <= SPLINTER_LOYALTY
         and kinds.get(r["faction"], {}).get("kind") != "town"),
        key=lambda r: float(r["loyalty"]),
    )
    for r in cands:
        parent = r["faction"]
        founder = r["person"]
        followers = [
            x["person"] for x in rows
            if x["faction"] == parent and x["person"] != founder and float(x["loyalty"]) <= SPLINTER_LOYALTY + 1.5
            and pair_view(world, x["person"], founder).score >= 1.0
        ]
        ctx = {"type": "splinter", "parent": parent, "parent_kind": kinds.get(parent, {}).get("kind", "other"),
               "founders": [founder] + followers[:2], "location": _public(world, r.get("location") or "")}
        return _found(world, now, ctx, rng, memory=memory)

    # 结盟：两个对各自势力都不太忠诚、彼此投缘的人
    best_loyalty: Dict[str, float] = {}
    direct: Dict[str, set] = {}
    where: Dict[str, str] = {}
    for r in rows:
        if kinds.get(r["faction"], {}).get("kind") in GROUNDLESS_KINDS:
            continue
        p = r["person"]
        best_loyalty[p] = max(best_loyalty.get(p, 0.0), float(r["loyalty"]))
        direct.setdefault(p, set()).add(r["faction"])
        where[p] = r.get("location") or ""
    people = sorted(best_loyalty)
    for i, a in enumerate(people):
        for b in people[i + 1:]:
            if where.get(a) != where.get(b) or direct[a] & direct[b]:
                continue
            if best_loyalty[a] > COALITION_LOYALTY or best_loyalty[b] > COALITION_LOYALTY:
                continue
            if pair_view(world, a, b).score < 0 or pair_view(world, b, a).score < 0:
                continue
            if not _personally_close(world, a, b):
                continue
            ctx = {"type": "coalition", "founders": [a, b], "location": _public(world, where.get(a) or ""),
                   "from": sorted(direct[a] | direct[b])}
            return _found(world, now, ctx, rng, memory=memory)

    # 外来者：一伙新人进驻无人控制的地点
    homes = private_homes(world)
    free = sorted(loc for loc, ctl in control.items() if not ctl and loc not in homes)
    chance = _env_float("FACTION_ARRIVAL_CHANCE", 0.15)
    if free and rng.random() < chance:
        ctx = {"type": "arrival", "founders": [], "location": rng.choice(free)}
        return _found(world, now, ctx, rng, memory=memory)
    return []


def _personally_close(world, a: str, b: str) -> bool:
    rows = _rows(
        world,
        """
        MATCH (x:Person {name: $a})-[r:FRIENDS_WITH|LIKES|FAMILY_OF]-(y:Person {name: $b})
        RETURN count(r) AS n
        """,
        a=a,
        b=b,
    )
    if rows and int(rows[0].get("n") or 0) > 0:
        return True
    try:
        from inner_life import load_inner_life

        return b in load_inner_life(world, a).bonds and a in load_inner_life(world, b).bonds
    except Exception:
        return False


_ARRIVAL_NAMES = ["AshenBand", "GreyRiders", "SaltTraders", "LanternOrder", "ThornKin",
                  "RiverWardens", "IronPilgrims", "DuskCompany"]
_ARRIVAL_TRAITS = ["expansionist", "militant", "mercantile", "zealous", "secretive"]


def _unique_name(world, base: str) -> str:
    base = re.sub(r"[^0-9A-Za-z]+", "", base) or "NewFaction"
    taken = {r["n"] for r in _rows(world, "MATCH (n) WHERE n.name IS NOT NULL RETURN n.name AS n") if r.get("n")}
    name, i = base, 2
    while name in taken:
        name, i = f"{base}{i}", i + 1
    return name


def fallback_story(world, ctx: Dict[str, Any], rng: random.Random) -> Dict[str, Any]:
    """无 LLM 时按规则写新势力：名字、目标、来历、特性（带剧情理由）、关系。"""
    t = ctx["type"]
    loc = ctx.get("location") or "the wilds"
    founders = ctx.get("founders") or []
    if t == "splinter":
        parent, lead = ctx["parent"], founders[0]
        kind = {"gang": "gang", "crew": "gang", "authority": "order", "family": "family",
                "guild": "crew", "town": "crew"}.get(ctx.get("parent_kind", ""), "crew")
        return {
            "name": f"Free{parent}" if not _taken(world, f"Free{parent}") else f"{lead}Loyalists",
            "kind": kind,
            "goal": f"Prove that {parent} was wrong to cast them aside",
            "description": f"{lead} walked out of {parent} at {loc}, sick of how they were treated, "
                           f"and took {', '.join(founders[1:]) or 'nobody but their pride'} along.",
            "traits": [
                {"id": "vengeful", "why": f"born from {lead}'s grudge against {parent}"},
                {"id": "insular", "why": "they trust only those who walked out together"},
            ],
            "relations": [{"with": parent, "stance": "rival", "reason": f"{lead} broke away in anger"}],
            "chronicle": f"{lead} broke away from {parent} and founded a faction of their own.",
        }
    if t == "coalition":
        a, b = founders[:2]
        return {
            "name": f"{a}{b}Pact",
            "kind": "crew",
            "goal": "Stand by each other whatever their old sides demand",
            "description": f"{a} and {b} came from different camps but found more loyalty in each other "
                           f"than in {', '.join(ctx.get('from') or []) or 'their old sides'}.",
            "traits": [
                {"id": "welcoming", "why": "founded by people who know how it feels to be outsiders"},
                {"id": "mercantile", "why": "they hold together by trading favours across old lines"},
            ],
            "relations": [{"with": f, "stance": "neutral", "reason": "a founder left their ranks"} for f in ctx.get("from") or []],
            "chronicle": f"{a} and {b} swore a pact at {loc}, beholden to neither of their old sides.",
        }
    picks = rng.sample(_ARRIVAL_TRAITS, 2)
    name = next((n for n in rng.sample(_ARRIVAL_NAMES, len(_ARRIVAL_NAMES)) if not _taken(world, n)), "Newcomers")
    return {
        "name": name,
        "kind": rng.choice(["gang", "order", "guild"]),
        "goal": f"Make {loc} their own",
        "description": f"Strangers came down the road and settled at {loc}, where nobody held sway.",
        "traits": [{"id": p, "why": f"how they meant to survive after arriving at {loc}"} for p in picks],
        "relations": [],
        "chronicle": f"Newcomers calling themselves {name} settled at {loc}.",
    }


def _taken(world, name: str) -> bool:
    return bool(_rows(world, "MATCH (n {name: $n}) RETURN n.name AS n LIMIT 1", n=name))


STORY_SYSTEM = """You write faction history for a small living RPG world.
A new faction has just emerged. Ground everything in the people, places and history given.
Its traits must come from its story: every trait needs a "why" that points at a concrete event or person.
Output ONLY valid JSON."""


def _story_prompt(world, ctx: Dict[str, Any]) -> str:
    try:
        rows = _rows(world, "MATCH (m {name: 'WorldMeta'}) WHERE m.premise IS NOT NULL RETURN m.premise AS p LIMIT 1")
        premise = (rows[0].get("p") or "") if rows else ""
    except Exception:
        premise = ""
    lines = [f"World: {premise}", f"What happened: {ctx['type']} at {ctx.get('location') or '(somewhere)'}"]
    for p in ctx.get("founders") or []:
        prof = _profile(world, p)
        lines.append(f"Founder {p}: {prof.get('personality')}; {prof.get('background')}; "
                     f"longs for {prof.get('desire') or '?'}; fears {prof.get('fear') or '?'}")
    if ctx.get("parent"):
        lines.append(f"They broke away from {ctx['parent']} (traits: {traits_text(world, ctx['parent']) or 'none'}).")
        for e in recent_events(world, ctx["parent"], 3):
            lines.append(f"  {ctx['parent']} history, day {e.get('day')}: {e.get('text')}")
    if ctx.get("from"):
        lines.append(f"The founders came from: {', '.join(ctx['from'])}.")
    loc = ctx.get("location")
    if loc:
        rows = _rows(world, "MATCH (l:Location {name: $l}) RETURN coalesce(l.description, '') AS d", l=loc)
        if rows:
            lines.append(f"Place {loc}: {rows[0].get('d')}")
    lines.append("Factions that exist: " + ", ".join(f["name"] for f in active_factions(world)))
    for e in recent_events(world, None, 5):
        lines.append(f"Recent, day {e.get('day')}: {e.get('text')}")
    lines.append("Trait ids (pick 1-3): " + ", ".join(f"{k} = {v['text']}" for k, v in TRAITS.items()))
    lines.append(
        'JSON: {"name":"CamelCase unique name","kind":"family|gang|guild|crew|order|authority|other",'
        '"goal":"<=12 words","description":"origin story, <=50 words",'
        '"traits":[{"id":"...","why":"<=20 words"}],'
        '"relations":[{"with":"existing faction","stance":"allied|friendly|neutral|rival|hostile","reason":"..."}],'
        '"chronicle":"one sentence for the history books"}'
    )
    return "\n".join(lines)


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I)
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(text[i:j + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def narrate_emergence(world, ctx: Dict[str, Any], rng: random.Random) -> Dict[str, Any]:
    """LLM 写新势力的剧情；字段逐项校验，缺的用规则补。"""
    base = fallback_story(world, ctx, rng)
    if os.getenv("FACTION_STORY_LLM", "1").strip().lower() in ("0", "false", "no"):
        return base
    try:
        from llm_router import invoke_primary, primary_available

        if not primary_available():
            return base
        raw = invoke_primary(_story_prompt(world, ctx), system=STORY_SYSTEM, max_tokens=500,
                             timeout=_env_float("FACTION_STORY_TIMEOUT", 25))
    except Exception as e:
        logger.info("faction story LLM failed: %s", e)
        return base
    data = _parse_json(raw if isinstance(raw, str) else str(raw))
    if not data:
        return base
    out = dict(base)
    if data.get("name"):
        out["name"] = str(data["name"])
    if str(data.get("kind") or "") in ("family", "gang", "guild", "crew", "order", "authority", "other"):
        out["kind"] = data["kind"]
    for k in ("goal", "description", "chronicle"):
        if str(data.get(k) or "").strip():
            out[k] = str(data[k]).strip()[:300]
    traits = [
        {"id": str(t.get("id")), "why": str(t.get("why") or "")[:160]}
        for t in data.get("traits") or [] if isinstance(t, dict) and t.get("id") in TRAITS
    ][:3]
    if traits:
        out["traits"] = traits
    existing = {f["name"] for f in active_factions(world)}
    rels = [
        {"with": r["with"], "stance": r["stance"], "reason": str(r.get("reason") or "")[:160]}
        for r in data.get("relations") or []
        if isinstance(r, dict) and r.get("with") in existing and r.get("stance") in STANCE_SCORE
    ]
    if rels or not base["relations"]:
        out["relations"] = rels or base["relations"]
    return out


def _found(world, now: float, ctx: Dict[str, Any], rng: random.Random, *, memory=None) -> List[Dict[str, Any]]:
    story = narrate_emergence(world, ctx, rng)
    name = _unique_name(world, story["name"])
    from factions import DEFAULT_JOIN

    policy, threshold = DEFAULT_JOIN.get(story["kind"], DEFAULT_JOIN["other"])
    _rows(
        world,
        """
        MERGE (f:Faction {name: $n})
        SET f.kind = $kind, f.scale = 'small', f.goal = $goal, f.description = $desc, f.origin = $origin,
            f.traits = $traits, f.status = 'active', f.founded_at = $t, f.founded_day = $d,
            f.join_policy = $policy, f.join_standing = $thr, f.offscreen = $off
        """,
        n=name, kind=story["kind"], goal=story["goal"], desc=story["description"], origin=ctx["type"],
        traits=json.dumps(story["traits"], ensure_ascii=False), t=float(now), d=day_of(now),
        policy=policy, thr=float(threshold), off=0.6 if ctx["type"] == "arrival" else 0.0,
    )
    founders = ctx.get("founders") or []
    for i, p in enumerate(founders):
        _rows(
            world,
            """
            MATCH (p:Person {name: $p}), (f:Faction {name: $f})
            MERGE (p)-[m:MEMBER_OF]->(f)
            SET m.role = $role, m.loyalty = $l
            """,
            p=p, f=name, role="founder" if i == 0 else "member", l=8.0 if i == 0 else 7.0,
        )
        if ctx["type"] == "splinter":
            _rows(world, "MATCH (:Person {name: $p})-[m:MEMBER_OF]->(:Faction {name: $f}) DELETE m",
                  p=p, f=ctx["parent"])
            adjust_standing(world, p, ctx["parent"], -3.0)
    for r in story["relations"]:
        set_stance(world, name, r["with"], r["stance"], r.get("reason") or "")
    loc = ctx.get("location")
    if loc:
        set_strength(world, name, loc, 3.0 if ctx["type"] == "arrival" else 2.0)
    refresh_control(world)
    ev = record_event(world, now, ctx["type"], story["chronicle"], [name] + ([ctx["parent"]] if ctx.get("parent") else []),
                      location=loc or "", memory=memory)
    if memory is not None:
        for p in founders:
            try:
                memory.add(p, f"You founded {name}: {story['description']}", type="faction", location=loc or "")
            except Exception:
                pass
    ev["new_faction"] = name
    return [ev]


# ---------------------------------------------------------------------------
# 招募 / 规模 / 解散
# ---------------------------------------------------------------------------

def _tick_recruit(world, now: float, control: Dict[str, Optional[str]], *, memory=None) -> List[Dict[str, Any]]:
    """控制某地的势力吸收站在那里、对原势力不再忠诚的人（每轮最多一人）。"""
    rows = presence_rows(world)
    loyal: Dict[str, float] = {}
    mine: Dict[str, set] = {}
    for r in rows:
        loyal[r["person"]] = max(loyal.get(r["person"], 0.0), float(r["loyalty"]))
        mine.setdefault(r["person"], set()).add(r["faction"])
    people = _rows(
        world,
        """
        MATCH (p:Person)-[:LOCATED_AT]->(l:Location)
        WHERE coalesce(p.hp, 100) > 0 AND NOT (coalesce(p.is_player, false) OR coalesce(p.role, '') = 'player')
        RETURN p.name AS p, l.name AS l ORDER BY p
        """,
    )
    stances = _stance_map(world)
    for r in people:
        ctl = control.get(r["l"])
        if not ctl or ctl in mine.get(r["p"], set()):
            continue
        limit = RECRUIT_LOYALTY + trait_sum(world, ctl, "recruit")
        if loyal.get(r["p"], 0.0) > limit:
            continue
        if any((stances.get(frozenset((ctl, f))) or {}).get("stance") == "hostile" for f in mine.get(r["p"], set())):
            continue
        _rows(
            world,
            """
            MATCH (p:Person {name: $p}), (f:Faction {name: $f})
            MERGE (p)-[m:MEMBER_OF]->(f) SET m.role = 'member', m.loyalty = 6.0
            """,
            p=r["p"],
            f=ctl,
        )
        return [record_event(world, now, "recruit", f"{r['p']} threw in their lot with {ctl} at {r['l']}.",
                             [ctl], location=r["l"], memory=memory)]
    return []


def _tick_scale_and_dissolve(world, now: float, *, memory=None) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for f in active_factions(world):
        if f["kind"] == "town":
            continue
        rows = _rows(
            world,
            """
            MATCH (fa:Faction {name: $f})
            OPTIONAL MATCH (p:Person)-[:MEMBER_OF]->(fa) WHERE coalesce(p.hp, 100) > 0
            WITH fa, count(DISTINCT p) AS members
            OPTIONAL MATCH (fa)-[c:CONTROLS]->(:Location) WHERE c.strength >= $min
            RETURN members, count(c) AS held
            """,
            f=f["name"],
            min=CONTROL_MIN,
        )
        members = int(rows[0].get("members") or 0) if rows else 0
        held = int(rows[0].get("held") or 0) if rows else 0
        if members == 0 and held == 0:
            _rows(world, "MATCH (f:Faction {name: $f}) SET f.status = 'dissolved', f.dissolved_at = $t",
                  f=f["name"], t=float(now))
            events.append(record_event(world, now, "dissolved",
                                       f"{f['name']} faded away: no members left, no ground held.", [f["name"]],
                                       memory=memory))
            continue
        size = members + held
        scale = next(s for n, s in SCALE_BY_SIZE if size >= n)
        order = ["small", "medium", "large"]
        if order.index(scale) > order.index(f["scale"] if f["scale"] in order else "small"):
            _rows(world, "MATCH (fa:Faction {name: $f}) SET fa.scale = $s", f=f["name"], s=scale)
            events.append(record_event(world, now, "rise", f"{f['name']} has grown into a {scale} power.",
                                       [f["name"]], memory=memory))
    return events


def chronicle(world, limit: int = 12) -> List[Dict[str, Any]]:
    """UI：最近的势力大事。"""
    return recent_events(world, None, limit)


def force_tick(world, *, ticks: int = 1, sim_time: Optional[float] = None, memory=None) -> List[Dict[str, Any]]:
    """立刻结算若干轮（调试 / 让历史往前走），不等游戏时钟。"""
    meta = _meta(world)
    n = int(meta.get("faction_tick_n") or 0)
    base = float(sim_time if sim_time is not None else (meta.get("faction_tick_t") or 0.0))
    events: List[Dict[str, Any]] = []
    for i in range(max(1, min(20, int(ticks)))):
        n += 1
        events += tick_once(world, base + tick_period() * (i + 1), n, memory=memory)
    _set_meta(world, faction_tick_t=base + tick_period() * max(1, min(20, int(ticks))), faction_tick_n=n)
    return events
