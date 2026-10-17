"""
任务线（导演第二版）：往现有世界里注入一条可证明能玩通的任务线。

    spec     数据结构 + 校验（纯）
    digest   读世界摘要（只读数据库）
    author   LLM 写 → 校验 → 验证 → 反馈重试（模型 / 验证由调用方注入）
    verify   PDDL 证明玩家能做完（纯内存快照）
    apply    写进世界（只增不删）
    runtime  每拍推进进度（GameLoop.hooks）
"""

from __future__ import annotations

from typing import Optional

from .apply import apply_questline
from .author import AuthorResult, author_questline
from .digest import WorldDigest, read_digest
from .runtime import QuestTracker, active_questlines, check_progress
from .spec import QuestlinePatch, validate
from .verify import Verification, verify


def draft_questline(world, theme: str = "", *, story_arc: str = "", llm=None, max_attempts: int = 4,
                    complete: bool = False) -> AuthorResult:
    """读世界 → LLM 写 → 校验 + PDDL 验证。不写数据库。"""
    from compiler import load_snapshot

    digest = read_digest(world, story_arc=story_arc)
    snap = load_snapshot(world)
    if llm is None:
        llm = _default_llm

    return author_questline(
        digest, theme, llm=llm,
        verify_fn=lambda patch: verify(snap, patch, digest.player),
        max_attempts=max_attempts,
        complete=complete,
    )


def _default_llm(system: str, prompt: str) -> str:
    from llm_router import invoke_primary
    from model_instances import creative_model_name

    return invoke_primary(prompt, system=system, max_tokens=5000, timeout=180, model=creative_model_name())


__all__ = [
    "AuthorResult", "QuestTracker", "QuestlinePatch", "Verification", "WorldDigest",
    "active_questlines", "apply_questline", "author_questline", "check_progress",
    "draft_questline", "read_digest", "validate", "verify",
]
