"""
LLM call tracing: in-memory ring buffer for logs and the /inspect page.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

_MAX = 160
_lock = threading.Lock()
_traces: Deque[Dict[str, Any]] = deque(maxlen=_MAX)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def record_llm_trace(
    *,
    kind: str = "dialogue",
    prompt: str = "",
    raw: str = "",
    ok: bool = False,
    error: str = "",
    meta: Optional[Dict[str, Any]] = None,
    parsed: Optional[Dict[str, Any]] = None,
    gbnf: Optional[Dict[str, Any]] = None,
    elapsed_ms: Optional[float] = None,
) -> str:
    """Append a trace record and return its id.

    kind:
      - dialogue: conversation
      - goal_decision: multi-step goal decision (Qwen / local GBNF)
      - pddl_plan: PDDL or rule-based planning result
    meta optional fields:
      - provider: "qwen" | "local_gbnf" | "pddl"
      - fallback_used: bool
      - character / step / goal / target_entity / engine
    gbnf optional fields:
      - used, error, grammar_preview, provider, fallback_used
    """
    tid = uuid.uuid4().hex[:12]
    m = dict(meta or {})
    g = dict(gbnf or {})
    if "provider" not in m and g.get("provider"):
        m["provider"] = g["provider"]
    if "fallback_used" not in m and "fallback_used" in g:
        m["fallback_used"] = bool(g.get("fallback_used"))
    entry = {
        "id": tid,
        "ts": _now_iso(),
        "kind": kind,
        "ok": bool(ok),
        "error": error or "",
        "elapsed_ms": elapsed_ms,
        "prompt": prompt or "",
        "prompt_len": len(prompt or ""),
        "raw": raw or "",
        "raw_len": len(raw or ""),
        "meta": m,
        "parsed": parsed,
        "gbnf": g,
    }
    with _lock:
        _traces.appendleft(entry)
    return tid


def list_llm_traces(*, limit: int = 40, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    with _lock:
        items = list(_traces)
    if kind:
        items = [t for t in items if t.get("kind") == kind]
    out = []
    for t in items[: max(1, min(limit, _MAX))]:
        out.append(
            {
                "id": t["id"],
                "ts": t["ts"],
                "kind": t["kind"],
                "ok": t["ok"],
                "error": t.get("error") or "",
                "elapsed_ms": t.get("elapsed_ms"),
                "prompt_len": t.get("prompt_len", 0),
                "raw_len": t.get("raw_len", 0),
                "meta": t.get("meta") or {},
                "provider": (t.get("meta") or {}).get("provider"),
                "fallback_used": bool((t.get("meta") or {}).get("fallback_used")),
                "raw_preview": (t.get("raw") or "")[:280],
            }
        )
    return out


def get_llm_trace(trace_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        for t in _traces:
            if t.get("id") == trace_id:
                return dict(t)
    return None


def clear_llm_traces() -> int:
    with _lock:
        n = len(_traces)
        _traces.clear()
    return n


def latest_llm_trace(*, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with _lock:
        for t in _traces:
            if kind is None or t.get("kind") == kind:
                return dict(t)
    return None
