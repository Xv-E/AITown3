"""
Qwen（OpenAI 兼容）主推理 + 本地 LlamaCpp GBNF 格式兜底。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from env_loader import load_project_env

load_project_env()

logger = logging.getLogger(__name__)

T = TypeVar("T")


def invoke_primary(
    prompt: str,
    *,
    system: Optional[str] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    model: Optional[str] = None,
) -> str:
    """调用主模型（Qwen / OpenAI 兼容 API），无 GBNF。model 不给就用 OPENAI_MODEL。"""
    from model_instances import chat_completion

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    kwargs: Dict[str, Any] = {}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if timeout is not None:
        kwargs["timeout"] = timeout
    if model:
        kwargs["model"] = model
    return chat_completion(messages, **kwargs)


def invoke_local_gbnf(
    prompt: str,
    grammar_str: str,
    *,
    max_tokens: Optional[int] = None,
) -> str:
    """本地 LlamaCpp + GBNF，仅用于格式修复。"""
    from llama_cpp import LlamaGrammar
    from model_instances import get_llama_instance, llama_call_lock

    llm = get_llama_instance()
    grammar = LlamaGrammar.from_string(grammar_str)
    tokens = max_tokens
    if tokens is None:
        tokens = int(os.getenv("LLAMA_MAX_TOKENS", "768"))
    kwargs: Dict[str, Any] = {"grammar": grammar, "max_tokens": tokens}
    with llama_call_lock:
        try:
            raw = llm.invoke(prompt, **kwargs)
        except TypeError:
            raw = llm.invoke(prompt, grammar=grammar)
    return raw if isinstance(raw, str) else str(raw)


def build_repair_prompt(
    *,
    draft: str,
    schema_hint: str = "",
    repair_context: str = "",
) -> str:
    """把主模型草稿压成 GBNF 可接受的结构化输出。"""
    parts = [
        "Convert the draft below into ONE valid JSON value that matches the required schema.",
        "Do not add markdown. Do not invent new facts beyond the draft.",
        "Preserve meaning; fix structure, speakers, and field types only.",
    ]
    if schema_hint:
        parts.append(f"Schema / constraints:\n{schema_hint}")
    if repair_context:
        parts.append(f"Context:\n{repair_context}")
    parts.append(f"Draft to convert:\n{draft}")
    parts.append("JSON:")
    return "\n\n".join(parts)


def _is_empty_parsed(parsed: Any) -> bool:
    if parsed is None:
        return True
    if parsed == {} or parsed == []:
        return True
    return False


def invoke_structured(
    prompt: str,
    *,
    parse_fn: Callable[[str], T],
    grammar_str: str,
    repair_context: str = "",
    schema_hint: str = "",
    system: Optional[str] = None,
    max_tokens: Optional[int] = None,
    local_max_tokens: Optional[int] = None,
    accept_fn: Optional[Callable[[T], bool]] = None,
) -> Tuple[Optional[T], Dict[str, Any]]:
    """
    先主模型，再按需本地 GBNF 修复。

    parse_fn: 把 raw 文本解析为业务对象；失败应返回 None / {} / []。
    accept_fn: 可选额外校验；返回 False 视为不合格。
    """
    meta: Dict[str, Any] = {
        "provider": "qwen",
        "fallback_used": False,
        "primary_raw": "",
        "repair_raw": "",
        "error": "",
        "gbnf": {"used": False, "error": "", "grammar_preview": ""},
    }

    def _ok(parsed: T) -> bool:
        if _is_empty_parsed(parsed):
            return False
        if accept_fn is not None and not accept_fn(parsed):
            return False
        return True

    primary_raw = ""
    try:
        primary_raw = invoke_primary(prompt, system=system, max_tokens=max_tokens)
        meta["primary_raw"] = primary_raw
    except Exception as e:
        meta["error"] = f"primary: {e}"
        logger.warning("primary LLM failed: %s", e)
        primary_raw = ""

    if primary_raw:
        try:
            parsed = parse_fn(primary_raw)
            if _ok(parsed):
                return parsed, meta
        except Exception as e:
            logger.info("primary parse failed: %s", e)
            meta["error"] = f"primary_parse: {e}"

    # 本地 GBNF 兜底
    meta["fallback_used"] = True
    meta["provider"] = "local_gbnf"
    meta["gbnf"]["used"] = True
    meta["gbnf"]["grammar_preview"] = (grammar_str or "")[:1200]

    draft = primary_raw or "(no primary output)"
    repair_prompt = build_repair_prompt(
        draft=draft,
        schema_hint=schema_hint or repair_context,
        repair_context=repair_context if schema_hint else "",
    )
    try:
        repair_raw = invoke_local_gbnf(
            repair_prompt,
            grammar_str,
            max_tokens=local_max_tokens,
        )
        meta["repair_raw"] = repair_raw
        parsed2 = parse_fn(repair_raw)
        if _ok(parsed2):
            meta["error"] = ""
            return parsed2, meta
        meta["error"] = meta.get("error") or "fallback_parse_failed"
        return None, meta
    except Exception as e:
        meta["gbnf"]["error"] = str(e)
        meta["error"] = f"fallback: {e}"
        logger.warning("local GBNF repair failed: %s", e)
        return None, meta


def primary_available() -> bool:
    from model_instances import is_primary_configured

    return is_primary_configured()
