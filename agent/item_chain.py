"""
通用获取链规划 — 从世界交互表（effects_on × accepts_from）反推「怎么得到 / 用上某物」。

不含任何具体玩法：炉子、货架、点单板都只是世界数据。
  - 想对 target 使用东西：找能作用于 target 的工具；没有就按 accepts_from 的 kind 去获取。
  - 想得到某 kind：先看手上 → 再看地上 → 再找「哪条交互会 spawn_item 该 kind 给 @actor」递归求解。
计划里尚未出生的物品用占位符 `kind:<kind>`，执行时再绑定到手上同类物品。
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from registry import act

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

PlanStep = Tuple[str, List[str]]
KIND_PREFIX = "kind:"
MAX_DEPTH = 3


def kind_ref(kind: str) -> str:
    return f"{KIND_PREFIX}{kind}"


def parse_kind_ref(ref: str) -> Optional[str]:
    if isinstance(ref, str) and ref.startswith(KIND_PREFIX):
        return ref[len(KIND_PREFIX):].strip().lower() or None
    return None


@dataclass
class ItemInfo:
    name: str
    kind: str = ""
    portable: bool = True
    location: Optional[str] = None
    holder: Optional[str] = None
    props: Dict[str, Any] = field(default_factory=dict)

    def accepts(self) -> List[str]:
        raw = self.props.get("accepts_from")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = []
        return [str(x).strip().lower() for x in (raw or []) if x]


@dataclass
class Recipe:
    tool: str
    target: str
    preconditions: List[Dict[str, Any]] = field(default_factory=list)
    produces: List[str] = field(default_factory=list)
    consumes: bool = False


@dataclass
class WorldView:
    adjacency: Dict[str, List[str]] = field(default_factory=dict)
    items: Dict[str, ItemInfo] = field(default_factory=dict)
    person_loc: Dict[str, str] = field(default_factory=dict)
    recipes: List[Recipe] = field(default_factory=list)

    def kind_of(self, name: str) -> str:
        info = self.items.get(name)
        return info.kind if info else (parse_kind_ref(name) or "")

    def location_of(self, entity: str) -> Optional[str]:
        if entity in self.person_loc:
            return self.person_loc[entity]
        info = self.items.get(entity)
        if not info:
            return None
        if info.location:
            return info.location
        if info.holder:
            return self.person_loc.get(info.holder)
        return None

    def producers_of(self, kind: str) -> List[Recipe]:
        k = kind.lower()
        return [r for r in self.recipes if k in r.produces]


def recipe_from_interaction(inter: Any) -> Recipe:
    produces: List[str] = []
    for op in getattr(inter, "ops", None) or []:
        if op.get("op") != "spawn_item":
            continue
        if str(op.get("held_by") or "") != "@actor":
            continue
        if op.get("archetype"):
            from content import apply_archetype

            op = apply_archetype(dict(op))
        kind = str(op.get("item_kind") or "").strip().lower()
        if kind:
            produces.append(kind)
    return Recipe(
        tool=inter.tool,
        target=inter.target,
        preconditions=list(getattr(inter, "preconditions", None) or []),
        produces=produces,
        consumes=bool(getattr(inter, "consumes_tool", False)),
    )


def load_world_view(world: "Neo4jWorld", viewer: Optional[str] = None) -> WorldView:
    """viewer 给定时只保留他知道的地点 / 物品（knowledge.py）。"""
    view = _load_true_view(world)
    if viewer:
        from knowledge import filter_view, load_knowledge

        view = filter_view(view, viewer, load_knowledge(world, viewer))
    return view


def _load_true_view(world: "Neo4jWorld") -> WorldView:
    view = WorldView()
    for r in world.run_query(
        "MATCH (a:Location)-[:ADJACENT_TO]->(b:Location) RETURN a.name AS a, b.name AS b"
    ) or []:
        if r.get("a") and r.get("b"):
            view.adjacency.setdefault(r["a"], []).append(r["b"])
    for r in world.run_query(
        """
        MATCH (p:Person)-[:LOCATED_AT]->(l:Location)
        RETURN p.name AS name, l.name AS loc
        """
    ) or []:
        if r.get("name"):
            view.person_loc[r["name"]] = r.get("loc")
    for r in world.run_query(
        """
        MATCH (i:Item)
        OPTIONAL MATCH (i)-[:LOCATED_AT]->(l:Location)
        OPTIONAL MATCH (p:Person)-[:HAS_ITEM]->(i)
        RETURN i.name AS name, coalesce(i.item_kind, '') AS kind,
               coalesce(i.portable, true) AS portable,
               l.name AS loc, p.name AS holder, properties(i) AS props
        """
    ) or []:
        name = r.get("name")
        if not name:
            continue
        view.items[name] = ItemInfo(
            name=name,
            kind=str(r.get("kind") or "").lower(),
            portable=bool(r.get("portable")),
            location=r.get("loc"),
            holder=r.get("holder"),
            props=dict(r.get("props") or {}),
        )
    try:
        from world_interactions import list_interactions

        view.recipes = [recipe_from_interaction(it) for it in list_interactions()]
    except Exception:
        view.recipes = []
    return view


def _prop_value(view: WorldView, entity: str, prop: str) -> Any:
    info = view.items.get(entity)
    return info.props.get(prop) if info else None


def preconditions_hold(view: WorldView, recipe: Recipe, actor: str) -> bool:
    """规划期只评估数值 / 属性条件；位置、持有类条件交给执行期 check。"""
    ctx = {"@actor": actor, "@tool": recipe.tool, "@target": recipe.target}
    for cond in recipe.preconditions:
        op = str(cond.get("op") or "")
        if op not in ("prop_gte", "prop_eq", "prop_neq"):
            continue
        entity = ctx.get(str(cond.get("entity")), str(cond.get("entity")))
        got = _prop_value(view, entity, str(cond.get("prop") or ""))
        want = cond.get("value")
        if op == "prop_gte":
            try:
                if float(got if got is not None else 0) < float(want or 0):
                    return False
            except (TypeError, ValueError):
                return False
        elif op == "prop_eq" and got != want:
            return False
        elif op == "prop_neq" and got == want:
            return False
    return True


class ChainPlanner:
    """在 WorldView 快照上模拟「走路 / 拿起 / 使用」，产出可执行步骤。"""

    def __init__(self, view: WorldView, character: str, *, max_depth: int = MAX_DEPTH):
        self.view = view
        self.character = character
        self.max_depth = max_depth
        self.loc = view.person_loc.get(character)
        self.held: List[Tuple[str, str]] = [
            (n, i.kind) for n, i in view.items.items() if i.holder == character
        ]
        self.claimed: set = set()
        self.steps: List[PlanStep] = []
        self.error = ""

    # ----- 状态快照（递归失败时回滚）-----
    def _save(self):
        return (len(self.steps), self.loc, list(self.held), set(self.claimed))

    def _restore(self, snap) -> None:
        n, loc, held, claimed = snap
        del self.steps[n:]
        self.loc, self.held, self.claimed = loc, held, claimed

    # ----- 寻路 -----
    def _route(self, dest: str) -> Optional[List[str]]:
        if not self.loc or not dest:
            return None
        if self.loc == dest:
            return [self.loc]
        prev: Dict[str, Optional[str]] = {self.loc: None}
        q = deque([self.loc])
        while q:
            cur = q.popleft()
            for nxt in self.view.adjacency.get(cur, []):
                if nxt in prev:
                    continue
                prev[nxt] = cur
                if nxt == dest:
                    path = [dest]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])  # type: ignore[arg-type]
                    return list(reversed(path))
                q.append(nxt)
        return None

    def distance(self, dest: Optional[str]) -> int:
        route = self._route(dest) if dest else None
        return len(route) - 1 if route else 10_000

    def go(self, dest: str) -> bool:
        route = self._route(dest)
        if route is None:
            self.error = f"No route from {self.loc} to {dest}"
            return False
        for a, b in zip(route, route[1:]):
            self.steps.append((act("travel"), [self.character, a, b]))
        self.loc = dest
        return True

    # ----- 持有 -----
    def _held_ref(self, *, name: str = "", kind: str = "") -> Optional[str]:
        for n, k in self.held:
            if n in self.claimed:
                continue
            if name and n == name:
                return n
            if kind and k == kind:
                return n
        return None

    def _drop_held(self, ref: str) -> None:
        for idx, (n, _k) in enumerate(self.held):
            if n == ref:
                del self.held[idx]
                return

    def obtain_item(self, name: str) -> Optional[str]:
        if self._held_ref(name=name):
            return name
        info = self.view.items.get(name)
        if info is None or info.holder or not info.portable or not info.location:
            self.error = f"{name} cannot be picked up"
            return None
        if name in self.claimed:
            return None
        if not self.go(info.location):
            return None
        self.steps.append((act("take"), [self.character, name, info.location]))
        self.held.append((name, info.kind))
        return name

    def obtain_kind(self, kind: str, depth: int, *, allow_craft: bool = True) -> Optional[str]:
        kind = kind.lower()
        held = self._held_ref(kind=kind)
        if held:
            return held
        ground = [
            i for i in self.view.items.values()
            if i.kind == kind and i.portable and i.location and not i.holder
            and i.name not in self.claimed
        ]
        ground.sort(key=lambda i: (self.distance(i.location), i.name))
        for info in ground:
            snap = self._save()
            got = self.obtain_item(info.name)
            if got:
                return got
            self._restore(snap)
        if not allow_craft or depth <= 0:
            self.error = f"No {kind} available"
            return None
        for recipe in self.view.producers_of(kind):
            if not preconditions_hold(self.view, recipe, self.character):
                continue
            snap = self._save()
            if self.use_on(recipe.tool, recipe.target, depth - 1):
                return self._held_ref(kind=kind)
            self._restore(snap)
        self.error = f"No way to obtain {kind}"
        return None

    def _resolve_tool(self, ref: str, depth: int) -> Optional[str]:
        kind = parse_kind_ref(ref)
        if kind:
            return self.obtain_kind(kind, depth)
        return self.obtain_item(ref)

    def use_on(self, tool_ref: str, target: str, depth: int) -> bool:
        tool = self._resolve_tool(tool_ref, depth)
        if not tool:
            return False
        recipe = next(
            (r for r in self.view.recipes if r.tool == tool and r.target == target), None
        )
        if recipe and not preconditions_hold(self.view, recipe, self.character):
            self.error = f"{tool} cannot be used on {target} right now"
            return False
        self.claimed.add(tool)
        t_loc = self.view.location_of(target)
        if not t_loc or not self.go(t_loc):
            self.error = self.error or f"Cannot reach {target}"
            return False
        self.steps.append(("use_on", [self.character, tool, target, t_loc, t_loc]))
        consumes = recipe.consumes if recipe else True
        if consumes:
            self._drop_held(tool)
        for kind in (recipe.produces if recipe else []):
            ref = kind_ref(kind)
            self.held.append((ref, kind))
        self.claimed.discard(tool)
        return True

    def use_self(self, ref: str, *, craft: bool = False) -> bool:
        kind = parse_kind_ref(ref)
        item = (
            self.obtain_kind(kind, self.max_depth if craft else 0, allow_craft=craft)
            if kind else self.obtain_item(ref)
        )
        if not item:
            return False
        self.steps.append((act("consume"), [self.character, item]))
        self._drop_held(item)
        return True


def tool_options(view: WorldView, target: str, prefer_kind: str = "") -> List[str]:
    """能作用于 target 的工具引用：具体工具名 + 目标 accepts_from 里的 kind 占位。"""
    prefer = (prefer_kind or "").strip().lower()
    out: List[str] = []
    for r in view.recipes:
        if r.target != target:
            continue
        if prefer and view.kind_of(r.tool) != prefer:
            continue
        if r.tool not in out:
            out.append(r.tool)
    info = view.items.get(target)
    accepted_kinds = [
        a[len(KIND_PREFIX):] for a in (info.accepts() if info else []) if a.startswith(KIND_PREFIX)
    ]
    if prefer and prefer in accepted_kinds:
        out.append(kind_ref(prefer))
    elif not prefer:
        out.extend(kind_ref(k) for k in accepted_kinds)
    return out


def plan_use_on(
    view: WorldView,
    character: str,
    target: str,
    *,
    prefer_kind: str = "",
    max_depth: int = MAX_DEPTH,
) -> Tuple[Optional[List[PlanStep]], str]:
    options = tool_options(view, target, prefer_kind)
    if not options:
        return None, f"Nothing can be used on {target}"
    held = {n for n, i in view.items.items() if i.holder == character}

    def _rank(ref: str) -> Tuple[int, int]:
        if ref in held:
            return (0, 0)
        info = view.items.get(ref)
        if info is not None:
            if info.holder:
                return (9, 0)
            planner = ChainPlanner(view, character)
            return (1, planner.distance(info.location))
        return (2, 0)

    last_error = ""
    for ref in sorted(options, key=_rank):
        recipe = next((r for r in view.recipes if r.tool == ref and r.target == target), None)
        if recipe and not preconditions_hold(view, recipe, character):
            last_error = f"{ref} cannot be used on {target} right now"
            continue
        planner = ChainPlanner(view, character, max_depth=max_depth)
        if planner.use_on(ref, target, max_depth):
            return planner.steps, ""
        last_error = planner.error or last_error
    return None, last_error or f"No usable tool for {target}"


def plan_obtain(
    view: WorldView,
    character: str,
    ref: str,
    *,
    max_depth: int = MAX_DEPTH,
) -> Tuple[Optional[List[PlanStep]], str]:
    planner = ChainPlanner(view, character, max_depth=max_depth)
    got = planner._resolve_tool(ref, max_depth)
    if not got:
        return None, planner.error or f"Cannot obtain {ref}"
    return planner.steps, ""


def plan_use_self(
    view: WorldView,
    character: str,
    ref: str,
    *,
    craft: bool = False,
) -> Tuple[Optional[List[PlanStep]], str]:
    planner = ChainPlanner(view, character)
    if not planner.use_self(ref, craft=craft):
        return None, planner.error or f"Cannot use {ref}"
    return planner.steps, ""


def resolve_step_args(world: "Neo4jWorld", actor: str, args: List[str]) -> List[str]:
    """执行期把 `kind:x` 占位绑定到 actor 手上的同类物品。"""
    out: List[str] = []
    for a in args:
        kind = parse_kind_ref(a)
        if not kind:
            out.append(a)
            continue
        rows = world.run_query(
            """
            MATCH (p:Person {name: $n})-[:HAS_ITEM]->(i:Item)
            WHERE toLower(coalesce(i.item_kind, '')) = $k
            RETURN i.name AS name ORDER BY i.name LIMIT 1
            """,
            n=actor,
            k=kind,
        )
        out.append(rows[0]["name"] if rows else a)
    return out


# ----- 给 LLM 读的通用描述 -----

def describe_recipes(view: WorldView, *, limit: int = 12) -> List[str]:
    """「X 用在 Y 上 → 得到 Z」，按 kind 去重。"""
    seen = set()
    lines: List[str] = []
    for r in view.recipes:
        if not r.produces:
            continue
        tool_kind = view.kind_of(r.tool) or r.tool
        key = (tool_kind, r.target, tuple(r.produces))
        if key in seen:
            continue
        seen.add(key)
        where = view.location_of(r.target) or "?"
        lines.append(
            f"- use a {tool_kind} on {r.target} (at {where}) -> get {', '.join(r.produces)}"
        )
        if len(lines) >= limit:
            break
    return lines
