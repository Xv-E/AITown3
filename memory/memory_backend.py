"""
取 NPC 记忆单例（Neo4jMemory，记忆存在世界库的 :Memory 节点里）。

    from memory_backend import get_memory
    mem = get_memory()
"""

from __future__ import annotations

import os
import sys
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_AITOWN3_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


def get_memory() -> Any:
    """返回记忆单例。Neo4j 连不上或嵌入模型加载失败时抛异常，由调用方决定降级。"""
    for sub in ("neo4j", "memory"):
        path = os.path.join(_AITOWN3_ROOT, sub)
        if path not in sys.path:
            sys.path.insert(0, path)
    from neo4j_memory import Neo4jMemory
    mem = Neo4jMemory.get_instance()
    try:
        mem.ensure_ready()  # 现在就建索引、试算一次向量，别等到写第一条记忆时才失败
    except Exception:
        mem.close()
        raise
    return mem
