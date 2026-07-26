"""
NPC 知识包：从世界图加载 Fact / Rumor / 信任，供 LLM 提示使用。
不生成任何对话念白——台词一律由 LLM 返回。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Clue:
    kind: str  # fact | rumor
    name: str
    text: str
    secret_level: int = 0
    tags: List[str] = field(default_factory=list)


@dataclass
class NpcKnowledge:
    name: str
    personality: str = ""
    background: str = ""
    mood: str = "neutral"
    role: str = "npc"
    speech_style: str = ""
    secret_policy: str = "share_gossip"
    trust_toward_player: int = 0
    facts: List[Clue] = field(default_factory=list)
    rumors: List[Clue] = field(default_factory=list)
    memory_snippets: List[str] = field(default_factory=list)


def get_trust(world, name: str) -> int:
    if world is None:
        return 0
    try:
        rows = world.run_query(
            """
            MATCH (p:Person {name: $name})
            RETURN coalesce(p.trust_toward_player, 0) AS t
            LIMIT 1
            """,
            name=name,
        )
        return int(rows[0]["t"]) if rows else 0
    except Exception:
        return 0


def set_trust(world, name: str, value: int) -> int:
    value = max(-5, min(10, int(value)))
    if world is None:
        return value
    try:
        world.run_query(
            """
            MATCH (p:Person {name: $name})
            SET p.trust_toward_player = $t
            """,
            name=name,
            t=value,
        )
    except Exception:
        pass
    return value


def adjust_trust(world, name: str, delta: int) -> int:
    cur = get_trust(world, name)
    return set_trust(world, name, cur + int(delta))


def load_npc_knowledge(
    world,
    name: str,
    *,
    memory=None,
    player_message: str = "",
) -> NpcKnowledge:
    kb = NpcKnowledge(name=name)
    if world is None:
        return kb

    rows = world.run_query(
        """
        MATCH (p:Person {name: $name})
        RETURN coalesce(p.personality, '') AS personality,
               coalesce(p.background, '') AS background,
               coalesce(p.mood, 'neutral') AS mood,
               coalesce(p.role, 'npc') AS role,
               coalesce(p.speech_style, '') AS speech_style,
               coalesce(p.secret_policy, 'share_gossip') AS secret_policy,
               coalesce(p.trust_toward_player, 0) AS trust
        LIMIT 1
        """,
        name=name,
    )
    if rows:
        r = rows[0]
        kb.personality = r.get("personality") or ""
        kb.background = r.get("background") or ""
        kb.mood = r.get("mood") or "neutral"
        kb.role = r.get("role") or "npc"
        kb.speech_style = r.get("speech_style") or ""
        kb.secret_policy = r.get("secret_policy") or "share_gossip"
        kb.trust_toward_player = int(r.get("trust") or 0)

    try:
        frows = world.run_query(
            """
            MATCH (p:Person {name: $name})-[:KNOWS_FACT]->(f:Fact)
            RETURN f.name AS name,
                   coalesce(f.text, f.name) AS text,
                   coalesce(f.secret_level, 0) AS secret_level
            """,
            name=name,
        )
        for fr in frows:
            text = fr.get("text") or ""
            fname = fr.get("name") or ""
            if not text:
                continue
            kb.facts.append(
                Clue(
                    kind="fact",
                    name=fname,
                    text=text,
                    secret_level=int(fr.get("secret_level") or 0),
                )
            )
    except Exception:
        pass

    try:
        rrows = world.run_query(
            """
            MATCH (p:Person {name: $name})-[:BELIEVES]->(r:Rumor)
            RETURN r.name AS name,
                   coalesce(r.text, r.name) AS text
            """,
            name=name,
        )
        for rr in rrows:
            text = rr.get("text") or ""
            rname = rr.get("name") or ""
            if not text:
                continue
            kb.rumors.append(
                Clue(
                    kind="rumor",
                    name=rname,
                    text=text,
                    secret_level=0,
                )
            )
    except Exception:
        pass

    if memory is not None:
        kb.memory_snippets = _search_memories(memory, name, player_message)

    return kb


def _search_memories(memory, name: str, player_message: str, top_k: int = 5) -> List[str]:
    snippets: List[str] = []
    query = (player_message or "").strip() or "recent conversation"
    try:
        hits = memory.search(name, query, top_k=top_k)
        for h in hits:
            t = (h.get("text") or "").strip()
            if t:
                snippets.append(t)
    except Exception:
        try:
            items = memory.list(name) or []
            for item in items[-top_k:]:
                t = (item.get("text") or "").strip()
                if t:
                    snippets.append(t)
        except Exception:
            pass
    return snippets


def knowledge_prompt_block(knowledge: NpcKnowledge) -> str:
    """把图数据序列化进 LLM 提示；不含任何预设台词。"""
    parts = [
        f"secret_policy: {knowledge.secret_policy or 'share_gossip'}",
        f"trust_toward_player: {knowledge.trust_toward_player}",
        f"role: {knowledge.role or 'npc'}",
    ]
    if knowledge.rumors:
        parts.append("Known rumors (you may share according to personality/policy):")
        for c in knowledge.rumors:
            parts.append(f"  - [{c.name}] {c.text}")
    else:
        parts.append("Known rumors: (none)")
    if knowledge.facts:
        parts.append(
            "Known facts (respect secret_level vs trust/policy; high secret_level = harder to reveal):"
        )
        for c in knowledge.facts:
            parts.append(f"  - [{c.name}] (secret_level={c.secret_level}) {c.text}")
    else:
        parts.append("Known facts: (none)")
    return "\n".join(parts)


def can_reveal_clue(
    clue: Clue,
    *,
    trust: int,
    secret_policy: str = "share_gossip",
) -> bool:
    """根据信任与 secret_policy 判断 NPC 是否会透露该线索（仅作 prompt 提示，不作学事实门槛）。"""
    level = int(clue.secret_level or 0)
    policy = (secret_policy or "share_gossip").strip().lower()
    if level <= 0:
        return True
    if policy in ("open", "share_all", "helpful"):
        return trust >= max(0, level - 1)
    if policy in ("share_gossip", "gossip"):
        # secret_level=1 在轻度信任下可分享；更高需更高信任
        return trust >= level
    if policy in ("guarded", "secretive"):
        return trust >= level + 2
    return trust >= level


def shareable_facts_for_player(knowledge: NpcKnowledge) -> List[Clue]:
    return [
        c
        for c in (knowledge.facts or [])
        if can_reveal_clue(
            c,
            trust=int(knowledge.trust_toward_player or 0),
            secret_policy=knowledge.secret_policy,
        )
    ]


def person_knows_fact(world, person: str, fact_name: str) -> bool:
    if world is None or not person or not fact_name:
        return False
    try:
        rows = world.run_query(
            """
            MATCH (p:Person {name: $p})-[:KNOWS_FACT]->(f:Fact {name: $f})
            RETURN f.name AS n LIMIT 1
            """,
            p=person,
            f=fact_name,
        )
        return bool(rows)
    except Exception:
        return False


_STOPWORDS = frozenset(
    """
    a an the and or but if to of in on at for from with by as is are was were be been
    this that these those it its you your he she they we my our his her their
    can could would should will may might about into over under then than so not no
    """.split()
)


def _content_tokens(text: str) -> set:
    """英文词 + 连续中日韩字串，过滤停用词。"""
    import re as _re

    s = (text or "").lower()
    out: set = set()
    for m in _re.finditer(r"[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}", s):
        tok = m.group(0)
        if tok in _STOPWORDS:
            continue
        out.add(tok)
    return out


def fact_reflected_in_dialogue(clue: Clue, spoken_text: str) -> bool:
    """
    判断 NPC 台词是否真正传达了该 Fact（非信任批量赠送）。
    规则：事实原文片段出现在台词中，或足够多的内容词重叠。
    """
    spoken = (spoken_text or "").strip()
    if not spoken:
        return False
    spoken_l = spoken.lower()
    text = (clue.text or "").strip()
    name = (clue.name or "").strip()

    # CamelCase 名几乎不会出现在口语里，跳过；短名误匹配风险高
    if name and len(name) >= 8 and name.lower() in spoken_l:
        return True

    if not text:
        return False

    # 较长连续子串（事实里的关键句）
    for part in _re_split_clauses(text):
        p = part.strip()
        if len(p) >= 12 and p.lower() in spoken_l:
            return True

    ft = _content_tokens(text)
    st = _content_tokens(spoken)
    if len(ft) < 2:
        return False
    hit = ft & st
    # 至少一半内容词，且至少 2 个命中，避免一句闲聊误学整条
    return len(hit) >= 2 and len(hit) / len(ft) >= 0.4


def _re_split_clauses(text: str) -> List[str]:
    import re as _re

    parts = _re.split(r"[.!?;；。！？\n]+", text or "")
    # 再按逗号切较长句，便于匹配局部信息
    out: List[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        out.append(p)
        if len(p) > 40:
            out.extend(x.strip() for x in _re.split(r"[,，]", p) if len(x.strip()) >= 12)
    return out


def transfer_facts_from_dialogue(
    world,
    npc_name: str,
    listener: str,
    lines: List[Dict[str, str]],
    *,
    knowledge: Optional[NpcKnowledge] = None,
) -> List[str]:
    """
    对话后：仅当 NPC 台词实际传达了某条 Fact 时，才链到听者。
    不再按信任批量赠送。
    """
    if world is None or not npc_name or not listener or npc_name == listener:
        return []
    spoken_parts: List[str] = []
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        if (ln.get("speaker") or "").strip() != npc_name:
            continue
        t = (ln.get("text") or "").strip()
        if t:
            spoken_parts.append(t)
    spoken = " ".join(spoken_parts)
    if not spoken:
        return []

    kb = knowledge or load_npc_knowledge(world, npc_name)
    gained: List[str] = []
    for clue in kb.facts or []:
        fname = (clue.name or "").strip()
        if not fname:
            continue
        if person_knows_fact(world, listener, fname):
            continue
        if not fact_reflected_in_dialogue(clue, spoken):
            continue
        try:
            world.link_entities(listener, "Person", fname, "Fact", "KNOWS_FACT")
            gained.append(fname)
        except Exception:
            continue
    return gained


def transfer_shareable_facts(
    world,
    npc_name: str,
    listener: str,
    *,
    knowledge: Optional[NpcKnowledge] = None,
    lines: Optional[List[Dict[str, str]]] = None,
) -> List[str]:
    """
    兼容旧名：若提供 lines，按真实对话转移；否则不转移（已废弃信任批量赠送）。
    """
    if lines is not None:
        return transfer_facts_from_dialogue(
            world, npc_name, listener, lines, knowledge=knowledge
        )
    return []


def npc_has_unshared_facts(world, npc_name: str, listener: str) -> bool:
    """听者是否还能从该 NPC 立刻学到可分享事实（仅用于调试/UI，不作目标满足判定）。"""
    if world is None or not npc_name or not listener:
        return False
    kb = load_npc_knowledge(world, npc_name)
    for clue in shareable_facts_for_player(kb):
        if clue.name and not person_knows_fact(world, listener, clue.name):
            return True
    return False

