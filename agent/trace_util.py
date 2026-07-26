"""从 agent/ 侧安全写入 web/llm_trace（失败静默，不影响主流程）。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def record_trace(
    *,
    kind: str,
    prompt: str = "",
    raw: str = "",
    ok: bool = False,
    error: str = "",
    meta: Optional[Dict[str, Any]] = None,
    parsed: Optional[Dict[str, Any]] = None,
    gbnf: Optional[Dict[str, Any]] = None,
    elapsed_ms: Optional[float] = None,
) -> str:
    try:
        web_path = str(Path(__file__).resolve().parent.parent / "web")
        if web_path not in sys.path:
            sys.path.insert(0, web_path)
        from llm_trace import record_llm_trace

        return record_llm_trace(
            kind=kind,
            prompt=prompt,
            raw=raw,
            ok=ok,
            error=error,
            meta=meta,
            parsed=parsed,
            gbnf=gbnf,
            elapsed_ms=elapsed_ms,
        )
    except Exception as e:
        logger.debug("record_trace failed: %s", e)
        return ""
