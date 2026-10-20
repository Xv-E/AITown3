"""
内心与人情 — 让对话有感情、让求助有来由。

人物在图上可带（均可选，世界 JSON / 导演生成）：
  desire  深层渴望（不同于当天的 goal）
  fear    害怕失去 / 面对的东西
  secret  不愿说出口的事；只在足够亲近时才会吐露
  bonds   JSON {对方名字: "感受 + 共同经历"}

对话里 NPC 若真心求人帮忙，可返回 favor {item, why, reward}，
这里把它落成一个 Quest(kind=favor)：描述写「为什么」，而不只是「缺什么」。
求助对象把东西交到求助者手里时，任务完成。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 亲近到可以吐露秘密：互为朋友，或对玩家信任达到这个值
CONFIDE_TRUST = 6
MAX_OPEN_FAVORS_PER_GIVER = 1
_FAVOR_TEXT_MAX = 240


@dataclass
class InnerLife:
    name: str
    desire: str = ""
    fear: str = ""
    secret: str = ""
    bonds: Dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.desire or self.fear or self.secret or self.bonds)


def _parse_bonds(raw: Any) -> Dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if k and v}
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if k and v}
        except json.JSONDecodeError:
            pass
    return {}


def load_inner_life(world, name: str) -> InnerLife:
    inner = InnerLife(name=name)
    if world is None or not name:
        return inner
    try:
        rows = world.run_query(
            """
            MATCH (p:Person {name: $n})
            RETURN coalesce(p.desire, '') AS desire,
                   coalesce(p.fear, '') AS fear,
                   coalesce(p.secret, '') AS secret,
                   p.bonds AS bonds
            LIMIT 1
            """,
            n=name,
        ) or []
    except Exception:
        rows = []
    if rows:
        r = rows[0]
        inner.desire = str(r.get("desire") or "")
        inner.fear = str(r.get("fear") or "")
        inner.secret = str(r.get("secret") or "")
        inner.bonds = _parse_bonds(r.get("bonds"))
    return inner


def graph_relations(world, a: str, b: str) -> List[str]:
    """a 与 b 之间的社交关系类型（两个方向，去重）。"""
    if world is None or not a or not b:
        return []
    try:
        rows = world.run_query(
            """
            MATCH (x:Person {name: $a})-[r]-(y:Person {name: $b})
            WHERE type(r) IN ['KNOWS', 'FRIENDS_WITH', 'LIKES', 'DISLIKES', 'RIVALS_WITH', 'FAMILY_OF']
            RETURN DISTINCT type(r) AS rel
            """,
            a=a,
            b=b,
        ) or []
    except Exception:
        return []
    return sorted({str(r["rel"]) for r in rows if r.get("rel")})


def is_close(rels: List[str], trust: Optional[int] = None) -> bool:
    if any(r in ("FRIENDS_WITH", "FAMILY_OF") for r in rels):
        return True
    return trust is not None and trust >= CONFIDE_TRUST


def relationship_line(inner: InnerLife, other: str, rels: List[str]) -> str:
    """一句话描述「我」与对方的关系。"""
    bits: List[str] = []
    if rels:
        bits.append(", ".join(r.lower().replace("_", " ") for r in rels))
    bond = inner.bonds.get(other, "")
    if bond:
        bits.append(bond)
    if not bits:
        return f"You and {other} have no history yet; first impressions matter."
    return f"You and {other}: " + "; ".join(bits)


def others_in_heart(inner: InnerLife, exclude: str = "", limit: int = 2) -> str:
    """心里还挂着的其他人（可在对话里自然提起）。"""
    rest = [(k, v) for k, v in inner.bonds.items() if k != exclude][:limit]
    return "; ".join(f"{k} — {v}" for k, v in rest)


def inner_life_block(
    inner: InnerLife,
    *,
    listener: str,
    rels: List[str],
    trust: Optional[int] = None,
) -> str:
    """对话 prompt 用：渴望 / 恐惧 / 秘密（按亲近度给出处理方式）/ 关系。"""
    lines: List[str] = []
    if inner.desire:
        lines.append(f"What you long for: {inner.desire}")
    if inner.fear:
        lines.append(f"What you fear: {inner.fear}")
    if inner.secret:
        if is_close(rels, trust):
            lines.append(
                f"Your secret: {inner.secret} (you are close to {listener}; "
                "you may confide it if the moment feels right)"
            )
        else:
            lines.append(
                f"Your secret: {inner.secret} (keep it hidden from {listener}; "
                "it only shows as hesitation, deflection, or a change of subject)"
            )
    lines.append(relationship_line(inner, listener, rels))
    others = others_in_heart(inner, exclude=listener)
    if others:
        lines.append(f"Others on your mind: {others}")
    return "\n".join(lines)


DIALOGUE_CRAFT = """How to speak:
- You are a person with a past, not a vending machine. Let feelings (worry, pride, guilt, affection, resentment, hope) show through subtext and small details; do not just name the emotion.
- Draw on your history with the listener and with others in town when it fits; small personal stories make the world real.
- If you ask for help, the request must grow out of your story: say WHY it matters to you and who else it affects (a promise, a debt, a loved one, a fear). Never a bare "I need X, bring me X".
- React to what was actually said. Do not repeat your job description."""


def favor_prompt_block(item_options: List[str]) -> str:
    opts = ", ".join(item_options[:16]) if item_options else "(none)"
    return (
        "Optional favor: only if you genuinely ask the listener for something in your lines, add\n"
        '"favor":{"item":"<one of: ' + opts + '>","why":"the personal reason, one sentence",'
        '"reward":"what you offer or promise"}. Otherwise omit "favor".'
    )


def favor_item_options(world, limit: int = 16) -> List[str]:
    """可被求取的东西：可携带物品的种类（archetype / item_kind）或具名物品。"""
    if world is None:
        return []
    try:
        rows = world.run_query(
            """
            MATCH (i:Item)
            WHERE coalesce(i.portable, true) = true
            RETURN DISTINCT coalesce(nullif(i.archetype, ''), nullif(i.item_kind, ''), i.name) AS k
            ORDER BY k
            """
        ) or []
    except Exception:
        return []
    return [str(r["k"]) for r in rows if r.get("k")][:limit]


def _clip(text: Any, n: int = _FAVOR_TEXT_MAX) -> str:
    t = str(text or "").strip()
    return t[:n]


def parse_favor(raw: Any) -> Optional[Dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    item = _clip(raw.get("item"), 48)
    why = _clip(raw.get("why") or raw.get("reason"))
    if not item or not why or item.startswith("<"):
        return None
    return {"item": item, "why": why, "reward": _clip(raw.get("reward"))}


def _matches_item_sql(alias: str = "i") -> str:
    return (
        f"({alias}.name = $item OR toLower(coalesce({alias}.archetype, '')) = toLower($item) "
        f"OR toLower(coalesce({alias}.item_kind, '')) = toLower($item))"
    )


def _item_exists(world, item: str) -> bool:
    try:
        rows = world.run_query(
            f"MATCH (i:Item) WHERE {_matches_item_sql()} RETURN i.name AS n LIMIT 1",
            item=item,
        ) or []
        return bool(rows)
    except Exception:
        return False


def _holds_item(world, person: str, item: str) -> bool:
    try:
        rows = world.run_query(
            f"""
            MATCH (p:Person {{name: $p}})-[:HAS_ITEM]->(i:Item)
            WHERE {_matches_item_sql()}
            RETURN i.name AS n LIMIT 1
            """,
            p=person,
            item=item,
        ) or []
        return bool(rows)
    except Exception:
        return False


def favor_quest_name(giver: str, item: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z]+", "", item.title()) or "Help"
    return f"Favor{giver}{safe}"


def register_favor(
    world,
    giver: str,
    helper: str,
    favor: Dict[str, str],
    *,
    location: str = "",
) -> Optional[str]:
    """把对话里的求助落成 Quest。返回任务名；无效 / 重复时返回 None。"""
    if world is None or not giver or not helper or giver == helper:
        return None
    item = favor.get("item") or ""
    if not item or not _item_exists(world, item) or _holds_item(world, giver, item):
        return None
    try:
        open_rows = world.run_query(
            """
            MATCH (g:Person {name: $g})-[:GIVES_QUEST]->(q:Quest {kind: 'favor'})
            WHERE toLower(coalesce(q.state, 'active')) = 'active'
            RETURN q.name AS n
            """,
            g=giver,
        ) or []
    except Exception:
        open_rows = []
    if len(open_rows) >= MAX_OPEN_FAVORS_PER_GIVER:
        return None

    name = favor_quest_name(giver, item)
    reward = favor.get("reward") or ""
    objective = f"Bring {item} to {giver}" + (f" — {giver} offers: {reward}" if reward else "")
    world.add_entity(
        name,
        entity_type="Quest",
        kind="favor",
        description=favor.get("why") or "",
        objective=objective,
        stage="asked",
        state="active",
        item=item,
        helper=helper,
        reward=reward,
        location=location,
        asked_at=time.time(),
    )
    world.link_entities(giver, "Person", name, "Quest", "GIVES_QUEST")
    world.link_entities(helper, "Person", name, "Quest", "ACCEPTED_QUEST")
    return name


def open_favors(world, person: str) -> List[Dict[str, Any]]:
    """person 作为求助者或被求助者的未完成人情。"""
    if world is None or not person:
        return []
    try:
        rows = world.run_query(
            """
            MATCH (g:Person)-[:GIVES_QUEST]->(q:Quest {kind: 'favor'})
            WHERE toLower(coalesce(q.state, 'active')) = 'active'
              AND (g.name = $p OR q.helper = $p)
            RETURN q.name AS name, g.name AS giver, q.helper AS helper,
                   q.item AS item, coalesce(q.description, '') AS why,
                   coalesce(q.reward, '') AS reward
            ORDER BY name
            """,
            p=person,
        ) or []
    except Exception:
        return []
    return [dict(r) for r in rows]


def favors_text(world, person: str) -> str:
    lines: List[str] = []
    for f in open_favors(world, person):
        if f.get("giver") == person:
            lines.append(f"- You asked {f.get('helper')} for {f.get('item')}: {f.get('why')}")
        else:
            reward = f" (they offered: {f['reward']})" if f.get("reward") else ""
            lines.append(
                f"- {f.get('giver')} asked you for {f.get('item')}: {f.get('why')}{reward}"
            )
    return "\n".join(lines)


def sync_favor_quests(world) -> List[Dict[str, Any]]:
    """求助者手里已有所求之物 → 任务完成。返回刚完成的任务。"""
    done: List[Dict[str, Any]] = []
    for f in _all_open_favors(world):
        giver, item = f.get("giver") or "", f.get("item") or ""
        if not giver or not item or not _holds_item(world, giver, item):
            continue
        try:
            world.run_query(
                "MATCH (q:Quest {name: $n}) SET q.state = 'completed', q.stage = 'done'",
                n=f["name"],
            )
        except Exception:
            continue
        done.append(f)
    return done


def _all_open_favors(world) -> List[Dict[str, Any]]:
    if world is None:
        return []
    try:
        rows = world.run_query(
            """
            MATCH (g:Person)-[:GIVES_QUEST]->(q:Quest {kind: 'favor'})
            WHERE toLower(coalesce(q.state, 'active')) = 'active'
            RETURN q.name AS name, g.name AS giver, q.helper AS helper, q.item AS item
            """
        ) or []
    except Exception:
        return []
    return [dict(r) for r in rows]


_MOOD_RE = re.compile(r"^[A-Za-z][A-Za-z \-]{1,23}$")


def apply_mood(world, name: str, mood: Any) -> Optional[str]:
    """对话后情绪变化写回 Person.mood（短词才接受）。"""
    m = str(mood or "").strip().lower()
    if world is None or not name or not m or not _MOOD_RE.match(m):
        return None
    try:
        world.run_query("MATCH (p:Person {name: $n}) SET p.mood = $m", n=name, m=m)
    except Exception:
        return None
    return m


def settle_favors(world, memory=None) -> List[str]:
    """完成已兑现的人情：记入双方记忆；帮忙的是玩家则信任 +1。返回备注。"""
    notes: List[str] = []
    for f in sync_favor_quests(world):
        giver, helper, item = f.get("giver"), f.get("helper"), f.get("item")
        notes.append(f"{f['name']}: {giver} got {item}")
        if helper:
            try:
                rows = world.run_query(
                    """
                    MATCH (p:Person {name: $n})
                    RETURN coalesce(p.is_player, false) AS ip, coalesce(p.role, '') AS role
                    """,
                    n=helper,
                ) or []
                if rows and (rows[0].get("ip") or rows[0].get("role") == "player"):
                    from npc_knowledge import adjust_trust

                    adjust_trust(world, giver, 1)
            except Exception:
                pass
        if memory is not None and giver:
            try:
                memory.add(
                    giver,
                    f"The favor you asked for came through: you now have {item}"
                    + (f" — {helper} did not forget you." if helper else "."),
                    type="favor",
                    actor=helper or "",
                    target=giver,
                )
                if helper:
                    memory.add(
                        helper,
                        f"{giver} finally has the {item} they asked you for.",
                        type="favor",
                        actor=helper,
                        target=giver,
                    )
            except Exception:
                pass
    return notes
