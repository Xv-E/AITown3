"""
模板世界加载器 — 从 worlds/*.json 文本配置加载可玩世界。

每个文件一个世界；文件名（不含扩展名）为 world_id。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .blueprint import EventBlueprint

logger = logging.getLogger(__name__)

_WORLDS_DIR = Path(__file__).resolve().parent.parent / "worlds"
# 主世界：无缝营地。其余模板是小型演示 / 回归测试
_DEFAULT_WORLD_ID = "town_daily_life"


def worlds_dir() -> Path:
    return _WORLDS_DIR


def default_world_id() -> str:
    return _DEFAULT_WORLD_ID


def list_worlds() -> List[Dict[str, str]]:
    """列出可用模板世界（id / title / premise）。"""
    root = worlds_dir()
    if not root.is_dir():
        return []
    out: List[Dict[str, str]] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = _read_json(path)
        except Exception as e:
            logger.warning("Skip world file %s: %s", path.name, e)
            continue
        wid = str(data.get("id") or path.stem).strip() or path.stem
        out.append(
            {
                "id": wid,
                "title": str(data.get("title") or wid),
                "premise": str(data.get("premise") or ""),
                "default": wid == _DEFAULT_WORLD_ID,
            }
        )
    # 主世界排最前，界面上先看到它
    out.sort(key=lambda w: not w["default"])
    return out


def load_world(world_id: str) -> EventBlueprint:
    """按 id 加载模板世界为 EventBlueprint。"""
    wid = (world_id or "").strip()
    if not wid:
        raise ValueError("world_id is required")
    path = _resolve_world_path(wid)
    if path is None:
        known = ", ".join(w["id"] for w in list_worlds()) or "(none)"
        raise ValueError(f"Unknown world_id: {wid}. Available: {known}")
    data = _read_json(path)
    bp = EventBlueprint.from_dict(data, source="world")
    return bp


def pick_world(prompt: str = "") -> EventBlueprint:
    """按提示关键词选世界；没命中就用主世界。"""
    text = (prompt or "").lower()
    worlds = list_worlds()
    if not worlds:
        raise ValueError("No world templates found under worlds/")
    by_id = {w["id"]: w for w in worlds}
    chosen = default_world_id() if default_world_id() in by_id else worlds[0]["id"]
    for wid, meta in by_id.items():
        blob = f"{wid} {meta.get('title', '')} {meta.get('premise', '')}".lower()
        if wid in text or (meta.get("title") or "").lower() in text:
            chosen = wid
            break
        if any(tok in text and tok in blob for tok in ("钥匙", "门", "key", "door", "vault")):
            chosen = wid
            break
        if any(
            tok in text and tok in blob
            for tok in ("档案", "archive", "活板", "trapdoor", "封印", "sealed", "撬棍", "crowbar")
        ):
            chosen = wid
            break
        if any(
            tok in text and tok in blob
            for tok in (
                "女友", "生病", "蓝草", "医生", "森林",
                "girlfriend", "sick", "blueherb", "doctor", "lila",
            )
        ):
            chosen = wid
            break
        if any(
            tok in text and tok in blob
            for tok in (
                "日常", "日程", "小镇日常", "daily", "routine", "slice of life",
                "town_daily", "ordinary day", "篝火", "营地", "伐木", "采集",
                "campfire", "production", "wood", "berry",
            )
        ):
            chosen = wid
            break
    bp = load_world(chosen)
    if (prompt or "").strip() and len(prompt.strip()) > 12:
        bp.premise = prompt.strip()
    return bp


def _resolve_world_path(world_id: str) -> Optional[Path]:
    root = worlds_dir()
    direct = root / f"{world_id}.json"
    if direct.is_file():
        return direct
    for path in root.glob("*.json"):
        try:
            data = _read_json(path)
        except Exception:
            continue
        if str(data.get("id") or "").strip() == world_id:
            return path
    return None


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return data
