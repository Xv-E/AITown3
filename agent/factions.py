"""
势力 — 家庭、帮派、行会、城镇……大小不一，可以嵌套。

图结构：
  (:Faction {name, kind, scale, goal, description, values})
  (:Person)-[:MEMBER_OF {role, loyalty 0..10}]->(:Faction)
  (:Faction)-[:PART_OF]->(:Faction)                 小势力属于大势力（家 → 镇）
  (:Faction)-[:FACTION_REL {stance, reason}]-(:Faction)  allied|friendly|neutral|rival|hostile（无向读取）
  (:Person)-[:STANDING {value -10..10}]->(:Faction)  某势力对某人的看法（声望）

机制：
  - 成员身份沿 PART_OF 向上继承（忠诚度减半），所以镇与帮派的敌对也落到每个家庭成员身上。
  - attitude(me, other)：我因为各自的势力对对方的倾向（共同势力 / 势力立场 / 对方在我方的声望）。
  - ripple(actor, target, delta)：对某人做的事（送礼、攻击、谈话结果、兑现人情），
    会传到对方所在的每个势力，并按立场波及其盟友与敌人。小势力（家、帮派）反应最强烈。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

STANCE_SCORE = {"allied": 3.0, "friendly": 1.0, "neutral": 0.0, "rival": -2.0, "hostile": -4.0}
# 同属一个势力时的亲近加成（按种类优先，其次按规模）
SHARED_BONUS_KIND = {"family": 4.0, "gang": 3.0, "crew": 3.0}
SHARED_BONUS_SCALE = {"small": 3.0, "medium": 2.0, "large": 1.0}
# 规模越小，成员越把势力的事当成自己的事
SCALE_WEIGHT = {"small": 1.0, "medium": 0.8, "large": 0.6}
# 对某个成员做的事，多少传到其势力的声望上
RIPPLE_SHARE = {"small": 0.5, "medium": 0.3, "large": 0.15}
STANCE_RIPPLE = {"allied": 0.5, "friendly": 0.25, "rival": -0.25, "hostile": -0.5}
STANDING_WEIGHT = 0.3
INHERITED_LOYALTY = 0.5
STANDING_MIN, STANDING_MAX = -10.0, 10.0

# 动作对目标本人的分量（正：帮助；负：伤害）
ACTION_DELTA = {"give": 2.0, "attack": -6.0}


@dataclass
class Membership:
    faction: str
    kind: str = "other"
    scale: str = "medium"
    role: str = "member"
    loyalty: float = 6.0
    goal: str = ""
    description: str = ""
    via: str = ""  # 继承自哪个子势力（直接成员为空）

    @property
    def direct(self) -> bool:
        return not self.via


def _rows(world, query: str, **params) -> List[Dict[str, Any]]:
    if world is None:
        return []
    try:
        return world.run_query(query, **params) or []
    except Exception:
        return []


def memberships(world, person: str) -> List[Membership]:
    """直接成员身份 + 沿 PART_OF 继承的上级势力。"""
    rows = _rows(
        world,
        """
        MATCH (p:Person {name: $n})-[m:MEMBER_OF]->(f:Faction)
        OPTIONAL MATCH path = (f)-[:PART_OF*1..3]->(up:Faction)
        RETURN f.name AS f, coalesce(f.kind, 'other') AS fk, coalesce(f.scale, 'medium') AS fs,
               coalesce(f.goal, '') AS fg, coalesce(f.description, '') AS fd,
               coalesce(m.role, 'member') AS role, coalesce(m.loyalty, 6.0) AS loyalty,
               up.name AS up, coalesce(up.kind, 'other') AS uk, coalesce(up.scale, 'medium') AS us,
               coalesce(up.goal, '') AS ug, coalesce(up.description, '') AS ud,
               CASE WHEN path IS NULL THEN 0 ELSE length(path) END AS depth
        """,
        n=person,
    )
    out: Dict[str, Membership] = {}
    for r in rows:
        name = r.get("f")
        if not name:
            continue
        loyalty = float(r.get("loyalty") or 0)
        out[name] = Membership(
            faction=name, kind=r.get("fk") or "other", scale=r.get("fs") or "medium",
            role=r.get("role") or "member", loyalty=loyalty,
            goal=r.get("fg") or "", description=r.get("fd") or "",
        )
    for r in rows:
        up = r.get("up")
        if not up or up in out:
            continue
        depth = max(1, int(r.get("depth") or 1))
        out[up] = Membership(
            faction=up, kind=r.get("uk") or "other", scale=r.get("us") or "medium",
            role="member", loyalty=float(r.get("loyalty") or 0) * (INHERITED_LOYALTY ** depth),
            goal=r.get("ug") or "", description=r.get("ud") or "", via=r.get("f") or "",
        )
    return sorted(out.values(), key=lambda m: (not m.direct, -m.loyalty, m.faction))


def faction_relations(world, names_a: Iterable[str], names_b: Iterable[str]) -> List[Dict[str, str]]:
    """两组势力之间的立场（无向；同一对只取一条）。"""
    a, b = sorted(set(names_a)), sorted(set(names_b))
    if not a or not b:
        return []
    rows = _rows(
        world,
        """
        MATCH (x:Faction)-[r:FACTION_REL]-(y:Faction)
        WHERE x.name IN $a AND y.name IN $b
        RETURN x.name AS a, y.name AS b, coalesce(r.stance, 'neutral') AS stance,
               coalesce(r.reason, '') AS reason
        """,
        a=a,
        b=b,
    )
    seen: set = set()
    out: List[Dict[str, str]] = []
    for r in rows:
        key = (r.get("a"), r.get("b"))
        if not all(key) or key in seen or key[0] == key[1]:
            continue
        seen.add(key)
        out.append({"a": key[0], "b": key[1], "stance": str(r.get("stance") or "neutral"),
                    "reason": str(r.get("reason") or "")})
    return out


def stances_of(world, faction: str) -> List[Dict[str, str]]:
    """某势力与所有势力的立场。"""
    rows = _rows(
        world,
        """
        MATCH (x:Faction {name: $f})-[r:FACTION_REL]-(y:Faction)
        RETURN DISTINCT y.name AS other, coalesce(r.stance, 'neutral') AS stance
        """,
        f=faction,
    )
    return [{"other": r["other"], "stance": str(r.get("stance") or "neutral")}
            for r in rows if r.get("other") and r["other"] != faction]


def standing(world, person: str, faction: str) -> float:
    rows = _rows(
        world,
        """
        MATCH (:Person {name: $p})-[s:STANDING]->(:Faction {name: $f})
        RETURN coalesce(s.value, 0.0) AS v LIMIT 1
        """,
        p=person,
        f=faction,
    )
    return float(rows[0]["v"]) if rows else 0.0


def standings(world, person: str) -> Dict[str, float]:
    rows = _rows(
        world,
        """
        MATCH (:Person {name: $p})-[s:STANDING]->(f:Faction)
        RETURN f.name AS f, coalesce(s.value, 0.0) AS v
        """,
        p=person,
    )
    return {r["f"]: float(r["v"]) for r in rows if r.get("f")}


def adjust_standing(world, person: str, faction: str, delta: float) -> float:
    cur = standing(world, person, faction)
    new = round(max(STANDING_MIN, min(STANDING_MAX, cur + float(delta))), 2)
    _rows(
        world,
        """
        MATCH (p:Person {name: $p}), (f:Faction {name: $f})
        MERGE (p)-[s:STANDING]->(f)
        SET s.value = $v
        """,
        p=person,
        f=faction,
        v=new,
    )
    return new


def standing_label(value: float) -> str:
    if value >= 6:
        return "trusted"
    if value >= 3:
        return "welcome"
    if value > -1:
        return "unknown"
    if value > -4:
        return "distrusted"
    return "hated"


def attitude_label(score: float) -> str:
    if score >= 4:
        return "loyal / protective"
    if score >= 1.5:
        return "friendly"
    if score > -1.5:
        return "neutral"
    if score > -4:
        return "wary"
    return "hostile"


def _shared_bonus(m: Membership) -> float:
    return SHARED_BONUS_KIND.get(m.kind, SHARED_BONUS_SCALE.get(m.scale, 2.0))


@dataclass
class PairView:
    me: str
    other: str
    mine: List[Membership]
    theirs: List[Membership]
    shared: List[str]
    cross: List[Dict[str, str]]  # a ∈ 我方，b ∈ 对方
    their_standing: Dict[str, float]  # 对方在我方各势力的声望
    score: float = 0.0
    reasons: List[str] = None  # type: ignore[assignment]

    @property
    def label(self) -> str:
        return attitude_label(self.score)


def pair_view(world, me: str, other: str) -> PairView:
    mine = memberships(world, me)
    theirs = memberships(world, other)
    mine_by = {m.faction: m for m in mine}
    their_names = {m.faction for m in theirs}
    shared = sorted(set(mine_by) & their_names)
    cross = faction_relations(
        world, [n for n in mine_by if n not in shared], [n for n in their_names if n not in shared]
    )
    their_standing = {
        f: standing(world, other, f) for f in mine_by if f not in their_names
    }

    score = 0.0
    reasons: List[str] = []
    for f in shared:
        m = mine_by[f]
        bonus = _shared_bonus(m) * m.loyalty / 10.0
        score += bonus
        reasons.append(f"both of {f} ({m.kind})")
    for c in cross:
        m = mine_by.get(c["a"])
        if m is None:
            continue
        part = STANCE_SCORE.get(c["stance"], 0.0) * m.loyalty / 10.0 * SCALE_WEIGHT.get(m.scale, 0.8)
        if part:
            score += part
            reasons.append(f"your {c['a']} is {c['stance']} with their {c['b']}")
    for f, v in their_standing.items():
        m = mine_by[f]
        part = v * STANDING_WEIGHT * m.loyalty / 10.0
        if abs(part) >= 0.1:
            score += part
            reasons.append(f"{f} thinks of them as {standing_label(v)}")
    score = round(max(-10.0, min(10.0, score)), 1)
    return PairView(me, other, mine, theirs, shared, cross, their_standing, score, reasons)


def _membership_line(m: Membership) -> str:
    via = f", through {m.via}" if m.via else ""
    goal = f" — goal: {m.goal}" if m.goal else ""
    return f"{m.faction} ({m.kind}, {m.scale}; {m.role}, loyalty {m.loyalty:.0f}/10{via}){goal}"


def faction_secret_lines(world, me: str, view: PairView) -> List[str]:
    """我知道的势力内情，按对方与该势力的关系给出分享方式。"""
    rows = _rows(
        world,
        """
        MATCH (:Person {name: $n})-[:KNOWS_FACT]->(f:Fact)
        WHERE coalesce(f.faction, '') <> ''
        RETURN f.name AS name, f.faction AS faction
        """,
        n=me,
    )
    if not rows:
        return []
    their = {m.faction for m in view.theirs}
    out: List[str] = []
    for r in rows:
        fac = r.get("faction")
        if not fac:
            continue
        if fac in their:
            how = "they are one of yours; you may share it"
        else:
            stance = next(
                (s["stance"] for s in faction_relations(world, [fac], their)
                 if STANCE_SCORE.get(s["stance"], 0) != 0),
                "",
            )
            if stance in ("rival", "hostile"):
                how = f"they side with {fac}'s enemies; never tell them"
            elif stance in ("allied", "friendly"):
                how = "an ally; share if it helps both sides"
            else:
                how = "an outsider; only if they have earned real trust"
        out.append(f"[{r.get('name')}] belongs to {fac}: {how}")
    return out


FACTION_CRAFT = (
    "Faction loyalty shapes this talk: protect your own people, never hand rivals your side's "
    "secrets, and remember that what is done to one member, their whole faction hears about. "
    "Family and gang ties are personal; town-wide ties are looser."
)


def faction_block(world, me: str, other: str, view: Optional[PairView] = None) -> str:
    """对话 prompt 用：双方势力、彼此立场、我方对对方的看法、内情分享规则。"""
    view = view or pair_view(world, me, other)
    if not view.mine and not view.theirs:
        return ""
    lines: List[str] = []
    if view.mine:
        lines.append("Your factions:")
        lines += [f"- {_membership_line(m)}" for m in view.mine[:4]]
    else:
        lines.append("Your factions: none — you stand alone.")
    if view.theirs:
        lines.append(f"{other}'s factions: " + "; ".join(
            f"{m.faction} ({m.kind}, {m.role})" for m in view.theirs[:4]
        ))
    else:
        lines.append(f"{other} belongs to no faction you know of.")
    for f in view.shared:
        lines.append(f"You and {other} are both of {f}.")
    for c in view.cross:
        why = f" — {c['reason']}" if c.get("reason") else ""
        lines.append(f"Your {c['a']} is {c['stance']} with their {c['b']}{why}.")
    for f, v in view.their_standing.items():
        if abs(v) >= 0.5:
            lines.append(f"{f} regards {other} as {standing_label(v)} ({v:+.1f}).")
    lines.append(f"Because of all this you lean {view.label} toward {other} (faction score {view.score:+.1f}).")
    secrets = faction_secret_lines(world, me, view)
    if secrets:
        lines.append("Faction matters you know:")
        lines += [f"- {s}" for s in secrets[:4]]
    lines.append(FACTION_CRAFT)
    return "\n".join(lines)


def faction_situation_text(world, me: str, people_here: Iterable[str]) -> str:
    """决策 prompt 用：我的势力与目标 + 在场者对我方的立场。"""
    mine = memberships(world, me)
    lines: List[str] = []
    if mine:
        lines.append("Your factions (their goals are yours too, as far as your loyalty goes):")
        lines += [f"- {_membership_line(m)}" for m in mine[:4]]
        for m in mine:
            if not m.direct:
                continue
            foes = [s for s in stances_of(world, m.faction) if s["stance"] in ("rival", "hostile")]
            if foes:
                lines.append(f"  {m.faction} opposes: " + ", ".join(f"{s['other']} ({s['stance']})" for s in foes))
    for other in people_here:
        view = pair_view(world, me, other)
        if not view.reasons:
            continue
        lines.append(f"{other}: you lean {view.label} — " + "; ".join(view.reasons[:3]))
    return "\n".join(lines)


def ripple(world, actor: str, target: str, delta: float, *, why: str = "") -> List[str]:
    """actor 对 target 做了分量为 delta 的事 → 更新 actor 在相关势力的声望。返回变化说明。"""
    if world is None or not actor or not target or actor == target or not delta:
        return []
    targets = memberships(world, target)
    if not targets:
        return []
    actor_in = {m.faction for m in memberships(world, actor)}
    changes: Dict[str, float] = {}
    for m in targets:
        share = RIPPLE_SHARE.get(m.scale, 0.3)
        if not m.direct:
            share *= INHERITED_LOYALTY
        if m.faction in actor_in and delta < 0:
            share *= 1.5  # 自己人对自己人下手：背叛
        changes[m.faction] = changes.get(m.faction, 0.0) + delta * share
    target_names = {m.faction for m in targets}
    for m in targets:
        if not m.direct:
            continue
        share = RIPPLE_SHARE.get(m.scale, 0.3)
        for s in stances_of(world, m.faction):
            k = STANCE_RIPPLE.get(s["stance"])
            if not k or s["other"] in target_names:
                continue
            changes[s["other"]] = changes.get(s["other"], 0.0) + delta * share * k
    notes: List[str] = []
    for fac, d in sorted(changes.items()):
        if abs(d) < 0.05:
            continue
        before = standing(world, actor, fac)
        after = adjust_standing(world, actor, fac, d)
        if abs(after - before) >= 0.5:
            tail = f" ({why})" if why else ""
            notes.append(f"{fac}: {actor} {before:+.1f}→{after:+.1f}{tail}")
    return notes


def on_action(world, capability: Optional[str], args: List[str]) -> List[str]:
    """动作执行成功后的势力后果（give / attack）。args 为动作的位置参数。"""
    if world is None or not capability or capability not in ACTION_DELTA:
        return []
    if capability == "give" and len(args) >= 3:
        actor, target = args[0], args[2]
        why = f"gave {args[1]} to {target}"
    elif capability == "attack" and len(args) >= 2:
        actor, target = args[0], args[1]
        why = f"attacked {target}"
        _personal_grudge(world, actor, target)
    else:
        return []
    return ripple(world, actor, target, ACTION_DELTA[capability], why=why)


def _personal_grudge(world, actor: str, target: str) -> None:
    """玩家打人：被打的人自己也记仇（个人信任只记对玩家的）。"""
    rows = _rows(
        world,
        "MATCH (p:Person {name: $n}) RETURN coalesce(p.is_player, false) AS ip, coalesce(p.role, '') AS role",
        n=actor,
    )
    if rows and (rows[0].get("ip") or rows[0].get("role") == "player"):
        try:
            from npc_knowledge import adjust_trust

            adjust_trust(world, target, -3)
        except Exception:
            pass


def player_faction_summary(world, player: str) -> List[Dict[str, Any]]:
    """UI：每个势力 + 玩家在其中的身份与声望。"""
    rows = _rows(
        world,
        """
        MATCH (f:Faction)
        OPTIONAL MATCH (f)-[:PART_OF]->(par:Faction)
        RETURN f.name AS name, coalesce(f.kind, 'other') AS kind,
               coalesce(f.scale, 'medium') AS scale, coalesce(f.goal, '') AS goal,
               par.name AS parent
        ORDER BY name
        """,
    )
    mine = {m.faction: m for m in memberships(world, player)}
    stand = standings(world, player)
    out: List[Dict[str, Any]] = []
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        v = stand.get(name, 0.0)
        m = mine.get(name)
        out.append({
            "name": name,
            "kind": r.get("kind"),
            "scale": r.get("scale"),
            "goal": r.get("goal") or "",
            "parent": r.get("parent") or "",
            "member": bool(m),
            "role": m.role if m else "",
            "standing": round(v, 1),
            "label": "member" if m else standing_label(v),
        })
    return out


def membership_summary(world, person: str) -> str:
    return "; ".join(
        f"{m.faction} ({m.kind}, {m.role}{', via ' + m.via if m.via else ''})"
        for m in memberships(world, person)
    )
