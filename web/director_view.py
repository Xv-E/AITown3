"""
导演选项卡的数据：把 Storyteller / 冲突 / 任务线整理成玩家看得懂的几段话。
只读，不改世界；GameLoop 没起来（模拟没开过）时也给出世界里已有的任务线和仇怨。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def beat_line(beat: Dict[str, Any]) -> str:
    """一拍剧情 → 一句话。"""
    from narration import pretty

    op = beat.get("op")
    if op == "announce":
        return f"Announced to everyone: {beat.get('text')}"
    if op == "whisper":
        urgent = " (urgent - interrupts their plan)" if beat.get("urgent") else ""
        return f"Whispered to {pretty(beat.get('to'))}{urgent}: {beat.get('text')}"
    if op == "adjust":
        change = f"{beat.get('from')} -> {beat.get('to')}" if "from" in beat else f"by {beat.get('delta')}"
        why = f" - {beat.get('why')}" if beat.get("why") else ""
        return f"Changed {pretty(beat.get('item'))}'s {beat.get('prop')} {change}{why}"
    return str(beat.get("text") or op or "")


def conflict_line(c: Dict[str, Any]) -> str:
    from narration import pretty

    how = f" ({c.get('how')})" if c.get("how") else ""
    return f"{pretty(c.get('culprit'))} got to {pretty(c.get('item'))} before {pretty(c.get('loser'))}{how}"


def _stage_rows(stages: List[Dict[str, Any]], current: int, state: str) -> List[Dict[str, str]]:
    rows = []
    for i, st in enumerate(stages):
        if state == "done" or i < current:
            mark = "done"
        elif i == current and state == "active":
            mark = "current"
        else:
            mark = "todo"
        rows.append({
            "title": str(st.get("title") or st.get("id") or f"Stage {i + 1}"),
            "objective": str(st.get("objective") or ""),
            "giver": str(st.get("giver") or ""),
            "state": mark,
        })
    return rows


def build_director_view(loop: Any, world: Any) -> Dict[str, Any]:
    from routine import DAY_LENGTH_SECONDS, format_clock

    out: Dict[str, Any] = {
        "running": loop is not None,
        "mode": "off",
        "thinking": False,
        "arc": "",
        "next_clock": "",
        "next_in_hours": None,
        "beats": [],
        "conflicts": [],
        "tensions": [],
        "chronicle": [],
        "questlines": [],
    }
    st = getattr(loop, "storyteller", None) if loop is not None else None
    if st is not None:
        llm = not getattr(loop, "force_mock", True) and st.use_llm
        out["mode"] = "llm" if llm else "template"
        out["thinking"] = getattr(loop, "_story_future", None) is not None
        out["arc"] = st.arc
        next_at = st.last_at + st.interval
        out["next_clock"] = format_clock(max(next_at, loop.sim_time))
        out["next_in_hours"] = round(max(0.0, next_at - loop.sim_time) * 24.0 / DAY_LENGTH_SECONDS, 2)
        out["beats"] = [
            {"clock": b.get("clock", ""), "op": b.get("op", ""), "line": beat_line(b)}
            for b in reversed(st.beats[-12:])
        ]
    if loop is not None:
        out["conflicts"] = [
            {"clock": c.get("clock", ""), "line": conflict_line(c)} for c in reversed(loop.conflicts[-8:])
        ]
        out["chronicle"] = [
            {"clock": c.get("clock", ""), "line": str(c.get("text") or "")} for c in reversed(loop.chronicle[-8:])
        ]
    if world is not None:
        try:
            from conflicts import all_tensions
            from narration import pretty

            out["tensions"] = [
                {
                    "line": f"{pretty(t['who'])} -> {pretty(t['toward'])}: level {int(t.get('level') or 0)}"
                    + (f" ({t['reason']})" if t.get("reason") else ""),
                }
                for t in all_tensions(world, 8)
            ]
        except Exception:  # noqa: BLE001
            pass
        try:
            from quest import active_questlines

            for q in active_questlines(world):
                state = str(q.get("state") or "")
                current = int(q.get("current") or 0)
                out["questlines"].append({
                    "title": str(q.get("title") or q.get("name") or ""),
                    "premise": str(q.get("premise") or ""),
                    "state": state,
                    "stages": _stage_rows(q.get("stages") or [], current, state),
                })
        except Exception:  # noqa: BLE001
            pass
    return out


def draft_summary(body: Dict[str, Any]) -> Dict[str, Any]:
    """起草结果 → 选项卡里预览用的几行字。"""
    patch: Optional[Dict[str, Any]] = body.get("patch") or None
    lines: List[str] = []
    if patch:
        lines.append(patch.get("title") or "(untitled)")
        if patch.get("premise"):
            lines.append(patch["premise"])
        new = [x.get("name") for k in ("regions", "persons", "items") for x in patch.get(k) or [] if x.get("name")]
        if new:
            lines.append("Adds: " + ", ".join(new))
        for i, st in enumerate(patch.get("stages") or [], 1):
            giver = f" (from {st.get('giver')})" if st.get("giver") else ""
            lines.append(f"{i}. {st.get('title')}{giver}: {st.get('objective')}")
    for p in body.get("problems") or []:
        lines.append(f"Problem: {p}")
    return {"ok": bool(body.get("ok")), "lines": lines, "attempts": body.get("attempts")}
