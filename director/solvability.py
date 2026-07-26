"""
蓝图可解性门闩 — 在写入 Neo4j 前用静态状态搜索证明存在通关路径。

不做完整对话/信任推理；覆盖导演主流谜题：
  key_door / 门钥 use_on、deliver（拿到物并走到目标人）、
  use_on_person、talk_info（走到掌握隐藏事实的 NPC）。

状态：玩家位置 + 背包 + 已解锁门集合 + 已启用的连通边。
动作：移动（开图）、拾取地面物、从可达 NPC 处取得其持有物（简化为交谈/获取）、
      在目标同地执行已编译的 tool→target 交互（应用 link/set_prop）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .blueprint import EventBlueprint, ItemSpec


State = Tuple[str, frozenset, frozenset, frozenset]
# (player_loc, inventory, unlocked_doors, extra_edges as "A|B")


@dataclass
class SolvabilityResult:
    ok: bool
    reason: str = ""
    puzzle_kind: str = ""
    win_goals: List[str] = field(default_factory=list)
    plan_sketch: List[str] = field(default_factory=list)
    steps: int = 0


def _player(bp: EventBlueprint) -> Optional[str]:
    for p in bp.persons:
        if (p.role or "").lower() == "player":
            return p.name
    return None


def _player_start(bp: EventBlueprint) -> Optional[str]:
    for p in bp.persons:
        if (p.role or "").lower() == "player":
            return p.located_at
    return None


def _item_by_name(bp: EventBlueprint) -> Dict[str, ItemSpec]:
    return {i.name: i for i in bp.items}


def _person_loc(bp: EventBlueprint) -> Dict[str, str]:
    return {p.name: p.located_at for p in bp.persons}


def _open_adj(bp: EventBlueprint) -> Dict[str, Set[str]]:
    adj: Dict[str, Set[str]] = {x.name: set() for x in bp.locations}
    for e in bp.adjacencies:
        if e.locked:
            continue
        if e.a in adj and e.b in adj:
            adj[e.a].add(e.b)
            adj[e.b].add(e.a)
    return adj


def _edge_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def _compiled_interactions(bp: EventBlueprint) -> List[Dict[str, Any]]:
    """与 world_interactions.compile 同规则的轻量版（不依赖 actions 路径）。"""
    by_item = _item_by_name(bp)
    by_person = {p.name: p for p in bp.persons}
    out: List[Dict[str, Any]] = []
    seen = set()
    for tool in bp.items:
        for raw in tool.effects_on or []:
            if not isinstance(raw, dict):
                continue
            target = raw.get("target") or ""
            if not target:
                continue
            accepts: List[str] = []
            if target in by_item:
                ti = by_item[target]
                accepts = list(ti.accepts_from or [])
                if not accepts and ti.key_item:
                    accepts = [ti.key_item]
            elif target in by_person:
                accepts = list(by_person[target].accepts_from or [])
            else:
                continue
            if tool.name not in accepts:
                continue
            key = f"{tool.name}|{target}"
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "tool": tool.name,
                    "target": target,
                    "ops": list(raw.get("ops") or []),
                    "consume": raw.get("consume"),
                }
            )
    return out


def _infer_puzzle_kind(bp: EventBlueprint) -> str:
    doors = [i for i in bp.items if (i.item_kind or "").lower() == "door"]
    inters = _compiled_interactions(bp)
    person_targets = {
        x["target"] for x in inters if x["target"] in {p.name for p in bp.persons}
    }
    if doors or any(
        (i.item_kind or "").lower() == "key" or "Door" in i.name for i in bp.items
    ):
        if any(x["target"] in {d.name for d in doors} for x in inters) or doors:
            return "key_door"
    if person_targets:
        return "use_on_person"
    player = _player(bp)
    for f in bp.facts:
        known = set(f.known_by or [])
        if known and player and player not in known:
            return "talk_info"
    mac = [
        i for i in bp.items
        if (i.quest_flag or "").lower() == "main_macguffin"
    ]
    if mac and any((p.role or "").lower() != "player" for p in bp.persons):
        return "deliver"
    return "custom"


def _item_world_loc(
    bp: EventBlueprint,
    item: ItemSpec,
    person_loc: Dict[str, str],
) -> Optional[str]:
    if item.held_by and item.held_by in person_loc:
        return person_loc[item.held_by]
    return item.located_at


def _apply_ops_to_graph(
    ops: List[Any],
    *,
    unlocked: Set[str],
    extra_edges: Set[str],
    target: str,
) -> Tuple[Set[str], Set[str]]:
    unlocked = set(unlocked)
    extra_edges = set(extra_edges)
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "set_prop" and op.get("prop") == "locked" and op.get("value") is False:
            ent = str(op.get("entity") or target)
            unlocked.add(ent)
        elif kind == "link" and op.get("rel") == "ADJACENT_TO":
            a, b = str(op.get("a") or ""), str(op.get("b") or "")
            if a and b and a != b:
                extra_edges.add(_edge_key(a, b))
    return unlocked, extra_edges


def _neighbors(
    loc: str,
    base_adj: Dict[str, Set[str]],
    extra_edges: frozenset,
) -> Set[str]:
    out = set(base_adj.get(loc, ()))
    for ek in extra_edges:
        a, b = ek.split("|", 1)
        if a == loc:
            out.add(b)
        elif b == loc:
            out.add(a)
    return out


def _win_conditions(
    bp: EventBlueprint,
    puzzle_kind: str,
) -> List[str]:
    """人读目标列表；搜索用的具体谓词在 check 内实现。"""
    goals: List[str] = []
    doors = [i for i in bp.items if (i.item_kind or "").lower() == "door"]
    inters = _compiled_interactions(bp)
    goal_locs = [
        x.name for x in bp.locations
        if "goal" in (x.tags or "").lower() or "secret" in (x.tags or "").lower()
    ]
    if puzzle_kind == "key_door":
        for d in doors:
            tools = [x["tool"] for x in inters if x["target"] == d.name]
            if tools:
                goals.append(f"Use {tools[0]} on {d.name}")
            else:
                goals.append(f"Unlock {d.name}")
        for g in goal_locs:
            goals.append(f"Reach {g}")
        if not goals and bp.quests:
            goals.append(bp.quests[0].objective or bp.quests[0].name)
        return goals
    if puzzle_kind == "use_on_person":
        for x in inters:
            if x["target"] in {p.name for p in bp.persons}:
                goals.append(f"Use {x['tool']} on {x['target']}")
        return goals or ["Complete person interaction"]
    if puzzle_kind == "talk_info":
        player = _player(bp)
        for f in bp.facts:
            known = [n for n in (f.known_by or []) if n != player]
            if known:
                goals.append(f"Talk to {known[0]} about {f.name}")
                break
        if not goals:
            npcs = [p.name for p in bp.persons if (p.role or "").lower() != "player"]
            if npcs:
                goals.append(f"Talk to {npcs[0]}")
        return goals
    if puzzle_kind == "deliver":
        mac = next(
            (i for i in bp.items if (i.quest_flag or "").lower() == "main_macguffin"),
            bp.items[0] if bp.items else None,
        )
        giver = None
        if bp.quests and bp.quests[0].giver:
            giver = bp.quests[0].giver
        if not giver:
            giver = next(
                (p.name for p in bp.persons if (p.role or "").lower() == "npc"),
                None,
            )
        if mac and giver:
            goals.append(f"Deliver {mac.name} to {giver}")
        elif mac:
            goals.append(f"Collect {mac.name}")
        return goals
    if bp.quests:
        goals.append(bp.quests[0].objective or bp.quests[0].name)
    return goals or ["Reach any goal-tagged location or unlock main door"]


def _is_winning(
    bp: EventBlueprint,
    puzzle_kind: str,
    *,
    loc: str,
    inv: frozenset,
    unlocked: frozenset,
    extra_edges: frozenset,
    base_adj: Dict[str, Set[str]],
) -> bool:
    doors = {i.name for i in bp.items if (i.item_kind or "").lower() == "door"}
    goal_locs = [
        x.name for x in bp.locations
        if "goal" in (x.tags or "").lower() or "secret" in (x.tags or "").lower()
    ]
    inters = _compiled_interactions(bp)
    person_names = {p.name for p in bp.persons}

    if puzzle_kind == "key_door":
        # 所有门解锁，且能走到目标地（若有）
        if doors and not doors.issubset(unlocked):
            return False
        if goal_locs:
            # 从当前位置在当前图上可达任一 goal
            seen = {loc}
            q = deque([loc])
            while q:
                cur = q.popleft()
                if cur in goal_locs:
                    return True
                for nxt in _neighbors(cur, base_adj, extra_edges):
                    if nxt not in seen:
                        seen.add(nxt)
                        q.append(nxt)
            return False
        return bool(doors) and doors.issubset(unlocked)

    if puzzle_kind == "use_on_person":
        # 简化：任一对人交互的 tool 已不在需要（consume）或我们记录“已用过”
        # 用：存在交互且 tool 曾可持有、目标人可达——搜索里用副作用标记更好
        # 这里：若交互 consume=true，tool 消失且我们把 target 记入 unlocked 集合的命名空间
        for x in inters:
            if x["target"] in person_names and f"used:{x['tool']}|{x['target']}" in unlocked:
                return True
        return False

    if puzzle_kind == "talk_info":
        player = _player(bp)
        for f in bp.facts:
            known = set(f.known_by or [])
            if not known or (player and player in known):
                continue
            for npc in known:
                if npc in person_names and f"talked:{npc}" in unlocked:
                    return True
        # 若无隐藏事实，走到任一 NPC 即可
        npcs = [p for p in bp.persons if (p.role or "").lower() != "player"]
        return any(f"talked:{p.name}" in unlocked for p in npcs)

    if puzzle_kind == "deliver":
        mac = next(
            (i for i in bp.items if (i.quest_flag or "").lower() == "main_macguffin"),
            None,
        )
        if not mac:
            return False
        if mac.name not in inv:
            return False
        giver = bp.quests[0].giver if bp.quests else None
        if not giver:
            giver = next(
                (p.name for p in bp.persons if (p.role or "").lower() == "npc"),
                None,
            )
        if not giver:
            return True  # 拿到即胜
        pl = _person_loc(bp).get(giver)
        return pl == loc

    # custom：门全开或到达 goal 地或持有 macguffin
    if doors and doors.issubset(unlocked):
        return True
    if goal_locs and loc in goal_locs:
        return True
    mac = next(
        (i for i in bp.items if (i.quest_flag or "").lower() == "main_macguffin"),
        None,
    )
    if mac and mac.name in inv:
        return True
    return False


def check_solvable(
    bp: EventBlueprint,
    *,
    puzzle_kind: Optional[str] = None,
    max_states: int = 8000,
) -> SolvabilityResult:
    """
    静态证明蓝图可解。失败时 reason 说明卡点。
    """
    kind = (puzzle_kind or "").strip().lower() or _infer_puzzle_kind(bp)
    win_goals = _win_conditions(bp, kind)

    if not bp.locations:
        return SolvabilityResult(False, "no_locations", kind, win_goals)
    start = _player_start(bp)
    if not start:
        return SolvabilityResult(False, "no_player_start", kind, win_goals)

    base_adj = _open_adj(bp)
    person_loc = _person_loc(bp)
    items = list(bp.items)
    inters = _compiled_interactions(bp)
    by_item = _item_by_name(bp)

    # 初始背包：玩家已持有
    player = _player(bp)
    init_inv = frozenset(
        i.name for i in items if i.held_by and i.held_by == player
    )

    init: State = (start, init_inv, frozenset(), frozenset())
    if _is_winning(
        bp, kind, loc=start, inv=init_inv, unlocked=frozenset(),
        extra_edges=frozenset(), base_adj=base_adj,
    ):
        return SolvabilityResult(
            True, "already_satisfied", kind, win_goals, ["noop"], 0
        )

    # key_door 硬前置：必须有可编译的门交互
    if kind == "key_door":
        doors = [i for i in items if (i.item_kind or "").lower() == "door"]
        if doors and not any(x["target"] == d.name for d in doors for x in inters):
            return SolvabilityResult(
                False, "door_has_no_use_on", kind, win_goals
            )

    q: deque = deque([init])
    parent: Dict[State, Tuple[Optional[State], str]] = {init: (None, "start")}
    sketch_at_win: List[str] = []

    def _record(nxt: State, prev: State, action: str) -> bool:
        if nxt in parent:
            return False
        parent[nxt] = (prev, action)
        q.append(nxt)
        return True

    visited = 0
    while q and visited < max_states:
        cur = q.popleft()
        visited += 1
        loc, inv, unlocked, extra = cur

        if _is_winning(
            bp, kind, loc=loc, inv=inv, unlocked=unlocked,
            extra_edges=extra, base_adj=base_adj,
        ):
            # 回溯草图
            sketch: List[str] = []
            s: Optional[State] = cur
            while s is not None:
                prev, act = parent[s]
                if prev is None:
                    break
                sketch.append(act)
                s = prev
            sketch.reverse()
            sketch_at_win = sketch
            return SolvabilityResult(
                True, "ok", kind, win_goals, sketch_at_win, len(sketch_at_win)
            )

        # move
        for nxt_loc in _neighbors(loc, base_adj, extra):
            nxt = (nxt_loc, inv, unlocked, extra)
            _record(nxt, cur, f"move:{loc}->{nxt_loc}")

        # pickup ground items here
        for it in items:
            if it.name in inv:
                continue
            if it.held_by:
                continue
            if (it.item_kind or "").lower() == "door":
                continue
            if it.portable is False:
                continue
            if it.located_at != loc:
                continue
            _record(
                (loc, frozenset(set(inv) | {it.name}), unlocked, extra),
                cur,
                f"pickup:{it.name}",
            )

        # obtain item held by NPC at this location (simplified fetch/steal/talk)
        for it in items:
            if it.name in inv or not it.held_by:
                continue
            if it.held_by == player:
                continue
            if person_loc.get(it.held_by) != loc:
                continue
            if (it.item_kind or "").lower() == "door" or it.portable is False:
                continue
            _record(
                (loc, frozenset(set(inv) | {it.name}), unlocked, extra),
                cur,
                f"obtain:{it.name}@from:{it.held_by}",
            )

        # talk to NPC here (talk_info)
        if kind in ("talk_info", "custom"):
            for p in bp.persons:
                if (p.role or "").lower() == "player":
                    continue
                if p.located_at != loc:
                    continue
                mark = f"talked:{p.name}"
                if mark in unlocked:
                    continue
                _record(
                    (loc, inv, frozenset(set(unlocked) | {mark}), extra),
                    cur,
                    f"talk:{p.name}",
                )

        # use_on interactions
        for inter in inters:
            tool, target = inter["tool"], inter["target"]
            if tool not in inv:
                continue
            # target must be co-located
            t_loc: Optional[str] = None
            if target in by_item:
                ti = by_item[target]
                if ti.held_by and ti.held_by in person_loc:
                    t_loc = person_loc[ti.held_by]
                else:
                    t_loc = ti.located_at
            elif target in person_loc:
                t_loc = person_loc[target]
            if t_loc != loc:
                continue

            new_unlocked, new_extra = _apply_ops_to_graph(
                inter.get("ops") or [],
                unlocked=set(unlocked),
                extra_edges=set(extra),
                target=target,
            )
            # door unlock without explicit set_prop
            if target in by_item and (by_item[target].item_kind or "").lower() == "door":
                new_unlocked.add(target)
                conn = by_item[target].connects_to or ""
                door_at = by_item[target].located_at or loc
                if conn and door_at:
                    new_extra.add(_edge_key(door_at, conn))

            new_inv = set(inv)
            if inter.get("consume") is True:
                new_inv.discard(tool)
            # mark person use
            if target in person_loc:
                new_unlocked.add(f"used:{tool}|{target}")

            nxt = (
                loc,
                frozenset(new_inv),
                frozenset(new_unlocked),
                frozenset(new_extra),
            )
            _record(nxt, cur, f"use_on:{tool}->{target}")

    # 诊断
    reason = "no_solution"
    if kind == "key_door":
        # 钥匙是否在开图可达处
        keys = [
            i for i in items
            if (i.item_kind or "").lower() == "key"
            or any(x["tool"] == i.name for x in inters)
        ]
        reach = set()
        dq = deque([start])
        reach.add(start)
        while dq:
            c = dq.popleft()
            for n in base_adj.get(c, ()):
                if n not in reach:
                    reach.add(n)
                    dq.append(n)
        for k in keys:
            kl = _item_world_loc(bp, k, person_loc)
            if kl and kl not in reach:
                reason = f"key_unreachable:{k.name}@{kl}"
                break
        else:
            if not inters:
                reason = "no_interactions"
            elif not any(
                (i.item_kind or "").lower() == "door" for i in items
            ):
                reason = "no_door"
            else:
                reason = "cannot_unlock_or_reach_goal"
    elif kind == "talk_info":
        reason = "cannot_reach_informant"
    elif kind == "deliver":
        reason = "cannot_collect_or_deliver"
    elif kind == "use_on_person":
        reason = "cannot_complete_use_on_person"

    return SolvabilityResult(
        False, reason, kind, win_goals, [], visited
    )


def assert_solvable(
    bp: EventBlueprint,
    *,
    puzzle_kind: Optional[str] = None,
) -> SolvabilityResult:
    """供规划器调用：不可解则 ok=False。"""
    return check_solvable(bp, puzzle_kind=puzzle_kind)
