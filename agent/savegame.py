"""
存档 / 读档：一个存档 = 一个 JSON 文件（saves/<slot>.json），包括

  world   Neo4j 里的整张图（所有节点 + 关系，原样；:Memory 节点除外，记忆走下面的 memory）
  sim     GameLoop 内存里的进度：时间、每个 NPC 的计划 / 正在做的事 / 触发器 / 历史、
          冲突、大事记、对话、剧情导演
  memory  每个 NPC 的长期记忆（Chroma 或 Neo4j，见 MEMORY_BACKEND；不存向量，读档时重算）

读档 = 清空图 → 原样建回 → 重建 GameLoop 并把进度填回去 → 记忆换成存档里的。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from game_loop import GameLoop

SAVE_VERSION = 1
SAVE_DIR = Path(os.environ.get("AITOWN3_SAVE_DIR", Path(__file__).resolve().parent.parent / "saves"))
AUTOSAVE = "autosave"
_SLOT_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 这些字段是派生 / 运行时对象，读档后会自己重新算出来
_SKIP_STATE_FIELDS = {"agent", "last_action_result", "observation"}


def slot_path(slot: str) -> Path:
    if not _SLOT_RE.match(slot or ""):
        raise ValueError("Save name: letters, digits, - and _ only (max 40)")
    return SAVE_DIR / f"{slot}.json"


# ---------------------------------------------------------------------
# 图
# ---------------------------------------------------------------------

def dump_graph(world) -> Dict[str, Any]:
    try:
        return _dump_graph(world, "elementId")
    except Exception:  # noqa: BLE001  Neo4j 4 没有 elementId()
        return _dump_graph(world, "id")


def _dump_graph(world, id_fn: str) -> Dict[str, Any]:
    nodes = world.run_query(
        f"MATCH (n) WHERE NOT n:Memory "
        f"RETURN toString({id_fn}(n)) AS id, labels(n) AS labels, properties(n) AS props"
    ) or []
    rels = world.run_query(
        f"""
        MATCH (a)-[r]->(b)
        WHERE NOT a:Memory AND NOT b:Memory
        RETURN toString({id_fn}(a)) AS a, toString({id_fn}(b)) AS b, type(r) AS type, properties(r) AS props
        """
    ) or []
    return {
        "nodes": [{"id": n["id"], "labels": list(n["labels"]), "props": dict(n["props"] or {})} for n in nodes],
        "rels": [{"a": r["a"], "b": r["b"], "type": r["type"], "props": dict(r["props"] or {})} for r in rels],
    }


def restore_graph(world, graph: Dict[str, Any]) -> Dict[str, int]:
    """清空数据库，把存档里的图原样建回来。"""
    world.run_query("MATCH (n) DETACH DELETE n")
    try:
        world.run_query("CREATE INDEX save_restore_sid IF NOT EXISTS FOR (n:_Restore) ON (n._sid)")
    except Exception:  # noqa: BLE001
        pass
    by_labels: Dict[tuple, List[Dict[str, Any]]] = {}
    for n in graph.get("nodes") or []:
        labels = tuple(sorted(l for l in n.get("labels") or [] if _LABEL_RE.match(l)))
        by_labels.setdefault(labels, []).append({"sid": n["id"], "props": n.get("props") or {}})
    for labels, rows in by_labels.items():
        label_str = "".join(f":`{l}`" for l in labels + ("_Restore",))
        for chunk in _chunks(rows, 500):
            world.run_query(
                f"UNWIND $rows AS r CREATE (n{label_str}) SET n = r.props, n._sid = r.sid",
                rows=chunk,
            )
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for r in graph.get("rels") or []:
        if _LABEL_RE.match(r.get("type") or ""):
            by_type.setdefault(r["type"], []).append({"a": r["a"], "b": r["b"], "props": r.get("props") or {}})
    for rtype, rows in by_type.items():
        for chunk in _chunks(rows, 500):
            world.run_query(
                f"""
                UNWIND $rows AS r
                MATCH (a:_Restore {{_sid: r.a}}) MATCH (b:_Restore {{_sid: r.b}})
                CREATE (a)-[x:`{rtype}`]->(b) SET x = r.props
                """,
                rows=chunk,
            )
    world.run_query("MATCH (n:_Restore) REMOVE n:_Restore, n._sid")
    return {"nodes": len(graph.get("nodes") or []), "rels": len(graph.get("rels") or [])}


def _chunks(rows: List[Any], size: int):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


# ---------------------------------------------------------------------
# 模拟进度（GameLoop）
# ---------------------------------------------------------------------

def _state_to_dict(state) -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": state.agent.name}
    for f in fields(state):
        if f.name in _SKIP_STATE_FIELDS:
            continue
        value = getattr(state, f.name)
        if f.name == "pending_action" and value is not None:
            value = asdict(value)
        elif f.name == "pending_triggers":
            value = [{**t.to_dict(), "data": dict(getattr(t, "data", {}) or {})} for t in value]
        elif f.name == "current_plan" and value is not None:
            value = [[name, list(args)] for name, args in value]
        out[f.name] = value
    return out


def _apply_state(state, data: Dict[str, Any]) -> None:
    from game_loop import PendingAction
    from triggers import Trigger

    names = {f.name for f in fields(state)} - _SKIP_STATE_FIELDS
    for key, value in data.items():
        if key not in names:
            continue
        if key == "pending_action" and value is not None:
            path = value.get("path")
            value = PendingAction(**{**value, "path": [tuple(c) for c in path] if path else None})
        elif key == "pending_triggers":
            value = [
                Trigger(t.get("source", "event"), t.get("kind", ""), t.get("text", ""),
                        float(t.get("priority", 0.5)), float(t.get("at", 0.0)), dict(t.get("data") or {}))
                for t in value or []
            ]
        elif key == "current_plan" and value is not None:
            value = [(name, list(args)) for name, args in value]
        setattr(state, key, value)


def loop_snapshot(loop: "GameLoop") -> Dict[str, Any]:
    st = loop.storyteller
    return {
        "sim_time": loop.sim_time,
        "tick_count": loop.tick_count,
        "chronicle": loop.chronicle,
        "conflicts": loop.conflicts,
        "conversations": loop.conversations,
        "conversation_seq": getattr(loop, "_conversation_seq", 0),
        "touched": {k: list(v) for k, v in loop._touched.items()},  # noqa: SLF001
        "storyteller": None if st is None else {
            "arc": st.arc, "beats": st.beats, "last_at": st.last_at,
            "template_turn": getattr(st, "_template_turn", 0),
            "threads": getattr(st, "threads", []),
            "last_questline_at": getattr(st, "last_questline_at", -1e9),
        },
        "agents": [_state_to_dict(s) for s in loop.agents],
    }


def restore_loop(loop: "GameLoop", snap: Dict[str, Any]) -> None:
    loop.sim_time = float(snap.get("sim_time") or 0.0)
    loop.tick_count = loop.turn_count = int(snap.get("tick_count") or 0)
    loop.chronicle = list(snap.get("chronicle") or [])
    loop.conflicts = list(snap.get("conflicts") or [])
    loop.conversations = list(snap.get("conversations") or [])
    loop._conversation_seq = int(snap.get("conversation_seq") or 0)  # noqa: SLF001
    loop._touched = {k: tuple(v) for k, v in (snap.get("touched") or {}).items()}  # noqa: SLF001
    st, saved = loop.storyteller, snap.get("storyteller")
    if st is not None and saved:
        st.arc = saved.get("arc") or ""
        st.beats = list(saved.get("beats") or [])
        st.last_at = float(saved.get("last_at") or 0.0)
        st._template_turn = int(saved.get("template_turn") or 0)  # noqa: SLF001
        st.threads = [
            # 存档时还在写的任务线不会接着写：让那条线索之后重新有机会
            dict(t, questline="" if t.get("questline") == "(being written)" else t.get("questline", ""))
            for t in saved.get("threads") or [] if isinstance(t, dict)
        ]
        st.last_questline_at = float(saved.get("last_questline_at", -1e9))
    for data in snap.get("agents") or []:
        state = loop.get_agent_state(data.get("name"))
        if state is not None:
            _apply_state(state, data)


# ---------------------------------------------------------------------
# NPC 记忆
# ---------------------------------------------------------------------

def dump_memory(memory) -> Dict[str, List[Dict[str, Any]]]:
    if memory is None:
        return {}
    out = {}
    for npc in memory.list_npcs():
        out[npc] = [{"id": m["id"], "text": m["text"], "metadata": m.get("metadata") or {}} for m in memory.list(npc)]
    return out


def restore_memory(memory, saved: Dict[str, List[Dict[str, Any]]]) -> int:
    if memory is None:
        return 0
    memory.clear_all()
    n = 0
    for npc, items in (saved or {}).items():
        for m in items:
            meta = {k: v for k, v in (m.get("metadata") or {}).items() if k != "owner"}
            memory.add(npc, m["text"], mem_id=m.get("id"), **meta)
            n += 1
    return n


# ---------------------------------------------------------------------
# 存 / 读 / 列表
# ---------------------------------------------------------------------

def _player_and_clock(graph: Dict[str, Any], sim_time: float) -> Dict[str, str]:
    from routine import format_clock

    player = next(
        (n["props"].get("name") for n in graph["nodes"]
         if "Person" in n["labels"] and (n["props"].get("is_player") or n["props"].get("role") == "player")),
        "",
    )
    return {"player": player or "", "clock": format_clock(sim_time)}


def stamp_world(world, token: str, slot: str) -> None:
    """在图里记下「现在的世界 = 哪个存档」：重启后据此判断能不能接着这个存档的进度继续。"""
    world.run_query(
        "MERGE (s:SaveStamp {id: 'current'}) SET s.token = $t, s.slot = $slot",
        t=token, slot=slot,
    )


def world_stamp(world) -> str:
    rows = world.run_query("MATCH (s:SaveStamp {id: 'current'}) RETURN s.token AS t LIMIT 1") or []
    return str(rows[0].get("t") or "") if rows else ""


def save_game(slot: str, world, loop: Optional["GameLoop"], memory=None, *, label: str = "") -> Dict[str, Any]:
    path = slot_path(slot)
    token = uuid.uuid4().hex
    stamp_world(world, token, slot)  # 先盖章再导出：存档里的图也带着这个章
    graph = dump_graph(world)
    sim = loop_snapshot(loop) if loop is not None else None
    meta = {
        "slot": slot,
        "label": label or slot,
        "version": SAVE_VERSION,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        **_player_and_clock(graph, sim["sim_time"] if sim else 0.0),
        "nodes": len(graph["nodes"]),
        "token": token,
    }
    body = {"meta": meta, "world": graph, "sim": sim, "memory": dump_memory(memory)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)  # 写完再换，存到一半崩了也不会坏掉旧存档
    return meta


def read_save(slot: str) -> Dict[str, Any]:
    path = slot_path(slot)
    if not path.exists():
        raise FileNotFoundError(f"No save called {slot}")
    body = json.loads(path.read_text(encoding="utf-8"))
    if int((body.get("meta") or {}).get("version") or 0) > SAVE_VERSION:
        raise ValueError("This save was made by a newer version")
    return body


def matching_save(world) -> Optional[Dict[str, Any]]:
    """数据库里的世界正是某个存档存下（或读进来）的那个：返回那个存档（没有就 None）。"""
    token = world_stamp(world)
    if not token:
        return None
    for meta in list_saves():
        if meta.get("token") == token:
            body = read_save(meta["slot"])
            return body if body.get("sim") else None
    return None


def list_saves() -> List[Dict[str, Any]]:
    out = []
    if not SAVE_DIR.exists():
        return out
    for path in sorted(SAVE_DIR.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                # meta 在最前面：只读开头一段，不把整张图读进来
                head = f.read(4096)
            meta = json.loads(head[head.index("{", 1):head.index("}") + 1]) if '"meta"' in head else {}
        except Exception:  # noqa: BLE001
            meta = {}
        out.append({"slot": path.stem, **meta, "size_kb": round(path.stat().st_size / 1024)})
    out.sort(key=lambda m: m.get("saved_at") or "", reverse=True)
    return out
