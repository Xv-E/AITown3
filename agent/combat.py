"""
战斗与敌对。

敌对（hostility(a, b) 返回 a 为什么把 b 当敌人，"" = 不是）——引擎只是问内容包规则的 hostility/3
（base 包见 content/base/rules/social.pl），原因、标签和解释都由规则给出。base 包的原因（按优先级）：
  · "attacked me"   b 刚打过 a（规则常量 combat_memory 之内）
  · "fighting"      a 刚打过 b，还在打
  · "faction"       一方属于见人就打的群体（特性 attacks_on_sight，如狼群、强盗），另一方不在其中
  · "criminal"      a 是守卫（Person.guard），亲眼看见 b 偷过东西 / 无故打人
  · "hatred"        a 对 b 的好感 ≤ 仇恨线（social.pl 的 hatred_threshold）
  · "war"           a 的群体记仇（特性 feud：一家人、朋友结的伙），又和 b 的群体敌对
前四种会主动动手；后两种只在自己明显更强时才动手，否则躲着走（content/base/rules/react.pl）。

一击（strike）：伤害 = 力量（power_level）+ 手上最好的武器（Item.damage）- 对方的护甲（Item.armor），至少 1。
HP 掉到 0 就死了：手上的东西掉在地上，名下的东西都没主了（谁捡归谁）。
打人之后谁怎么看、没被惹就动手算不算犯罪：内容包规则（content/base/rules/combat.pl）；
strike 只在打人的人身上记下 strike_provoked（动手前的敌对标签，'' = 没被惹）供规则用。
不打仗的时候 HP 慢慢回来：规则 tick/1（content/base/rules/time.pl）。

NPC 打还是逃：内容包规则 react/2（content/base/rules/react.pl），引擎经 rulebook.react 问。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple



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


def now(world) -> float:
    """模拟时钟（world_systems 每拍写在 WorldMeta 上）。"""
    rows = world.run_query("MATCH (m:WorldMeta {name: 'WorldMeta'}) RETURN m.sim_time AS t LIMIT 1") or []
    return float(rows[0]["t"] or 0.0) if rows and rows[0].get("t") is not None else 0.0


def hostilities(world, pairs, *, at: Optional[float] = None) -> Dict[Tuple[str, str], Dict[str, str]]:
    """{(a, b): {"label", "text"}}，只含 a 敌视 b 的那些对（一次问完）。"""
    from prolog_engine import atom
    from rulebook import ask

    pairs = [(a, b) for a, b in pairs if a and b and a != b]
    if not pairs:
        return {}
    plist = ", ".join(f"{atom(a)}-{atom(b)}" for a, b in pairs)
    rows = ask(world, "[A, B, L, T]",
               f"member(A-B, [{plist}]), hostility(A, B, W), label(W, L), explain(W, A, B, T)", at=at)
    return {(a, b): {"label": label, "text": text} for a, b, label, text in rows}


def why_hostile(world, a: str, b: str, *, at: Optional[float] = None) -> Optional[Dict[str, str]]:
    return hostilities(world, [(a, b)], at=at).get((a, b))


def hostility(world, a: str, b: str, *, at: Optional[float] = None) -> str:
    """a 为什么把 b 当敌人（规则给的标签），"" = 不是。"""
    why = why_hostile(world, a, b, at=at)
    return why["label"] if why else ""


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


def die(world, name: str, *, at: Optional[float] = None) -> List[str]:
    """死了：倒在走到的地方（路走到一半也停在那里），东西掉在身边、都没主了。返回掉下的东西。"""
    from ownership import release_all

    try:
        import spatial

        spatial.stop_walk_here(world, name, now(world) if at is None else float(at))
    except Exception:  # noqa: BLE001  没有地形的老世界
        pass
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
        out["dropped"] = die(world, target, at=at)
    return out
