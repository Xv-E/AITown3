"""
Talk 对话生成 — 台词一律由 LLM 返回；世界知识仅作提示上下文。

流程（自动 talk）:
  1) call：发起方 (speaker_a) 根据自身目标/已知事实生成一句发言
  2) call：被谈话方 (speaker_b) 根据知识包回复
若 UI 已传入 player_message，则跳过第 1 次 call，只生成 NPC 回复。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable, Any, Dict, List, Optional

from agent_profile import AgentProfile
from factions import faction_block, pair_view, ripple
from inner_life import (
    DIALOGUE_CRAFT,
    apply_mood,
    favor_item_options,
    favor_prompt_block,
    graph_relations,
    inner_life_block,
    load_inner_life,
    register_favor,
)
from npc_knowledge import (
    adjust_trust,
    knowledge_prompt_block,
    load_npc_knowledge,
)

logger = logging.getLogger(__name__)


def _strip_reasoning(text: str) -> str:
    """去掉 reasoning 模型常见的思考块，保留最终内容。"""
    if not text:
        return ""
    t = text
    for pat in (
        r"<think>[\s\S]*?</think>",
        r"<thinking>[\s\S]*?</thinking>",
        r"<reasoning>[\s\S]*?</reasoning>",
    ):
        t = re.sub(pat, "", t, flags=re.I)
    return t.strip()


def _extract_json_objects(text: str) -> List[str]:
    """按括号平衡提取所有顶层 JSON 对象字符串。"""
    objs: List[str] = []
    start = -1
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    objs.append(text[start : i + 1])
                    start = -1
    return objs


def _repair_truncated_json(text: str) -> Optional[dict]:
    """尽力修补被 max_tokens 截断的对话 JSON。"""
    if not text or '"lines"' not in text:
        return None
    i = text.find("{")
    if i < 0:
        return None
    frag = text[i:].strip()

    try:
        data = json.loads(frag)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    m = re.search(r'"lines"\s*:\s*(\[[\s\S]*)', frag)
    if not m:
        return None
    arr_part = m.group(1)
    lines: List[dict] = []
    for obj_m in re.finditer(
        r'\{\s*"speaker"\s*:\s*"([^"]*)"\s*,\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"\s*\}',
        arr_part,
    ):
        lines.append({"speaker": obj_m.group(1), "text": obj_m.group(2)})
    if not lines:
        return None

    trust_delta = 0
    tm = re.search(r'"trust_delta"\s*:\s*(-?\d+)', frag)
    if tm:
        try:
            trust_delta = int(tm.group(1))
        except ValueError:
            trust_delta = 0

    return {"lines": lines, "trust_delta": trust_delta}


def _unescape_text(line_text: str) -> str:
    return (
        line_text.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def _parse_dialogue_payload(
    text: str,
    speaker_a: str,
    speaker_b: str,
) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = _strip_reasoning(text)

    candidates: List[str] = []
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned)
    if fence:
        candidates.append(fence.group(1))
    candidates.extend(reversed(_extract_json_objects(cleaned)))
    if cleaned.strip().startswith("{"):
        candidates.append(cleaned.strip())

    data = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict) and "lines" in obj:
                data = obj
                break
        except json.JSONDecodeError:
            continue

    if data is None:
        data = _repair_truncated_json(cleaned)

    if not isinstance(data, dict):
        return None

    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list):
        return None
    lines: List[Dict[str, str]] = []
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker", "")).strip()
        line_text = str(item.get("text", "")).strip()
        if not line_text:
            continue
        line_text = _unescape_text(line_text)
        # 模型只应输出 NPC；若误带玩家行则丢掉，未知 speaker 归为 NPC
        if speaker == speaker_a:
            continue
        if speaker != speaker_b:
            speaker = speaker_b
        lines.append({"speaker": speaker, "text": line_text})
    if not lines:
        return None

    try:
        trust_delta = int(data.get("trust_delta", 0))
    except (TypeError, ValueError):
        trust_delta = 0
    trust_delta = max(-1, min(2, trust_delta))
    out: Dict[str, Any] = {"lines": lines, "trust_delta": trust_delta}
    out.update(_parse_heart_extras(data))
    return out


def _parse_heart_extras(data: Dict[str, Any]) -> Dict[str, Any]:
    """对话 JSON 里可选的情绪变化与求助。"""
    from inner_life import parse_favor

    extras: Dict[str, Any] = {}
    mood = str(data.get("mood") or "").strip()
    if mood:
        extras["mood"] = mood[:24]
    favor = parse_favor(data.get("favor"))
    if favor:
        extras["favor"] = favor
    return extras


def _invoke_dialogue_primary(prompt: str) -> tuple[str, Dict[str, Any]]:
    """Qwen 主路径（无 GBNF）。返回 (raw_text, provider_info)。"""
    from llm_router import invoke_primary

    # 1–2 句台词 + 可选 mood/favor，token 仍宜少，避免拖到超时后 raw=null
    max_tokens = int(os.getenv("DIALOGUE_MAX_TOKENS", "400"))
    timeout = float(os.getenv("DIALOGUE_LLM_TIMEOUT", "30"))
    info: Dict[str, Any] = {
        "used": False,
        "error": "",
        "grammar_preview": "",
        "provider": "qwen",
        "fallback_used": False,
    }
    raw = invoke_primary(prompt, max_tokens=max_tokens, timeout=timeout)
    return (raw if isinstance(raw, str) else str(raw)), info


def _repair_dialogue_local_gbnf(
    draft: str,
    *,
    speaker_a: str,
    speaker_b: str,
    player_message: str,
) -> tuple[str, Dict[str, Any]]:
    """本地 GBNF：把 Qwen 草稿压成合法对话 JSON。"""
    from gbnf import generate_dialogue_grammar, dialogue_limits_prompt_block
    from llm_router import build_repair_prompt, invoke_local_gbnf

    grammar_str = generate_dialogue_grammar(speaker_a, speaker_b, player_message)
    info: Dict[str, Any] = {
        "used": True,
        "error": "",
        "grammar_preview": grammar_str[:1200],
        "provider": "local_gbnf",
        "fallback_used": True,
    }
    schema = (
        f'{{"lines":[{{"speaker":"{speaker_b}","text":"..."}}],'
        f'"trust_delta":-1|0|1|2}}\n'
        f"{dialogue_limits_prompt_block()}\n"
        f"Do not include {speaker_a}'s utterance in lines."
    )
    repair_prompt = build_repair_prompt(
        draft=draft or "(empty)",
        schema_hint=schema,
        repair_context=f"player={speaker_a}; npc={speaker_b}",
    )
    max_tokens = int(os.getenv("DIALOGUE_MAX_TOKENS", "768"))
    try:
        raw = invoke_local_gbnf(repair_prompt, grammar_str, max_tokens=max_tokens)
        return (raw if isinstance(raw, str) else str(raw)), info
    except Exception as e:
        info["error"] = str(e)
        logger.warning("dialogue local GBNF repair failed: %s", e)
        raise


def _record_dialogue_trace(**kwargs) -> str:
    try:
        import sys
        from pathlib import Path

        web_path = str(Path(__file__).resolve().parent.parent / "web")
        if web_path not in sys.path:
            sys.path.insert(0, web_path)
        from llm_trace import record_llm_trace

        return record_llm_trace(kind="dialogue", **kwargs)
    except Exception as e:
        logger.debug("record_llm_trace failed: %s", e)
        return ""


_PLACEHOLDER_RE = re.compile(
    r"^(<"
    r"[^>]{0,40}"
    r">"
    r"|NPC[_\s]?TEXT"
    r"|\.\.\."
    r"|your reply here"
    r"|insert (reply|text) here"
    r")$",
    re.I,
)


def _is_placeholder_npc_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if _PLACEHOLDER_RE.match(t):
        return True
    low = t.lower()
    if "npc reply" in low or "npc_text" in low:
        return True
    # 整句被尖括号包住
    if t.startswith("<") and t.endswith(">") and len(t) <= 48:
        return True
    return False


def _speaker_facts_block(knowledge) -> str:
    """发起方已知信息（不含对方秘密）。"""
    if knowledge is None:
        return "(no known facts yet)"
    parts: List[str] = []
    for c in list(getattr(knowledge, "facts", None) or [])[:8]:
        parts.append(f"- [{c.name}] {c.text}")
    for c in list(getattr(knowledge, "rumors", None) or [])[:4]:
        parts.append(f"- rumor [{c.name}] {c.text}")
    if not parts:
        return "(no known facts yet — ask questions)"
    blob = "\n".join(parts)
    if len(blob) > 500:
        blob = blob[:500] + "…"
    return blob


def _parse_utterance_payload(text: str) -> Optional[str]:
    """解析发起方发言 JSON: {\"text\":\"...\"}。"""
    if not text:
        return None
    cleaned = _strip_reasoning(text)
    candidates: List[str] = []
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned)
    if fence:
        candidates.append(fence.group(1))
    candidates.extend(reversed(_extract_json_objects(cleaned)))
    if cleaned.strip().startswith("{"):
        candidates.append(cleaned.strip())

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        line = str(obj.get("text") or obj.get("utterance") or "").strip()
        if line:
            return _unescape_text(line)

    # 宽松：整段非 JSON 短句也可
    plain = cleaned.strip().strip('"').strip("'")
    if plain and len(plain) <= 220 and "{" not in plain and not _is_placeholder_npc_text(plain):
        return plain
    return None


def _parse_utterance_extras(text: str) -> Dict[str, Any]:
    cleaned = _strip_reasoning(text or "")
    for cand in reversed(_extract_json_objects(cleaned)):
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and (obj.get("text") or obj.get("utterance")):
            return _parse_heart_extras(obj)
    return {}


def _is_player(world, name: str) -> bool:
    if world is None or not name:
        return False
    try:
        rows = world.run_query(
            """
            MATCH (p:Person {name: $n})
            RETURN coalesce(p.is_player, false) AS ip, coalesce(p.role, '') AS role
            LIMIT 1
            """,
            n=name,
        ) or []
    except Exception:
        return False
    return bool(rows) and (bool(rows[0].get("ip")) or rows[0].get("role") == "player")


def _heart_blocks(world, me: str, other: str, knowledge=None) -> tuple[str, str]:
    """(内心 + 关系 + 势力块, 求助格式块)。玩家自己不生成求助任务。"""
    if world is None:
        return "", ""
    view = None
    try:
        view = pair_view(world, me, other)
    except Exception as e:
        logger.debug("faction view failed: %s", e)
    try:
        inner = load_inner_life(world, me)
        rels = graph_relations(world, me, other)
        # 亲近度 = 个人信任（仅对玩家）+ 势力倾向
        trust = view.score if view is not None and view.reasons else None
        if knowledge is not None and _is_player(world, other):
            trust = int(getattr(knowledge, "trust_toward_player", 0) or 0) + (trust or 0)
        heart = inner_life_block(inner, listener=other, rels=rels, trust=trust)
    except Exception as e:
        logger.debug("inner life block failed: %s", e)
        heart = ""
    try:
        fblock = faction_block(world, me, other, view=view) if view is not None else ""
    except Exception as e:
        logger.debug("faction block failed: %s", e)
        fblock = ""
    if fblock:
        heart = f"{heart}\n{fblock}" if heart else fblock
    try:
        from street_economy import economy_text

        money = economy_text(world, me)
    except Exception:
        money = ""
    if money:
        heart = f"{heart}\n{money}" if heart else money
    favor = ""
    try:
        if not _is_player(world, me):
            options = favor_item_options(world)
            if options:
                favor = favor_prompt_block(options)
    except Exception:
        favor = ""
    return heart, favor


def _shared_history(memory, me: str, other: str, top_k: int = 2) -> List[str]:
    """我和对方之间的过往（记忆检索，只保留提到对方的）。"""
    if memory is None or not other:
        return []
    try:
        hits = memory.search(me, f"{other} talked together", top_k=top_k + 3) or []
    except Exception:
        return []
    out: List[str] = []
    for h in hits:
        t = str(h.get("text") or "").strip()
        if t and other in t and t not in out:
            out.append(t[:160])
        if len(out) >= top_k:
            break
    return out


def _build_speaker_utterance_prompt(
    *,
    speaker_a: str,
    speaker_b: str,
    location: str,
    agent_a,
    partner_brief: str,
    facts_block: str,
    mem_hints: List[str],
    max_chars: int,
    retry_note: str = "",
    situation: str = "",
    heart_block: str = "",
    favor_block: str = "",
) -> str:
    retry = f"{retry_note}\n" if retry_note else ""
    hints = "; ".join(mem_hints[:3]) if mem_hints else "none"
    goal = (getattr(agent_a, "goal", "") or "").strip() or "(none set)"
    heart = f"{heart_block}\n" if heart_block else ""
    favor = f"{favor_block}\n" if favor_block else ""
    fmt = '{"text":"...","mood":"one word"' + (',"favor":{...}' if favor_block else "") + "}"
    return f"""{retry}You are {speaker_a} at {location}. You will speak ONE line to {speaker_b}.
Profile: {agent_a.personality}; {agent_a.background}; mood={agent_a.mood}
Current goal: {goal}
Your situation: {situation or '(nothing pressing)'}
{heart}About {speaker_b}: {partner_brief}
What you know (do not invent beyond this):
{facts_block}
Memory hints: {hints}
{DIALOGUE_CRAFT}
Write one in-character line: greet, confide, tease, apologise, share news, or ask for help. If your goal needs something, say what it means to you, not just what is missing. Speak in English only. Max {max_chars} chars.
{favor}Return ONLY JSON: {fmt}
JSON:"""


def _llm_speaker_utterance(
    world,
    speaker_a: str,
    speaker_b: str,
    location: str,
    *,
    memory=None,
) -> tuple[str, Dict[str, Any]]:
    """
    第一次 LLM call：生成发起方发言。
    返回 (utterance, meta)；失败时 utterance 为空字符串。
    """
    import time as _time

    from gbnf import DIALOGUE_FIELD_LIMITS

    max_chars = int(DIALOGUE_FIELD_LIMITS.get("player_text", 200))
    meta: Dict[str, Any] = {
        "phase": "utterance",
        "speaker_a": speaker_a,
        "speaker_b": speaker_b,
        "location": location,
        "provider": "qwen",
    }

    agent_a = (
        AgentProfile.from_world(world, speaker_a)
        if world
        else AgentProfile(world, speaker_a)
    )
    if memory:
        agent_a.set_memory(memory)

    # 对方只给公开人设，不塞对方秘密知识
    partner_brief = "someone nearby"
    try:
        agent_b = (
            AgentProfile.from_world(world, speaker_b)
            if world
            else AgentProfile(world, speaker_b)
        )
        partner_brief = f"{agent_b.personality}; {agent_b.background}"
        if len(partner_brief) > 180:
            partner_brief = partner_brief[:180] + "…"
    except Exception:
        pass

    knowledge_a = load_npc_knowledge(
        world,
        speaker_a,
        memory=memory,
        player_message=f"talk to {speaker_b}",
    )
    facts_block = _speaker_facts_block(knowledge_a)
    mem_hints = [m[:160] for m in (knowledge_a.memory_snippets or [])[:3] if m]
    mem_hints = _shared_history(memory, speaker_a, speaker_b) + mem_hints
    heart_block, favor_block = _heart_blocks(world, speaker_a, speaker_b, knowledge_a)

    situation_a = situation_brief(world, speaker_a)
    prompt = _build_speaker_utterance_prompt(
        speaker_a=speaker_a,
        speaker_b=speaker_b,
        location=location,
        agent_a=agent_a,
        partner_brief=partner_brief,
        facts_block=facts_block,
        mem_hints=mem_hints,
        max_chars=max_chars,
        situation=situation_a,
        heart_block=heart_block,
        favor_block=favor_block,
    )

    t0 = _time.perf_counter()
    raw = ""
    try:
        raw, ginfo = _invoke_dialogue_primary(prompt)
        meta["provider"] = ginfo.get("provider", "qwen")
    except Exception as e:
        logger.warning("speaker utterance LLM failed: %s", e)
        _record_dialogue_trace(
            prompt=prompt,
            raw="",
            ok=False,
            error=f"utterance invoke: {e}",
            meta=meta,
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )
        return "", meta

    text = _parse_utterance_payload(raw)
    if text and _is_placeholder_npc_text(text):
        text = None

    if not text:
        # 一次短重试
        retry_prompt = _build_speaker_utterance_prompt(
            speaker_a=speaker_a,
            speaker_b=speaker_b,
            location=location,
            agent_a=agent_a,
            partner_brief=partner_brief,
            facts_block=facts_block,
            mem_hints=mem_hints,
            situation=situation_a,
            max_chars=max_chars,
            retry_note=(
                "IMPORTANT: Previous answer was invalid. "
                f"Output real speech from {speaker_a} to {speaker_b} as JSON."
            ),
            heart_block=heart_block,
            favor_block=favor_block,
        )
        try:
            raw2, _ = _invoke_dialogue_primary(retry_prompt)
            text = _parse_utterance_payload(raw2)
            if text and _is_placeholder_npc_text(text):
                text = None
            raw = raw2 or raw
            prompt = retry_prompt
        except Exception as e:
            logger.warning("speaker utterance retry failed: %s", e)

    if text:
        if len(text) > max_chars:
            text = text[:max_chars]
        extras = _parse_utterance_extras(raw)
        if not favor_block:
            extras.pop("favor", None)
        meta.update(extras)
        tid = _record_dialogue_trace(
            prompt=prompt,
            raw=raw,
            ok=True,
            error="",
            meta=meta,
            parsed={"text": text, **extras},
            elapsed_ms=(_time.perf_counter() - t0) * 1000,
        )
        meta["trace_id"] = tid
        return text, meta

    _record_dialogue_trace(
        prompt=prompt,
        raw=raw,
        ok=False,
        error="unparseable utterance",
        meta=meta,
        elapsed_ms=(_time.perf_counter() - t0) * 1000,
    )
    return "", meta


HISTORY_TURNS = 8


def _is_player_name(world, name: str) -> bool:
    if world is None or not name:
        return False
    try:
        rows = world.run_query(
            "MATCH (p:Person {name: $n}) RETURN coalesce(p.is_player, false) AS pl, coalesce(p.role, '') AS role",
            n=name,
        ) or []
    except Exception:  # noqa: BLE001
        return False
    return bool(rows) and (bool(rows[0].get("pl")) or rows[0].get("role") == "player")


def history_block(history: Optional[List[Dict[str, str]]]) -> str:
    """这段对话里之前说过的话（玩家在对话框里连着聊时，NPC 要接得上）。"""
    turns = [
        (str(h.get("speaker") or "").strip(), str(h.get("text") or "").strip().replace('"', "'"))
        for h in (history or [])[-HISTORY_TURNS:]
        if isinstance(h, dict)
    ]
    turns = [(who, text[:200]) for who, text in turns if who and text]
    if not turns:
        return ""
    return "Conversation so far:\n" + "\n".join(f'{who}: "{text}"' for who, text in turns) + "\n"


# 之前聊天里这个人对对方说过的话：(说话的人, 听的人) -> 最近几句原话。模拟器启动时接上（见 GameLoop.said_to）。
EARLIER_LINES: Optional[Callable[[str, str], List[str]]] = None


def earlier_block(speaker: str, listener: str) -> str:
    """之前已经跟对方说过的话：别再说一遍，接着往下聊。"""
    if EARLIER_LINES is None or not speaker or not listener:
        return ""
    try:
        lines = [str(t).strip().replace('"', "'") for t in EARLIER_LINES(speaker, listener) if str(t).strip()]
    except Exception:  # noqa: BLE001
        return ""
    if not lines:
        return ""
    said = "\n".join(f'- "{t[:160]}"' for t in lines[-4:])
    return (
        f"\nEarlier you already told {listener}:\n{said}\n"
        "Don't repeat that (not even reworded). Move on: answer what they say now, mention what changed, "
        "or talk about something else.\n"
    )


def situation_brief(world, name: str) -> str:
    """说话的人此刻的处境：在忙什么、缺什么、挂着什么委托、店里卖什么缺什么。"""
    if world is None or not name:
        return ""
    lines: List[str] = []
    try:
        rows = world.run_query(
            """
            MATCH (p:Person {name: $n})
            RETURN coalesce(p.doing, '') AS doing, coalesce(p.coins, 0) AS coins, coalesce(p.trader, false) AS trader
            """,
            n=name,
        ) or []
    except Exception:
        rows = []
    if rows:
        if rows[0].get("doing"):
            lines.append(f"Right now you are trying to {rows[0]['doing']}.")
        lines.append(f"You have {int(float(rows[0].get('coins') or 0))} coins.")
    try:
        from job_board import open_requests

        for r in open_requests(world):
            if r["poster"] == name:
                pay = f"you'll pay {r['reward']} coins to whoever brings it" if r.get("reward") else "you can't pay much"
                lines.append(f"You NEED {r['kind']} and posted a request for it ({pay}). Reason: {r.get('reason') or '-'}")
    except Exception:
        pass
    if rows and rows[0].get("trader"):
        try:
            from economy import shop_of, shop_shortages

            shop = shop_of(world, name)
            if shop.get("sells"):
                lines.append(f"You run a shop selling: {', '.join(shop['sells'])}.")
            missing = shop_shortages(world, name)
            if missing:
                lines.append(f"Your shop is out of: {', '.join(missing)} - you'd welcome anyone bringing some.")
        except Exception:
            pass
    return "\n".join(lines)


def _build_dialogue_prompt(
    *,
    speaker_a: str,
    speaker_b: str,
    location: str,
    safe_msg: str,
    agent_b,
    style: str,
    clue_block: str,
    mem_hints: List[str],
    limits_block: str,
    limits: Dict[str, Any],
    retry_note: str = "",
    history: Optional[List[Dict[str, str]]] = None,
    speaker_is_player: bool = True,
    situation: str = "",
    heart_block: str = "",
    favor_block: str = "",
) -> str:
    """尽量短的对话 prompt，降低 DashScope 超时概率。"""
    retry = f"{retry_note}\n" if retry_note else ""
    so_far = history_block(history)
    hints = "; ".join(mem_hints[:3]) if mem_hints else "none"
    # 压缩 knowledge，避免长上下文拖慢
    clue = (clue_block or "").strip()
    if len(clue) > 600:
        clue = clue[:600] + "…"
    heart = f"{heart_block}\n" if heart_block else ""
    favor = f"{favor_block}\n" if favor_block else ""
    return f"""{retry}You are {speaker_b} at {location}. Stay in character. Reply in the language the player writes in (English if unsure).
{so_far}{'Player (' + speaker_a + ')' if speaker_is_player else speaker_a}: "{safe_msg or '(greets you)'}"
Profile: {agent_b.personality}; {agent_b.background}; mood={agent_b.mood}; style={style}
Your situation:
{situation or '(nothing pressing)'}
If they offer help or ask what you need, answer concretely from your situation: what you need, where it can be
found or how it is made, and what you'll give for it. Don't make up needs you don't have.
{heart}Knowledge (do not invent beyond this):
{clue}
Memory hints: {hints}
{DIALOGUE_CRAFT}
{limits_block}
{favor}Return ONLY JSON: {{"lines":[{{"speaker":"{speaker_b}","text":"..."}}],"trust_delta":0,"mood":"how you feel now, one word"}}
1-{limits['npc_lines_max']} NPC lines, each 1-{limits['npc_text']} chars. No player echo. trust_delta in -1..2.
Only include facts you actually say; the listener learns from your spoken words, not from unspoken knowledge.
JSON:"""


def _soft_npc_fallback(
    *,
    speaker_a: str,
    speaker_b: str,
    location: str,
    player_message: str,
    style: str = "",
    knowledge=None,
) -> Dict[str, Any]:
    """Qwen 超时/失败时的轻量台词，避免对话空白。"""
    msg = (player_message or "").strip()
    role = ""
    mood = ""
    if knowledge is not None:
        role = str(getattr(knowledge, "role", "") or "")
        mood = str(getattr(knowledge, "mood", "") or "")
    low = (style + " " + role + " " + mood).lower()
    if "antagonist" in low or "liar" in low or "thief" in low or "nervous" in low:
        npc_text = "Hm? Not now. I've got enough on my mind without your questions."
    elif "guard" in low or "captain" in low:
        npc_text = "Keep it short, I'm on duty. What do you want?"
    elif any(w in low for w in ("sad", "tired", "worried", "anxious")):
        npc_text = "Sorry… my head's somewhere else today. Ask me again in a little while?"
    else:
        npc_text = "Oh — hello. Give me a moment, I lost my train of thought."
    if len(npc_text) > 160:
        npc_text = npc_text[:160]
    lines: List[Dict[str, str]] = []
    if msg:
        lines.append({"speaker": speaker_a, "text": msg})
    lines.append({"speaker": speaker_b, "text": npc_text})
    parts = [f'{ln["speaker"]}: "{ln["text"]}"' for ln in lines]
    return {
        "summary": f"At {location}, " + " | ".join(parts),
        "lines": lines,
        "trust_delta": 0,
        "source": "soft_fallback",
    }


def _llm_dialogue(
    world,
    speaker_a: str,
    speaker_b: str,
    location: str,
    *,
    memory=None,
    player_message: str = "",
    knowledge=None,
    history: Optional[List[Dict[str, str]]] = None,
) -> Optional[Dict[str, Any]]:
    import time as _time

    try:
        from llm_router import primary_available

        if not primary_available():
            raise RuntimeError("OPENAI_API_KEY not configured")
    except Exception as e:
        logger.warning("primary LLM unavailable: %s", e)
        _record_dialogue_trace(
            prompt="",
            raw="",
            ok=False,
            error=f"primary unavailable: {e}",
            meta={
                "speaker_a": speaker_a,
                "speaker_b": speaker_b,
                "location": location,
                "player_message": player_message,
                "provider": "qwen",
                "fallback_used": False,
            },
        )
        return None

    agent_b = AgentProfile.from_world(world, speaker_b) if world else AgentProfile(world, speaker_b)
    if memory:
        agent_b.set_memory(memory)

    from gbnf import DIALOGUE_FIELD_LIMITS, dialogue_limits_prompt_block

    limits = DIALOGUE_FIELD_LIMITS
    msg = player_message.strip()
    if len(msg) > limits["player_text"]:
        msg = msg[: limits["player_text"]]
    safe_msg = msg.replace('"', "'")
    clue_block = knowledge_prompt_block(knowledge) if knowledge else "No knowledge loaded."
    mem = (knowledge.memory_snippets if knowledge else []) or []
    mem_hints = _shared_history(memory, speaker_b, speaker_a) + [m[:160] for m in mem[:3] if m]
    heart_block, favor_block = _heart_blocks(world, speaker_b, speaker_a, knowledge)
    style = ""
    if knowledge:
        style = knowledge.speech_style or knowledge.personality

    limits_block = dialogue_limits_prompt_block()
    meta = {
        "phase": "reply",
        "speaker_a": speaker_a,
        "speaker_b": speaker_b,
        "location": location,
        "player_message": msg,
        "provider": "qwen",
        "fallback_used": False,
    }

    prompt = _build_dialogue_prompt(
        speaker_a=speaker_a,
        speaker_b=speaker_b,
        location=location,
        safe_msg=safe_msg,
        agent_b=agent_b,
        style=style,
        clue_block=clue_block,
        mem_hints=mem_hints,
        limits_block=limits_block,
        limits=limits,
        history=history,
        speaker_is_player=_is_player_name(world, speaker_a),
        situation=situation_brief(world, speaker_b) + earlier_block(speaker_b, speaker_a),
        heart_block=heart_block,
        favor_block=favor_block,
    )

    t0 = _time.perf_counter()
    gbnf_info: Dict[str, Any] = {
        "used": False,
        "error": "",
        "grammar_preview": "",
        "provider": "qwen",
        "fallback_used": False,
    }
    text = ""
    last_raw = ""
    primary_failed = False
    try:
        text, gbnf_info = _invoke_dialogue_primary(prompt)
        last_raw = text or ""
        if not (text or "").strip():
            primary_failed = True
            gbnf_info["error"] = "empty primary output"
            logger.warning("dialogue primary returned empty")
            _record_dialogue_trace(
                prompt=prompt,
                raw="",
                ok=False,
                error="empty primary output",
                meta={**meta, "attempt": 1},
                gbnf=gbnf_info,
                elapsed_ms=(_time.perf_counter() - t0) * 1000,
            )
    except Exception as e:
        primary_failed = True
        elapsed = (_time.perf_counter() - t0) * 1000
        logger.warning("LLM invoke failed: %s", e)
        try:
            from model_instances import destroy_singleton

            destroy_singleton()
        except Exception:
            pass
        gbnf_info = {
            "used": False,
            "error": str(e),
            "grammar_preview": "",
            "provider": "qwen",
            "fallback_used": False,
        }
        _record_dialogue_trace(
            prompt=prompt,
            raw="",
            ok=False,
            error=f"invoke: {e}",
            meta={**meta, "attempt": 1},
            gbnf=gbnf_info,
            elapsed_ms=elapsed,
        )
        text = ""
        last_raw = ""

    def _finalize_from_raw(raw_text: str, used_prompt: str, ginfo: Dict[str, Any], attempt: int) -> Optional[Dict[str, Any]]:
        elapsed = (_time.perf_counter() - t0) * 1000
        logger.info(
            "dialogue LLM attempt=%s raw len=%s provider=%s gbnf=%s head=%r",
            attempt,
            len(raw_text),
            ginfo.get("provider"),
            ginfo.get("used"),
            (raw_text or "")[:200],
        )
        attempt_meta = {
            **meta,
            "attempt": attempt,
            "provider": ginfo.get("provider", meta.get("provider")),
            "fallback_used": bool(ginfo.get("fallback_used")),
        }
        parsed = _parse_dialogue_payload(raw_text, speaker_a, speaker_b)
        if not parsed:
            _record_dialogue_trace(
                prompt=used_prompt,
                raw=raw_text,
                ok=False,
                error="unparseable output",
                meta=attempt_meta,
                gbnf=ginfo,
                elapsed_ms=elapsed,
            )
            return None

        # 玩家台词由服务端注入（不占用模型输出）；模型只返回 NPC 行
        npc_only = []
        for ln in parsed["lines"]:
            if ln.get("speaker") != speaker_b:
                continue
            t = ln.get("text") or ""
            if len(t) > limits["npc_text"]:
                t = t[: limits["npc_text"]]
            npc_only.append({"speaker": speaker_b, "text": t})
        if msg:
            parsed["lines"] = [{"speaker": speaker_a, "text": msg}] + npc_only
        else:
            parsed["lines"] = npc_only

        npc_lines = npc_only
        if not npc_lines:
            _record_dialogue_trace(
                prompt=used_prompt,
                raw=raw_text,
                ok=False,
                error="no NPC lines after parse",
                meta=attempt_meta,
                parsed=parsed,
                gbnf=ginfo,
                elapsed_ms=elapsed,
            )
            return None

        if any(_is_placeholder_npc_text(ln.get("text", "")) for ln in npc_lines):
            _record_dialogue_trace(
                prompt=used_prompt,
                raw=raw_text,
                ok=False,
                error="placeholder NPC text",
                meta=attempt_meta,
                parsed=parsed,
                gbnf=ginfo,
                elapsed_ms=elapsed,
            )
            return None  # signal retry / fail

        parts = [f'{ln["speaker"]}: "{ln["text"]}"' for ln in parsed["lines"]]
        parsed["summary"] = f"At {location}, " + " | ".join(parts)
        parsed["source"] = "llm"
        tid = _record_dialogue_trace(
            prompt=used_prompt,
            raw=raw_text,
            ok=True,
            error="",
            meta=attempt_meta,
            parsed={
                "lines": parsed["lines"],
                "trust_delta": parsed.get("trust_delta"),
                "mood": parsed.get("mood"),
                "favor": parsed.get("favor"),
            },
            gbnf=ginfo,
            elapsed_ms=elapsed,
        )
        parsed["trace_id"] = tid
        return parsed

    result = _finalize_from_raw(text, prompt, gbnf_info, 1) if text else None
    if result is not None:
        return result

    # 首次若已超时/空响应，不再二次请求（避免再卡 30s）；直接短提示再试或软回退
    retry_prompt = _build_dialogue_prompt(
        speaker_a=speaker_a,
        speaker_b=speaker_b,
        location=location,
        safe_msg=safe_msg,
        agent_b=agent_b,
        style=style,
        clue_block=clue_block,
        mem_hints=mem_hints,
        limits_block=limits_block,
        limits=limits,
        history=history,
        speaker_is_player=_is_player_name(world, speaker_a),
        situation=situation_brief(world, speaker_b) + earlier_block(speaker_b, speaker_a),
        retry_note=(
            f"IMPORTANT: Previous answer was invalid (placeholder or bad JSON). "
            f"Write a real in-character reply for {speaker_b} about the player's question. "
            f"Do not output angle brackets or template words."
        ),
        heart_block=heart_block,
        favor_block=favor_block,
    )
    text2 = ""
    gbnf_info2: Dict[str, Any] = {
        "used": False,
        "error": "",
        "grammar_preview": "",
        "provider": "qwen",
        "fallback_used": False,
    }
    if not primary_failed:
        try:
            text2, gbnf_info2 = _invoke_dialogue_primary(retry_prompt)
            last_raw = text2 or last_raw
        except Exception as e:
            logger.warning("LLM retry invoke failed: %s", e)
            text2 = ""
            gbnf_info2["error"] = str(e)

    if text2:
        result = _finalize_from_raw(text2, retry_prompt, gbnf_info2, 2)
        if result is not None:
            return result

    # 本地 GBNF（仅当模型已加载或显式开启，避免首次加载卡死对话）
    use_local = os.getenv("DIALOGUE_LOCAL_FALLBACK", "0").strip() in ("1", "true", "yes")
    try:
        from model_instances import is_local_gbnf_ready
        import model_instances as mi

        already_loaded = getattr(mi, "_llama_instance", None) is not None
        use_local = use_local or already_loaded
        if use_local and is_local_gbnf_ready():
            draft = last_raw or (
                f"Player said: {msg}. NPC {speaker_b} ({style or agent_b.personality}) "
                f"at {location} should reply in character. Knowledge:\n{clue_block}"
            )
            text3, gbnf_info3 = _repair_dialogue_local_gbnf(
                draft,
                speaker_a=speaker_a,
                speaker_b=speaker_b,
                player_message=msg,
            )
            result = _finalize_from_raw(text3, retry_prompt, gbnf_info3, 3)
            if result is not None:
                return result
    except Exception as e:
        logger.warning("dialogue GBNF fallback failed: %s", e)

    # 软回退：保证有一句 NPC 台词，避免 inspect 显示 null / 对话空白
    soft = _soft_npc_fallback(
        speaker_a=speaker_a,
        speaker_b=speaker_b,
        location=location,
        player_message=msg,
        style=style or getattr(agent_b, "personality", ""),
        knowledge=knowledge,
    )
    soft["trace_id"] = _record_dialogue_trace(
        prompt=prompt,
        raw=json.dumps(soft, ensure_ascii=False),
        ok=True,
        error="soft_fallback_after_primary_failure",
        meta={**meta, "attempt": 4, "provider": "soft_fallback", "fallback_used": True},
        parsed={"lines": soft["lines"], "trust_delta": soft.get("trust_delta")},
        gbnf={"used": False, "provider": "soft_fallback", "fallback_used": True},
        elapsed_ms=(_time.perf_counter() - t0) * 1000,
    )
    return soft


def _llm_unavailable_result(
    speaker_a: str,
    speaker_b: str,
    location: str,
    player_message: str,
    *,
    error: str,
) -> Dict[str, Any]:
    """无 LLM 时不生成任何 NPC 台词。"""
    msg = (player_message or "").strip()
    lines: List[Dict[str, str]] = []
    if msg:
        lines.append({"speaker": speaker_a, "text": msg})
    return {
        "summary": f"Dialogue unavailable at {location}: {error}",
        "lines": lines,
        "trust_delta": 0,
        "source": "error",
        "error": error,
    }


def generate_talk_dialogue(
    world,
    speaker_a: str,
    speaker_b: str,
    location: str,
    *,
    memory=None,
    force_mock: bool = False,
    player_message: str = "",
    history: Optional[List[Dict[str, str]]] = None,
    listener_speaks: bool = True,
    apply_trust: bool = True,
) -> Dict[str, Any]:
    """
    返回:
      summary, lines, trust_delta, source, trust
      player_message, utterance_source (provided|llm|empty)
    NPC 台词仅来自 LLM；force_mock / 模型不可用时 source=error 且无 NPC 行。
    无 player_message 时先 LLM 生成发起方发言，再 LLM 生成对方回复。
    apply_trust=False：只生成不写图（后台预生成用），好感留到 apply_talk_dialogue 再记。
    """
    msg = (player_message or "").strip()
    utterance_source = "provided" if msg else "empty"
    utterance_meta: Dict[str, Any] = {}

    if force_mock:
        result = _llm_unavailable_result(
            speaker_a,
            speaker_b,
            location,
            msg,
            error="force_mock: dialogue requires LLM",
        )
        result["trust"] = 0
        result["player_message"] = msg
        result["utterance_source"] = utterance_source
        return result

    try:
        import sys
        from pathlib import Path

        web_path = str(Path(__file__).resolve().parent.parent / "web")
        if web_path not in sys.path:
            sys.path.insert(0, web_path)
        from llm_service import get_llm_status

        available = bool(get_llm_status().get("available"))
    except Exception:
        available = True

    if not available:
        result = _llm_unavailable_result(
            speaker_a,
            speaker_b,
            location,
            msg,
            error="LLM model file not available",
        )
        result["player_message"] = msg
        result["utterance_source"] = utterance_source
        return result

    # Call 1: 发起方发言（UI 未传入时）
    if not msg:
        try:
            msg, utterance_meta = _llm_speaker_utterance(
                world,
                speaker_a,
                speaker_b,
                location,
                memory=memory,
            )
            msg = (msg or "").strip()
            utterance_source = "llm" if msg else "empty"
        except Exception as e:
            logger.warning("speaker utterance generation failed: %s", e)
            msg = ""
            utterance_source = "empty"

    if not listener_speaks:
        # 对方是玩家：只说发起方这一句，回话由玩家自己在对话框里打
        return {
            "summary": f"{speaker_a} spoke to {speaker_b}",
            "lines": [{"speaker": speaker_a, "text": msg}] if msg else [],
            "trust_delta": 0,
            "trust": 0,
            "source": "llm" if msg else "error",
            "player_message": "",
            "utterance_source": utterance_source,
            "awaiting_reply": bool(msg),
        }

    knowledge = load_npc_knowledge(
        world, speaker_b, memory=memory, player_message=msg
    )

    # Call 2: 被谈话方回复
    result = _llm_dialogue(
        world,
        speaker_a,
        speaker_b,
        location,
        memory=memory,
        player_message=msg,
        knowledge=knowledge,
        history=history,
    )
    if result is None:
        result = _llm_unavailable_result(
            speaker_a,
            speaker_b,
            location,
            msg,
            error="LLM dialogue failed or returned unparseable output",
        )
        try:
            import sys
            from pathlib import Path

            web_path = str(Path(__file__).resolve().parent.parent / "web")
            if web_path not in sys.path:
                sys.path.insert(0, web_path)
            from llm_trace import latest_llm_trace

            latest = latest_llm_trace(kind="dialogue")
            if latest:
                result["trace_id"] = latest.get("id")
                result["inspect_url"] = f"/inspect?id={latest.get('id')}"
        except Exception:
            pass

    result["player_message"] = msg
    result["utterance_source"] = utterance_source
    if utterance_meta.get("trace_id"):
        result["utterance_trace_id"] = utterance_meta["trace_id"]
    if result.get("source") != "llm":
        result.pop("favor", None)
        result.pop("mood", None)
    for key in ("favor", "mood"):
        if utterance_meta.get(key):
            result[f"speaker_{key}"] = utterance_meta[key]
    if not apply_trust:
        result["trust_base"] = knowledge.trust_toward_player
        return result
    return _settle_trust(world, speaker_a, speaker_b, result, knowledge.trust_toward_player)


def _settle_trust(world, speaker_a: str, speaker_b: str, result: Dict[str, Any], base: int) -> Dict[str, Any]:
    """按这段话记好感：NPC 之间改 FEELS，对玩家改 trust_toward_player。"""
    delta = int(result.get("trust_delta") or 0)
    raw_delta = delta
    if not _is_player_name(world, speaker_a):
        # NPC 之间：回话的人对发起的人的好感随这段话变（trust_toward_player 只记对玩家的）
        if world is not None and delta and result.get("source") == "llm":
            try:
                from relations import adjust as _adjust_feeling

                _adjust_feeling(world, speaker_b, speaker_a, delta, reason="how our talk went")
            except Exception:  # noqa: BLE001
                pass
        delta = 0
    if world is not None and delta and result.get("source") == "llm":
        new_trust = adjust_trust(world, speaker_b, delta)
    else:
        new_trust = base + (delta if result.get("source") == "llm" else 0)
    result["trust"] = new_trust
    result["trust_delta"] = delta if result.get("source") == "llm" else 0
    if world is not None and raw_delta and result.get("source") == "llm":
        # 谈话结果同时传到 speaker_b 所在势力对 speaker_a 的声望
        try:
            notes = ripple(world, speaker_a, speaker_b, raw_delta * 0.5, why=f"talk with {speaker_b}")
            if notes:
                result["faction_changes"] = notes
        except Exception as e:
            logger.debug("talk ripple failed: %s", e)
    return result


def unavailable_dialogue(speaker_a: str, speaker_b: str, location: str, error: str) -> Dict[str, Any]:
    """没有台词的对话结果（例如预生成超时）。"""
    result = _llm_unavailable_result(speaker_a, speaker_b, location, "", error=error)
    result.update(trust=0, player_message="", utterance_source="empty")
    return result


def write_dialogue_memories(
    memory,
    lines: List[Dict[str, str]],
    location: str,
    *,
    speaker_a: str,
    speaker_b: str,
    player_message: str = "",
) -> None:
    """为双方写入 dialogue 记忆（听者必须能看到对方说的话）。"""
    if memory is None:
        return
    msg = (player_message or "").strip()
    for line in lines:
        speaker = line.get("speaker", "")
        text = line.get("text", "")
        if not speaker or not text:
            continue
        other = speaker_b if speaker == speaker_a else speaker_a
        if speaker == speaker_a:
            said = f"You told {other} at {location}: {text}"
            heard = f"{speaker_a} said to you at {location}: {text}"
        else:
            said = f"You told {speaker_a} at {location}: {text}"
            heard = f"{speaker} told you at {location}: {text}"
        # 说话者
        memory.add(
            speaker,
            said,
            type="dialogue",
            location=location,
            actor=speaker,
            target=other,
            with_player=speaker_a,
            player_message=msg[:200] if msg else "",
        )
        # 听者（关键：玩家侧必须留下线索文本，否则只会看到 talk->ok）
        if other and other != speaker:
            memory.add(
                other,
                heard,
                type="dialogue",
                location=location,
                actor=speaker,
                target=other,
                with_player=speaker_a,
                player_message=msg[:200] if msg else "",
            )


def _apply_heart_outcomes(
    world,
    memory,
    dialogue: Dict[str, Any],
    speaker_a: str,
    speaker_b: str,
    location: str,
) -> None:
    """对话后的情绪变化写回人物；真心求助落成 favor 任务并记入双方记忆。"""
    if world is None:
        return
    for who, key in ((speaker_b, "mood"), (speaker_a, "speaker_mood")):
        if dialogue.get(key):
            apply_mood(world, who, dialogue[key])
    for giver, helper, key in (
        (speaker_b, speaker_a, "favor"),
        (speaker_a, speaker_b, "speaker_favor"),
    ):
        favor = dialogue.get(key)
        if not favor:
            continue
        try:
            quest = register_favor(world, giver, helper, favor, location=location)
        except Exception as e:
            logger.debug("register favor failed: %s", e)
            quest = None
        if not quest:
            continue
        dialogue.setdefault("quests_created", []).append(quest)
        if memory is None:
            continue
        why = favor.get("why") or ""
        reward = favor.get("reward") or ""
        memory.add(
            giver,
            f"You asked {helper} at {location} for {favor['item']}: {why}",
            type="favor",
            location=location,
            actor=giver,
            target=helper,
        )
        memory.add(
            helper,
            f"{giver} asked you at {location} for {favor['item']}: {why}"
            + (f" They offered: {reward}" if reward else ""),
            type="favor",
            location=location,
            actor=giver,
            target=helper,
        )


def handle_talk_dialogue(
    world,
    speaker_a: str,
    speaker_b: str,
    location: str,
    *,
    memory=None,
    force_mock: bool = False,
    player_message: str = "",
    history: Optional[List[Dict[str, str]]] = None,
    listener_speaks: bool = True,
) -> Dict[str, Any]:
    """生成对话、更新信任、写入记忆，并转移可分享事实。"""
    dialogue = generate_talk_dialogue(
        world,
        speaker_a,
        speaker_b,
        location,
        memory=memory,
        force_mock=force_mock,
        player_message=player_message,
        history=history,
        listener_speaks=listener_speaks,
        apply_trust=False,
    )
    return apply_talk_dialogue(
        world, dialogue, speaker_a, speaker_b, location, memory=memory, player_message=player_message
    )


def apply_talk_dialogue(
    world,
    dialogue: Dict[str, Any],
    speaker_a: str,
    speaker_b: str,
    location: str,
    *,
    memory=None,
    player_message: str = "",
) -> Dict[str, Any]:
    """对话真的发生了：记好感、写记忆、转移事实（generate_talk_dialogue 只生成不写）。"""
    if "trust_base" in dialogue:
        _settle_trust(world, speaker_a, speaker_b, dialogue, int(dialogue.pop("trust_base") or 0))
    lines = dialogue.get("lines") or []
    resolved_msg = (dialogue.get("player_message") or player_message or "").strip()
    if not resolved_msg:
        for ln in lines:
            if ln.get("speaker") == speaker_a and (ln.get("text") or "").strip():
                resolved_msg = (ln.get("text") or "").strip()
                break
    if lines:
        write_dialogue_memories(
            memory,
            lines,
            location,
            speaker_a=speaker_a,
            speaker_b=speaker_b,
            player_message=resolved_msg,
        )
    # 按真实台词学 Fact：只有 NPC 说出的内容匹配到某条 Fact 才写入图
    try:
        from npc_knowledge import transfer_facts_from_dialogue

        gained = transfer_facts_from_dialogue(
            world, speaker_b, speaker_a, lines
        )
        # 反过来也一样：发起的人说出口的事实，对方也就知道了
        told = transfer_facts_from_dialogue(world, speaker_a, speaker_b, lines)
        if told:
            dialogue["facts_told"] = told
            if memory is not None:
                memory.add(
                    speaker_b,
                    f"Learned from what {speaker_a} said: {', '.join(told)}",
                    type="fact",
                    location=location,
                    actor=speaker_a,
                    facts=told,
                )
        if gained:
            dialogue["facts_gained"] = gained
            if memory is not None:
                memory.add(
                    speaker_a,
                    f"Learned from what {speaker_b} said: {', '.join(gained)}",
                    type="fact",
                    location=location,
                    actor=speaker_b,
                    facts=gained,
                )
    except Exception:
        pass
    _apply_heart_outcomes(world, memory, dialogue, speaker_a, speaker_b, location)
    try:
        from knowledge import share_from_dialogue

        for listener, notes in share_from_dialogue(world, lines, speaker_a, speaker_b).items():
            dialogue.setdefault("knowledge_gained", {})[listener] = notes
            if memory is not None:
                memory.add(
                    listener,
                    f"Heard at {location}: {'; '.join(notes)}",
                    type="fact",
                    location=location,
                )
    except Exception:
        pass
    return dialogue
