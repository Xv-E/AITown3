"""
战斗与敌对。

敌对（hostility(a, b) 返回 a 为什么把 b 当敌人，"" = 不是）：
  · "attacked me"   b 刚打过 a（COMBAT_MEMORY 之内）
  · "fighting"      a 刚打过 b，还在打
  · "faction"       两人阵营不同，而且有一方是见人就打的阵营（HOSTILE_FACTIONS，比如 monster / bandit）
  · "criminal"      a 是守卫（Person.guard），亲眼看见 b 偷过东西 / 无故打人
  · "hatred"        a 对 b 的好感 ≤ HATRED
  · "war"           两人的联盟互相敌对（relations.hostile）
前四种会主动动手（AGGRESSIVE）；后两种只在自己明显更强时才动手，否则躲着走。

一击（strike）：伤害 = 力量（power_level）+ 手上最好的武器（Item.damage）- 对方的护甲（Item.armor），至少 1。
HP 掉到 0 就死了：手上的东西掉在地上，名下的东西都没主了（谁捡归谁）。
打人之后：挨打的人好感 -4，他的盟友 -3；看见的人 -2——除非挨打的本来就是看的人的敌人。
没被惹就先动手（对方不是你的敌人）算犯罪，看见的人记下来，守卫会来找你。
不打仗的时候 HP 慢慢回来（regen）。

NPC 的反应见 react()：打得过就打（不在一个地点就先走过去），快不行了就逃到旁边没敌人的地点。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

HOSTILE_FACTIONS = {"monster", "monsters", "bandit", "bandits", "beast", "beasts", "undead", "raider", "raiders"}
AGGRESSIVE = {"attacked me", "fighting", "faction", "criminal"}
HATRED = -6
COMBAT_MEMORY = 25.0      # 模拟秒（两个游戏小时）：这么久之内打过你的人还是敌人
HURT_VICTIM = -4
HURT_ALLY = -3
HURT_WITNESS = -2
FLEE_AT = 0.3             # HP 掉到最大值的这么多以下，又打不过，就逃
BRAVE_RATIO = 1.3         # 只是讨厌（不是被打）的话，强出这么多才动手
REGEN = 8                 # 每两个游戏小时回这么多 HP（最近没挨打的人）


def stats(world, name: str) -> Dict[str, Any]:
    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})
        OPTIONAL MATCH (p)-[:HAS_ITEM]->(i:Item)
        OPTIONAL MATCH (p)-[:LOCATED_AT]->(l:Location)
        RETURN coalesce(p.hp, 0) AS hp, coalesce(p.max_hp, 100) AS max_hp,
               coalesce(p.power_level, 1) AS power, coalesce(p.dead, false) AS dead,
               coalesce(p.faction, '') AS faction, coalesce(p.guard, false) AS guard,
               coalesce(p.is_player, false) OR p.role = 'player' AS player,
               p.attacked_by AS attacked_by, p.attacked_at AS attacked_at,
               p.fighting AS fighting, p.fighting_at AS fighting_at,
               l.name AS location,
               max(coalesce(i.damage, 0)) AS weapon, sum(coalesce(i.armor, 0)) AS armor
        """,
        n=name,
    ) or []
    if not rows:
        return {}
    r = dict(rows[0])
    r["hp"] = int(r.get("hp") or 0)
    r["max_hp"] = max(int(r.get("max_hp") or 100), r["hp"], 1)
    r["power"] = int(r.get("power") or 1)
    r["weapon"] = int(r.get("weapon") or 0)
    r["armor"] = int(r.get("armor") or 0)
    r["alive"] = not r.get("dead") and r["hp"] > 0
    return r


def damage_of(attacker: Dict[str, Any], target: Dict[str, Any]) -> int:
    return max(1, attacker["power"] + attacker["weapon"] - target["armor"])


def strength(s: Dict[str, Any]) -> float:
    """大概打得过谁：每一击的伤害 × 能挨几下。"""
    return float(max(1, s["power"] + s["weapon"])) * float(max(1, s["hp"] + s["armor"] * 3))


def now(world) -> float:
    """模拟时钟（world_systems 每拍写在 WorldMeta 上）。"""
    rows = world.run_query("MATCH (m:WorldMeta {name: 'WorldMeta'}) RETURN m.sim_time AS t LIMIT 1") or []
    return float(rows[0]["t"] or 0.0) if rows and rows[0].get("t") is not None else 0.0


def _recent(when: Any, at: float) -> bool:
    return when is not None and at - float(when) <= COMBAT_MEMORY


def hostility(world, a: str, b: str, *, at: Optional[float] = None) -> str:
    if not a or not b or a == b:
        return ""
    at = now(world) if at is None else float(at)
    sa, sb = stats(world, a), stats(world, b)
    if not sa or not sb or not sa["alive"] or not sb["alive"]:
        return ""
    if sa.get("attacked_by") == b and _recent(sa.get("attacked_at"), at):
        return "attacked me"
    if sa.get("fighting") == b and _recent(sa.get("fighting_at"), at):
        return "fighting"
    fa, fb = str(sa.get("faction") or ""), str(sb.get("faction") or "")
    if fa != fb and (fa in HOSTILE_FACTIONS or fb in HOSTILE_FACTIONS):
        return "faction"
    if sa.get("guard"):
        from ownership import crimes_seen

        if crimes_seen(world, a, b):
            return "criminal"
    from relations import feeling, hostile

    if feeling(world, a, b) <= HATRED:
        return "hatred"
    if hostile(world, a, b):
        return "war"
    return ""


def _drop_everything(world, name: str, location: str) -> List[str]:
    rows = world.run_query(
        """
        MATCH (p:Person {name: $n})-[h:HAS_ITEM]->(i:Item)
        DELETE h
        RETURN i.name AS i
        """,
        n=name,
    ) or []
    dropped = [r["i"] for r in rows]
    if not location:
        return dropped
    near = None
    try:
        import spatial

        cell = spatial.person_cell(world, name)
        area = spatial.region_areas(world).get(location)
        if cell and area:
            near = (cell[0] - area[0], cell[1] - area[1])
    except Exception:  # noqa: BLE001
        spatial = None  # type: ignore[assignment]
    for item in dropped:
        world.run_query(
            "MATCH (i:Item {name: $i}), (l:Location {name: $l}) REMOVE i.x, i.y MERGE (i)-[:LOCATED_AT]->(l)",
            i=item, l=location,
        )
        if spatial is not None:
            try:
                spatial.place_item(world, item, near=near)
            except Exception:  # noqa: BLE001
                pass
    return dropped


def die(world, name: str) -> List[str]:
    """死了：东西掉在地上、都没主了。返回掉下的东西。"""
    from ownership import release_all

    s = stats(world, name)
    world.run_query("MATCH (p:Person {name: $n}) SET p.dead = true, p.hp = 0", n=name)
    dropped = _drop_everything(world, name, str(s.get("location") or ""))
    release_all(world, name)
    return dropped


def strike(world, attacker: str, target: str, *, at: Optional[float] = None) -> Dict[str, Any]:
    """attacker 打 target 一下（attack 动作的 strike 操作）。"""
    at = now(world) if at is None else float(at)
    sa, st = stats(world, attacker), stats(world, target)
    if not sa or not st or not st["alive"]:
        return {"damage": 0, "hp": st.get("hp", 0) if st else 0, "killed": False}
    provoked = hostility(world, attacker, target, at=at)  # 动手之前有没有理由（打完就成了「在打架」）
    dmg = damage_of(sa, st)
    hp = max(0, st["hp"] - dmg)
    world.run_query(
        """
        MATCH (t:Person {name: $t}) SET t.hp = $hp, t.max_hp = coalesce(t.max_hp, $max),
              t.attacked_by = $a, t.attacked_at = $at
        """,
        t=target, hp=hp, max=st["max_hp"], a=attacker, at=float(at),
    )
    world.run_query(
        "MATCH (a:Person {name: $a}) SET a.fighting = $t, a.fighting_at = $at, a.strike_provoked = $why",
        a=attacker, t=target, at=float(at), why=provoked,
    )
    out: Dict[str, Any] = {"damage": dmg, "hp": hp, "killed": False, "provoked": provoked}
    if hp <= 0:
        out["killed"] = True
        out["dropped"] = die(world, target)
    return out


def after_attack(world, attacker: str, target: str, *, at: float = 0.0, provoked: Optional[str] = None) -> Dict[str, Any]:
    """打完人之后：谁对打人的人有看法、算不算犯罪。provoked：动手前打人的人有没有理由（hostility）。"""
    from ownership import record_crime, witnesses
    from relations import adjust, allies_of

    adjust(world, target, attacker, HURT_VICTIM, reason="attacked me", at=at)
    for ally in allies_of(world, target):
        if ally != attacker:
            adjust(world, ally, attacker, HURT_ALLY, reason=f"attacked {target}", at=at)
    seen = [w for w in witnesses(world, attacker) if w != target]
    for w in seen:
        if not hostility(world, w, target, at=at):
            adjust(world, w, attacker, HURT_WITNESS, reason=f"saw them attack {target}", at=at)
    crime = False
    if provoked is None:
        rows = world.run_query(
            "MATCH (a:Person {name: $a}) RETURN coalesce(a.strike_provoked, '') AS why", a=attacker
        ) or []
        provoked = str(rows[0]["why"]) if rows else ""
    if not provoked:
        # 没被惹就动手：看见的人（包括挨打的）都记下来
        record_crime(world, attacker, "assault", victim=target, seen_by=seen + [target], at=at)
        crime = True
    return {"witnesses": seen, "crime": crime}


def regen(world, *, at: float = 0.0) -> int:
    """没在打仗的活人回一点 HP。返回回了血的人数。"""
    rows = world.run_query(
        """
        MATCH (p:Person)
        WHERE coalesce(p.dead, false) = false AND coalesce(p.hp, 0) > 0
          AND coalesce(p.hp, 0) < coalesce(p.max_hp, 100)
          AND (p.attacked_at IS NULL OR $at - p.attacked_at > $mem)
        SET p.hp = CASE WHEN p.hp + $r > coalesce(p.max_hp, 100) THEN coalesce(p.max_hp, 100) ELSE p.hp + $r END
        RETURN count(p) AS n
        """,
        at=float(at), mem=COMBAT_MEMORY, r=REGEN,
    ) or []
    return int(rows[0]["n"]) if rows else 0


# ---------------------------------------------------------------------
# NPC 怎么反应
# ---------------------------------------------------------------------


def enemies_near(world, me: str, *, at: float = 0.0) -> List[Tuple[str, str]]:
    """看得见的敌人：[(名字, 为什么)]，最近打过我的排前面。"""
    from ownership import witnesses

    out = []
    for other in witnesses(world, me):  # 看得见我的人 = 我看得见的人
        why = hostility(world, me, other, at=at)
        if why:
            out.append((other, why))
    out.sort(key=lambda p: (p[1] != "attacked me", p[1] != "fighting", p[0]))
    return out


def _neighbours(world, here: str) -> List[str]:
    rows = world.run_query(
        "MATCH (:Location {name: $l})-[:ADJACENT_TO]->(n:Location) RETURN n.name AS n ORDER BY n", l=here
    ) or []
    return [r["n"] for r in rows]


def _hostiles_at(world, me: str, place: str, *, at: float) -> int:
    rows = world.run_query(
        "MATCH (p:Person)-[:LOCATED_AT]->(:Location {name: $l}) WHERE p.name <> $me RETURN p.name AS n",
        l=place, me=me,
    ) or []
    return sum(1 for r in rows if hostility(world, r["n"], me, at=at))


def react(world, me: str, *, at: float = 0.0) -> Optional[Dict[str, Any]]:
    """这个 NPC 现在该打、该逃，还是不管：

      {"kind": "attack", "target": 谁, "why": ..., "plan": [(动作, 参数), ...]}
      {"kind": "flee", "from": 谁, "to": 地点, "plan": [...]}
      None：没有敌人，或者只是讨厌、又打不过（不招惹）
    """
    from registry import act

    sm = stats(world, me)
    if not sm or not sm["alive"] or sm.get("player"):
        return None
    enemies = enemies_near(world, me, at=at)
    if not enemies:
        return None
    here = str(sm.get("location") or "")
    target, why = enemies[0]
    st = stats(world, target)
    if not st:
        return None
    mine = strength(sm)
    threat = sum(strength(stats(world, e)) for e, w in enemies if w in AGGRESSIVE or hostility(world, e, me, at=at))
    losing = threat > mine
    if sm["hp"] <= FLEE_AT * sm["max_hp"] and losing and here:
        options = sorted(_neighbours(world, here), key=lambda p: (_hostiles_at(world, me, p, at=at), p))
        if options:
            dest = options[0]
            return {"kind": "flee", "from": target, "to": dest, "why": why,
                    "plan": [(act("travel"), [me, here, dest])]}
        return None
    if why not in AGGRESSIVE and mine < BRAVE_RATIO * strength(st):
        return None  # 只是讨厌、又不占上风：不招惹
    there = str(st.get("location") or "")
    if not here or not there:
        return None
    plan: List[Tuple[str, List[str]]] = []
    if there != here:
        if there not in _neighbours(world, here):
            return None
        plan.append((act("travel"), [me, here, there]))
    plan.append((act("attack"), [me, target, there]))
    return {"kind": "attack", "target": target, "why": why, "plan": plan}
