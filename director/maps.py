"""
手动配置的地图 — 导演不再自己发明地点与连通关系。

地图来源（按 id 查找）：
  maps/<id>.json      专门的地图文件：{id, title, description, locations, adjacencies}
  worlds/<id>.json    任意模板世界里的地点与连通（只取地图部分）
  "current"           当前 Neo4j 里已加载的地图

locations[] 与模板世界同格式：{name, description, tags, map:[x,y], grid:[w,h]}
adjacencies[] ：{a, b}，可选 locked + key（门）
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
MAPS_DIR = _ROOT / "maps"
WORLDS_DIR = _ROOT / "worlds"
CURRENT = "current"


def _read(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: top level must be an object")
    return data


def _map_part(data: Dict[str, Any], *, map_id: str, source: str) -> Dict[str, Any]:
    locations = [dict(x) for x in (data.get("locations") or []) if isinstance(x, dict) and x.get("name")]
    names = {x["name"] for x in locations}
    adjacencies = []
    for e in data.get("adjacencies") or []:
        if isinstance(e, dict) and e.get("a") in names and e.get("b") in names:
            adjacencies.append(dict(e))
        elif isinstance(e, (list, tuple)) and len(e) >= 2 and e[0] in names and e[1] in names:
            adjacencies.append({"a": e[0], "b": e[1]})
    return {
        "id": map_id,
        "title": str(data.get("title") or map_id),
        "description": str(data.get("description") or data.get("premise") or ""),
        "source": source,
        "locations": locations,
        "adjacencies": adjacencies,
    }


def map_from_graph(world) -> Optional[Dict[str, Any]]:
    """当前图里的地点与连通（含已锁的门边）。"""
    if world is None:
        return None
    try:
        rows = world.run_query(
            """
            MATCH (l:Location)
            RETURN l.name AS name, coalesce(l.description, '') AS description,
                   coalesce(l.tags, '') AS tags, l.map_x AS mx, l.map_y AS my,
                   l.grid_w AS gw, l.grid_h AS gh
            ORDER BY name
            """
        ) or []
        edges = world.run_query(
            """
            MATCH (a:Location)-[r:ADJACENT_TO|LOCKED_ADJACENT_TO]->(b:Location)
            WHERE a.name < b.name
            RETURN a.name AS a, b.name AS b, type(r) = 'LOCKED_ADJACENT_TO' AS locked, r.key AS key
            """
        ) or []
    except Exception as e:
        logger.debug("map_from_graph failed: %s", e)
        return None
    if not rows:
        return None
    locations = []
    for r in rows:
        loc: Dict[str, Any] = {"name": r["name"], "description": r.get("description") or "",
                               "tags": r.get("tags") or ""}
        if r.get("mx") is not None and r.get("my") is not None:
            loc["map"] = [r["mx"], r["my"]]
        if r.get("gw") and r.get("gh"):
            loc["grid"] = [r["gw"], r["gh"]]
        locations.append(loc)
    adjacencies = []
    for e in edges:
        edge: Dict[str, Any] = {"a": e["a"], "b": e["b"]}
        if e.get("locked") and e.get("key"):
            edge.update(locked=True, key=e["key"])
        adjacencies.append(edge)
    return {"id": CURRENT, "title": "Current map", "description": "The map loaded right now.",
            "source": "graph", "locations": locations, "adjacencies": adjacencies}


def list_maps(world=None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if world is not None and map_from_graph(world):
        out.append({"id": CURRENT, "title": "Current map", "source": "graph"})
    for root, source in ((MAPS_DIR, "maps"), (WORLDS_DIR, "worlds")):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            try:
                data = _read(path)
            except Exception as e:
                logger.warning("Skip map file %s: %s", path.name, e)
                continue
            if not data.get("locations"):
                continue
            mid = str(data.get("id") or path.stem)
            if any(m["id"] == mid for m in out):
                continue
            out.append({"id": mid, "title": str(data.get("title") or mid), "source": source,
                        "locations": len(data.get("locations") or [])})
    return out


def load_map(map_id: Optional[str] = None, *, world=None) -> Dict[str, Any]:
    """按 id 取地图；不传 id 时依次用：当前图 → DIRECTOR_MAP → maps/ 第一张 → worlds/ 第一张。"""
    mid = (map_id or "").strip()
    if not mid:
        cur = map_from_graph(world)
        if cur:
            return cur
        mid = os.getenv("DIRECTOR_MAP", "").strip()
    if mid == CURRENT:
        cur = map_from_graph(world)
        if not cur:
            raise ValueError("No map is loaded right now; load a template world or pass map_id")
        return cur
    if mid:
        for root, source in ((MAPS_DIR, "maps"), (WORLDS_DIR, "worlds")):
            for path in sorted(root.glob("*.json")) if root.is_dir() else []:
                data = _read(path)
                if str(data.get("id") or path.stem) == mid and data.get("locations"):
                    return _map_part(data, map_id=mid, source=source)
        raise ValueError(f"Unknown map_id: {mid}")
    for m in list_maps():
        return load_map(m["id"])
    raise ValueError("No hand-configured map found; add one under maps/ or worlds/")


def neighbors(map_data: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {x["name"]: [] for x in map_data.get("locations") or []}
    for e in map_data.get("adjacencies") or []:
        if e.get("locked"):
            continue
        out.setdefault(e["a"], []).append(e["b"])
        out.setdefault(e["b"], []).append(e["a"])
    return out


def map_prompt_block(map_data: Dict[str, Any]) -> str:
    """给 LLM 看的固定地图：地点、简介、连通。"""
    lines = [f"FIXED MAP '{map_data.get('title') or map_data.get('id')}' — use ONLY these places:"]
    nb = neighbors(map_data)
    for loc in map_data.get("locations") or []:
        desc = (loc.get("description") or "")[:90]
        tags = f" [{loc['tags']}]" if loc.get("tags") else ""
        lines.append(f"- {loc['name']}{tags}: {desc} (exits: {', '.join(nb.get(loc['name']) or []) or 'none'})")
    return "\n".join(lines)
