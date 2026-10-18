"""
按 MEMORY_BACKEND 选 NPC 记忆后端：chroma（默认）或 neo4j。

    from memory_backend import get_memory, backend_name
    mem = get_memory()          # ChromaMemory / Neo4jMemory 单例，接口相同
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_AITOWN3_ROOT = os.path.abspath(os.path.join(_HERE, ".."))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_AITOWN3_ROOT, ".env"))
except Exception:
    pass


def backend_name() -> str:
    name = (os.getenv("MEMORY_BACKEND") or "chroma").strip().lower()
    return name if name in ("chroma", "neo4j") else "chroma"


def get_memory(*, persist_path: Optional[str] = None) -> Any:
    """返回当前后端的记忆单例；加载失败时抛异常，由调用方决定降级。"""
    if backend_name() == "neo4j":
        neo4j_dir = os.path.join(_AITOWN3_ROOT, "neo4j")
        if neo4j_dir not in sys.path:
            sys.path.insert(0, neo4j_dir)
        from neo4j_memory import Neo4jMemory
        return Neo4jMemory.get_instance()

    from chroma_memory import ChromaMemory
    return ChromaMemory.get_instance(persist_path=persist_path)
