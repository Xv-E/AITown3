"""
街头经济 — 钱只在街上流转，从不凭空消失或出现。

钱：kind=cash 的物品（一张一元钞票）。每笔交易都是钞票换手：
  买东西   顾客把钱付给摊档（streets 包的 cash.effects_on）→ 钱进摊主口袋，顾客拿到食物
  进货     摊主到码头批发处买 supplies → 钱进批发商口袋
  保护费   帮派控制的地盘上，每家生意每天欠下 protection_fee（记在 (:Faction)-[:EXTORTS {dues}]->(:Item)）；
           成员 collect_dues 时从摊主口袋收走，老大抽一半
  工钱     有 wage 的势力，首领每天给每个成员发工钱（帮派养小弟、码头老板付搬运工）
  房租     rent_book：租客每天交房租给业主
于是：顾客 → 摊主 → 批发商 / 帮派 → 小弟 / 业主 → 又去摊档吃饭喝酒 → 摊主……

与势力联动：
  - 交保护费的地点，收钱的帮派控制力上升；别的帮派来"过界收数"会抢走对头的钱、引发张力；
  - 有差人（authority 势力的成员）在场时收不了，还会被记一笔；
  - 拖欠满 OVERDUE_DAYS 天，帮派会砸场：摊档存货清零、摊主受伤，和摊主所在的行会结怨。
重要的事写进编年史，相关的人都会记得。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from factions import _rows, adjust_standing, leader_of, memberships

logger = logging.getLogger(__name__)

OVERDUE_DAYS = 3.0
BOSS_CUT = 0.5
TURF_PER_PAYMENT = 0.5
TURF_PER_NEW_RACKET = 1.0
MUSCLE_IN_STANDING = -3.0
MUSCLE_IN_TENSION = 2.0
POLICE_STANDING = -1.0
SMASH_HP = 10


# ---------------------------------------------------------------------------
# 钱
# ---------------------------------------------------------------------------

def cash_of(world, person: str) -> int:
    rows = _rows(
        world,
        """
        MATCH (:Person {name: $p})-[:HAS_ITEM]->(c:Item)
        WHERE c.item_kind = 'cash'
        RETURN count(c) AS n
        """,
        p=person,
    )
    return int(rows[0]["n"]) if rows else 0


def transfer_cash(world, frm: str, to: str, amount: int) -> int:
    """把 frm 口袋里至多 amount 张钞票交到 to 手里，返回实际张数。"""
    amount = int(amount)
    if amount <= 0 or not frm or not to or frm == to:
        return 0
    rows = _rows(
        world,
        """
        MATCH (a:Person {name: $f})-[h:HAS_ITEM]->(c:Item)
        WHERE c.item_kind = 'cash'
        WITH h, c LIMIT $n
        MATCH (b:Person {name: $t})
        DELETE h
        MERGE (b)-[:HAS_ITEM]->(c)
        RETURN count(c) AS n
        """,
        f=frm,
        t=to,
        n=amount,
    )
    return int(rows[0]["n"]) if rows else 0


# ---------------------------------------------------------------------------
# 生意与保护费
# ---------------------------------------------------------------------------

def businesses(world, location: Optional[str] = None) -> List[Dict[str, Any]]:
    """要交保护费的生意（有 owner 和 protection_fee 的固定物）。"""
    rows = _rows(
        world,
        """
        MATCH (i:Item)-[:LOCATED_AT]->(l:Location)
        WHERE coalesce(i.protection_fee, 0) > 0 AND coalesce(i.owner, '') <> ''
          AND ($loc IS NULL OR l.name = $loc)
        OPTIONAL MATCH (f:Faction)-[x:EXTORTS]->(i)
        RETURN i.name AS name, i.owner AS owner, l.name AS location,
               toFloat(i.protection_fee) AS fee, coalesce(i.stock, 0) AS stock,
               coalesce(i.takings, 0) AS takings, l.controlled_by AS controller,
               collect({faction: f.name, dues: coalesce(x.dues, 0.0), paid: coalesce(x.paid, 0)}) AS rackets
        ORDER BY name
        """,
        loc=location,
    )
    out = []
    for r in rows:
        r = dict(r)
        r["rackets"] = [x for x in r.get("rackets") or [] if x.get("faction")]
        out.append(r)
    return out


def _faction_kind(world, faction: Optional[str]) -> str:
    if not faction:
        return ""
    rows = _rows(world, "MATCH (f:Faction {name: $f}) RETURN coalesce(f.kind, 'other') AS k", f=faction)
    return rows[0]["k"] if rows else ""


def gangs_of(world, person: str) -> List[str]:
    return [m.faction for m in memberships(world, person) if m.direct and m.kind == "gang"]


def _set_dues(world, faction: str, item: str, *, dues: float, paid_delta: int = 0, fee: float = 0.0) -> None:
    _rows(
        world,
        """
        MATCH (f:Faction {name: $f}), (i:Item {name: $i})
        MERGE (f)-[x:EXTORTS]->(i)
        SET x.dues = $d, x.paid = coalesce(x.paid, 0) + $p, x.fee = $fee
        """,
        f=faction,
        i=item,
        d=round(max(0.0, dues), 2),
        p=int(paid_delta),
        fee=float(fee),
    )


def accrue_dues(world, hours: float) -> None:
    """帮派控制的地盘上，每家生意按天欠下保护费；不再控制的帮派失去收数的权利。"""
    for b in businesses(world):
        ctl = b.get("controller")
        gang_holds = bool(ctl) and _faction_kind(world, ctl) == "gang"
        for r in b["rackets"]:
            if r["faction"] != ctl or not gang_holds:
                _rows(world, "MATCH (:Faction {name: $f})-[x:EXTORTS]->(:Item {name: $i}) DELETE x",
                      f=r["faction"], i=b["name"])
        if gang_holds:
            cur = next((r["dues"] for r in b["rackets"] if r["faction"] == ctl), 0.0)
            _set_dues(world, ctl, b["name"], dues=float(cur) + float(b["fee"]) * hours / 24.0, fee=float(b["fee"]))


def _police_here(world, location: str, exclude: str) -> List[Dict[str, str]]:
    return [dict(r) for r in _rows(
        world,
        """
        MATCH (p:Person)-[:MEMBER_OF]->(f:Faction {kind: 'authority'}), (p)-[:LOCATED_AT]->(:Location {name: $l})
        WHERE p.name <> $me AND coalesce(p.hp, 100) > 0 AND coalesce(f.status, 'active') = 'active'
        RETURN p.name AS person, f.name AS faction
        """,
        l=location,
        me=exclude,
    )]


def _add_turf(world, faction: str, location: str, amount: float) -> None:
    try:
        from faction_sim import set_strength, territory_of

        set_strength(world, faction, location, territory_of(world, faction).get(location, 0.0) + amount)
    except Exception as e:
        logger.debug("add turf failed: %s", e)


def _add_tension(world, a: str, b: str, amount: float) -> None:
    rows = _rows(
        world,
        """
        MATCH (:Faction {name: $a})-[r:FACTION_REL]-(:Faction {name: $b})
        SET r.tension = coalesce(r.tension, 0.0) + $t
        RETURN count(r) AS n
        """,
        a=a,
        b=b,
        t=float(amount),
    )
    if not rows or not int(rows[0].get("n") or 0):
        try:
            from faction_sim import set_stance

            set_stance(world, a, b, "neutral", "money on the same streets", amount)
        except Exception:
            pass


def _remember(memory, who: str, text: str, location: str = "") -> None:
    if memory is None or not who:
        return
    try:
        memory.add(who, text, type="economy", location=location)
    except Exception:
        pass


def _location_of(world, person: str) -> Optional[str]:
    rows = _rows(world, "MATCH (:Person {name: $p})-[:LOCATED_AT]->(l:Location) RETURN l.name AS l LIMIT 1", p=person)
    return rows[0]["l"] if rows else None


def _chronicle(world, text: str, factions: List[str], location: str, memory=None) -> Optional[Dict[str, Any]]:
    try:
        from faction_sim import record_event

        rows = _rows(world, "MATCH (m:WorldMeta {name: 'WorldMeta'}) RETURN coalesce(m.sim_time, 0.0) AS t LIMIT 1")
        now = float(rows[0]["t"]) if rows else 0.0
        return record_event(world, now, "economy", text, factions, location=location, memory=memory)
    except Exception:
        return None


def collect(world, collector: str, location: Optional[str] = None, *, memory=None) -> Dict[str, Any]:
    """collector 向此处每家生意收保护费。返回 {collected, notes}。"""
    loc = location or _location_of(world, collector)
    notes: List[str] = []
    gangs = gangs_of(world, collector)
    if not loc:
        return {"collected": 0, "notes": ["You are nowhere"]}
    if not gangs:
        return {"collected": 0, "notes": [f"{collector} has no gang to collect for"]}
    gang = gangs[0]
    police = _police_here(world, loc, collector)
    if police:
        cop = police[0]
        before_after = adjust_standing(world, collector, cop["faction"], POLICE_STANDING)
        notes.append(f"{cop['person']} is watching — no money changes hands here today "
                     f"({cop['faction']} standing {before_after:+.1f})")
        _remember(memory, cop["person"], f"You caught {collector} of {gang} trying to collect protection at {loc}.", loc)
        return {"collected": 0, "notes": notes}

    total = 0
    for b in businesses(world, loc):
        owner, fee = b["owner"], float(b["fee"])
        if owner == collector or owner in _gang_members(world, gang):
            continue
        mine = next((r for r in b["rackets"] if r["faction"] in gangs), None)
        theirs = next((r for r in b["rackets"] if r["faction"] not in gangs), None)
        if mine is None and theirs is None:
            # 无人罩的生意：开始收这家的数
            _set_dues(world, gang, b["name"], dues=fee, fee=fee)
            mine = {"faction": gang, "dues": fee}
            _add_turf(world, gang, loc, TURF_PER_NEW_RACKET)
            _chronicle(world, f"{gang} started taking protection money from {owner}'s {b['name']} at {loc}.",
                       [gang], loc, memory)
        if mine is not None:
            amount = min(int(float(mine["dues"])), cash_of(world, owner))
            if amount <= 0:
                notes.append(f"{owner} can't pay right now (owes {float(mine['dues']):.1f})"
                             if float(mine["dues"]) >= 1 else f"{owner} owes nothing yet")
                continue
            moved = transfer_cash(world, owner, collector, amount)
            _set_dues(world, mine["faction"], b["name"], dues=float(mine["dues"]) - moved, paid_delta=moved, fee=fee)
            _add_turf(world, mine["faction"], loc, TURF_PER_PAYMENT)
            boss = leader_of(world, mine["faction"])
            cut = transfer_cash(world, collector, boss, int(moved * BOSS_CUT)) if boss and boss != collector else 0
            total += moved
            notes.append(f"{owner} paid ${moved}" + (f" (${cut} goes up to {boss})" if cut else ""))
            _remember(memory, owner, f"You paid {collector} of {mine['faction']} ${moved} protection money at {loc}.", loc)
            _remember(memory, collector, f"You collected ${moved} from {owner} at {loc} for {mine['faction']}"
                      + (f"; ${cut} went up to {boss}." if cut else "."), loc)
            continue
        # 过界收数：抢对头的钱
        rival = theirs["faction"]
        amount = min(int(float(theirs["dues"])), cash_of(world, owner))
        if amount <= 0:
            notes.append(f"{owner} pays {rival}, and has nothing left for you")
            continue
        moved = transfer_cash(world, owner, collector, amount)
        _set_dues(world, rival, b["name"], dues=float(theirs["dues"]) - moved, fee=fee)
        _add_turf(world, gang, loc, TURF_PER_NEW_RACKET)
        after = adjust_standing(world, collector, rival, MUSCLE_IN_STANDING)
        _add_tension(world, gang, rival, MUSCLE_IN_TENSION)
        total += moved
        notes.append(f"muscled in on {rival}: {owner} paid ${moved} ({rival} standing {after:+.1f})")
        _chronicle(world, f"{collector} of {gang} took ${moved} from {owner} at {loc} — money {rival} counts as theirs.",
                   [gang, rival], loc, memory)
    if not notes:
        notes.append("No business here to collect from")
    return {"collected": total, "notes": notes}


def _gang_members(world, faction: str) -> set:
    return {r["p"] for r in _rows(world, "MATCH (p:Person)-[:MEMBER_OF]->(:Faction {name: $f}) RETURN p.name AS p",
                                  f=faction)}


# ---------------------------------------------------------------------------
# 每日结算：拖欠砸场、工钱、房租
# ---------------------------------------------------------------------------

def _owner_guilds(world, owner: str) -> List[str]:
    return [m.faction for m in memberships(world, owner) if m.direct and m.kind in ("guild", "crew")]


def settle_overdue(world, *, memory=None) -> List[Dict[str, Any]]:
    events = []
    for b in businesses(world):
        fee = float(b["fee"])
        for r in b["rackets"]:
            if float(r["dues"]) < fee * OVERDUE_DAYS:
                continue
            gang, owner = r["faction"], b["owner"]
            _rows(world, "MATCH (i:Item {name: $i}) SET i.stock = 0, i.smashed_by = $g", i=b["name"], g=gang)
            _rows(
                world,
                """
                MATCH (p:Person {name: $p})
                SET p.hp = CASE WHEN coalesce(p.hp, 100) > $hp THEN coalesce(p.hp, 100) - $hp ELSE 1 END,
                    p.mood = 'afraid'
                """,
                p=owner,
                hp=SMASH_HP,
            )
            _set_dues(world, gang, b["name"], dues=fee, fee=fee)
            for guild in _owner_guilds(world, owner):
                _add_tension(world, gang, guild, 1.0)
            ev = _chronicle(world, f"{gang} smashed up {owner}'s {b['name']} at {b['location']}: "
                                   f"{float(r['dues']):.0f} dollars behind on protection.",
                            [gang] + _owner_guilds(world, owner), b["location"], memory)
            _remember(memory, owner, f"{gang} smashed your {b['name']} because you were behind on protection money.",
                      b["location"])
            if ev:
                events.append(ev)
    return events


def pay_wages(world, *, memory=None) -> List[str]:
    notes = []
    rows = _rows(
        world,
        """
        MATCH (f:Faction)
        WHERE coalesce(f.wage, 0) > 0 AND coalesce(f.status, 'active') = 'active'
        RETURN f.name AS f, toInteger(f.wage) AS w
        """,
    )
    for r in rows:
        boss = leader_of(world, r["f"])
        if not boss:
            continue
        for m in sorted(_gang_members(world, r["f"]) - {boss}):
            moved = transfer_cash(world, boss, m, int(r["w"]))
            if moved:
                notes.append(f"{boss} paid {m} ${moved} ({r['f']})")
                _remember(memory, m, f"{boss} paid you ${moved} for your work with {r['f']}.")
            else:
                notes.append(f"{boss} couldn't pay {m} ({r['f']})")
                _remember(memory, m, f"{boss} had no money to pay you today.")
    return notes


def collect_rent(world, *, memory=None) -> List[str]:
    notes = []
    rows = _rows(
        world,
        """
        MATCH (i:Item {item_kind: 'rent_book'})
        OPTIONAL MATCH (i)-[:LOCATED_AT]->(l:Location)
        RETURN i.name AS name, i.landlord AS landlord, coalesce(i.rent, 1) AS rent,
               i.tenants AS tenants, coalesce(i.arrears, '{}') AS arrears, l.name AS location
        """,
    )
    for r in rows:
        landlord = r.get("landlord") or ""
        tenants = r.get("tenants") or []
        if isinstance(tenants, str):
            try:
                tenants = json.loads(tenants)
            except ValueError:
                tenants = []
        try:
            arrears = json.loads(r.get("arrears") or "{}")
        except ValueError:
            arrears = {}
        if not landlord:
            continue
        for t in tenants:
            if t == landlord:
                continue
            moved = transfer_cash(world, t, landlord, int(r["rent"]))
            if moved:
                arrears.pop(t, None)
                notes.append(f"{t} paid {landlord} ${moved} rent")
            else:
                arrears[t] = int(arrears.get(t, 0)) + 1
                notes.append(f"{t} is {arrears[t]} day(s) behind on rent to {landlord}")
                _remember(memory, t, f"You couldn't pay your rent to {landlord} today ({arrears[t]} days behind).",
                          r.get("location") or "")
        _rows(world, "MATCH (i:Item {name: $i}) SET i.arrears = $a", i=r["name"], a=json.dumps(arrears))
    return notes


# ---------------------------------------------------------------------------
# 时钟
# ---------------------------------------------------------------------------

def _meta(world) -> Dict[str, Any]:
    rows = _rows(
        world,
        "MATCH (m:WorldMeta {name: 'WorldMeta'}) RETURN m.econ_tick_t AS t, coalesce(m.econ_day, 0) AS d LIMIT 1",
    )
    return dict(rows[0]) if rows else {}


def _set_meta(world, **props) -> None:
    sets = ", ".join(f"m.{k} = ${k}" for k in props)
    _rows(world, f"MERGE (m:WorldMeta {{name: 'WorldMeta'}}) SET {sets}", **props)


def has_economy(world) -> bool:
    rows = _rows(world, "MATCH (c:Item) WHERE c.item_kind = 'cash' RETURN count(c) AS n")
    return bool(rows and int(rows[0].get("n") or 0))


def maybe_tick(world, sim_time: float, *, memory=None) -> Dict[str, Any]:
    """与势力同节奏：每轮累计保护费；跨过一天时结算拖欠、工钱、房租。"""
    if world is None or os.getenv("ECONOMY_SIM", "1").strip().lower() in ("0", "false", "no"):
        return {}
    if not has_economy(world):
        return {}
    from faction_sim import day_of, tick_period

    meta = _meta(world)
    last = meta.get("t")
    if last is None:
        _set_meta(world, econ_tick_t=float(sim_time), econ_day=day_of(sim_time))
        return {}
    period = tick_period()
    due = int((float(sim_time) - float(last)) // period)
    if due <= 0:
        return {}
    hours = period * due * 24.0 / _day_length()
    accrue_dues(world, hours)
    report: Dict[str, Any] = {"events": [], "notes": []}
    day = day_of(sim_time)
    if day > int(meta.get("d") or 0):
        report["events"] += settle_overdue(world, memory=memory)
        report["notes"] += pay_wages(world, memory=memory)
        report["notes"] += collect_rent(world, memory=memory)
    _set_meta(world, econ_tick_t=float(last) + period * due, econ_day=day)
    return report


def _day_length() -> float:
    try:
        from routine import DAY_LENGTH_SECONDS

        return float(DAY_LENGTH_SECONDS)
    except Exception:
        return 300.0


def on_action(world, action_name: str, args: List[str], *, memory=None) -> List[str]:
    """动作执行成功后的经济后果（目前：collect_dues）。"""
    if action_name != "collect_dues" or not args:
        return []
    res = collect(world, args[0], memory=memory)
    return res["notes"]


# ---------------------------------------------------------------------------
# 给 prompt / UI 看
# ---------------------------------------------------------------------------

def economy_text(world, person: str) -> str:
    if not has_economy(world):
        return ""
    lines = [f"Money: you have ${cash_of(world, person)}. Buying food or drink puts your money in the owner's pocket."]
    gangs = set(gangs_of(world, person))
    for b in businesses(world):
        for r in b["rackets"]:
            if r["faction"] in gangs and float(r["dues"]) >= 1:
                lines.append(f"{b['owner']}'s {b['name']} at {b['location']} owes {r['faction']} "
                             f"${float(r['dues']):.0f} (go there and collect_dues)")
        if b["owner"] == person:
            for r in b["rackets"]:
                lines.append(f"You owe {r['faction']} ${float(r['dues']):.1f} protection for {b['name']} "
                             f"(fee ${float(b['fee']):.0f}/day; {OVERDUE_DAYS:.0f} days behind and they smash the place).")
            if not b["rackets"]:
                lines.append(f"Nobody takes protection from your {b['name']} right now.")
            lines.append(f"{b['name']} has {b['stock']} left in stock.")
    return "\n".join(lines[:6])


def street_summary(world, person: str) -> Dict[str, Any]:
    if not has_economy(world):
        return {}
    rows = _rows(
        world,
        """
        MATCH (p:Person)-[:HAS_ITEM]->(c:Item) WHERE c.item_kind = 'cash'
        RETURN p.name AS p, count(c) AS n ORDER BY n DESC
        """,
    )
    return {
        "cash": cash_of(world, person),
        "richest": [{"name": r["p"], "cash": int(r["n"])} for r in rows[:5]],
        "money_supply": sum(int(r["n"]) for r in rows),
        "businesses": [
            {
                "name": b["name"], "owner": b["owner"], "location": b["location"], "stock": b["stock"],
                "fee": b["fee"], "takings": b["takings"],
                "protected_by": [r["faction"] for r in b["rackets"]],
                "dues": round(sum(float(r["dues"]) for r in b["rackets"]), 1),
            }
            for b in businesses(world)
        ],
    }
