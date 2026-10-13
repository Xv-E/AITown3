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
from typing import Any, Dict, List, Optional

from agent_profile import AgentProfile
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
    return {"lines": lines, "trust_delta": trust_delta}


def _invoke_dialogue_primary(prompt: str) -> tuple[str, Dict[str, Any]]:
    """Qwen 主路径（无 GBNF）。返回 (raw_text, provider_info)。"""
    from llm_router import invoke_primary

    # 对话只需 1–2 句短 JSON，token 宜少，避免拖到超时后 raw=null
    max_tokens = int(os.getenv("DIALOGUE_MAX_TOKENS", "220"))
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
) -> str:
    retry = f"{retry_note}\n" if retry_note else ""
    hints = "; ".join(mem_hints[:3]) if mem_hints else "none"
    goal = (getattr(agent_a, "goal", "") or "").strip() or "(none set)"
    return f"""{retry}You are {speaker_a} at {location}. You will speak ONE line to {speaker_b}.
Profile: {agent_a.personality}; {agent_a.background}; mood={agent_a.mood}
Current goal: {goal}
About {speaker_b}: {partner_brief}
What you know (do not invent beyond this):
{facts_block}
Memory hints: {hints}
Write one short in-character utterance (ask, greet, share, or press for info). Prefer advancing your goal. Speak in English only. Max {max_chars} chars.
Return ONLY JSON: {{"text":"..."}}
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

    prompt = _build_speaker_utterance_prompt(
        speaker_a=speaker_a,
        speaker_b=speaker_b,
        location=location,
        agent_a=agent_a,
        partner_brief=partner_brief,
        facts_block=facts_block,
        mem_hints=mem_hints,
        max_chars=max_chars,
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
            max_chars=max_chars,
            retry_note=(
                "IMPORTANT: Previous answer was invalid. "
                f"Output real speech from {speaker_a} to {speaker_b} as JSON."
            ),
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
        tid = _record_dialogue_trace(
            prompt=prompt,
            raw=raw,
            ok=True,
            error="",
            meta=meta,
            parsed={"text": text},
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
) -> str:
    """尽量短的对话 prompt，降低 DashScope 超时概率。"""
    retry = f"{retry_note}\n" if retry_note else ""
    hints = "; ".join(mem_hints[:2]) if mem_hints else "none"
    # 压缩 knowledge，避免长上下文拖慢
    clue = (clue_block or "").strip()
    if len(clue) > 600:
        clue = clue[:600] + "…"
    return f"""{retry}You are {speaker_b} at {location}. Stay in character. Reply in English only.
Player ({speaker_a}): "{safe_msg or '(greets you)'}"
Profile: {agent_b.personality}; {agent_b.background}; mood={agent_b.mood}; style={style}
Knowledge (do not invent beyond this):
{clue}
Memory hints: {hints}
{limits_block}
Return ONLY JSON: {{"lines":[{{"speaker":"{speaker_b}","text":"..."}}],"trust_delta":0}}
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
        npc_text = "The key? I… I haven't seen it. Don't look at me like that."
    elif "guard" in low or "captain" in low:
        npc_text = "I'm looking into the castle key myself. Share any leads you have."
    else:
        npc_text = "The key's missing? It's quite the stir — I've only heard rumors."
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
    mem_hints = [m[:160] for m in mem[:3] if m]
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
        retry_note=(
            f"IMPORTANT: Previous answer was invalid (placeholder or bad JSON). "
            f"Write a real in-character reply for {speaker_b} about the player's question. "
            f"Do not output angle brackets or template words."
        ),
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
) -> Dict[str, Any]:
    """
    返回:
      summary, lines, trust_delta, source, trust
      player_message, utterance_source (provided|llm|empty)
    NPC 台词仅来自 LLM；force_mock / 模型不可用时 source=error 且无 NPC 行。
    无 player_message 时先 LLM 生成发起方发言，再 LLM 生成对方回复。
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

    delta = int(result.get("trust_delta") or 0)
    if world is not None and delta and result.get("source") == "llm":
        new_trust = adjust_trust(world, speaker_b, delta)
    else:
        new_trust = knowledge.trust_toward_player + (
            delta if result.get("source") == "llm" else 0
        )
    result["trust"] = new_trust
    result["trust_delta"] = delta if result.get("source") == "llm" else 0
    result["player_message"] = msg
    result["utterance_source"] = utterance_source
    if utterance_meta.get("trace_id"):
        result["utterance_trace_id"] = utterance_meta["trace_id"]
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


def handle_talk_dialogue(
    world,
    speaker_a: str,
    speaker_b: str,
    location: str,
    *,
    memory=None,
    force_mock: bool = False,
    player_message: str = "",
) -> Dict[str, Any]:
    """生成对话、更新信任、写入记忆，并转移可分享事实。"""
    from npc_knowledge import transfer_shareable_facts

    dialogue = generate_talk_dialogue(
        world,
        speaker_a,
        speaker_b,
        location,
        memory=memory,
        force_mock=force_mock,
        player_message=player_message,
    )
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
