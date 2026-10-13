"""
PDDL 自动编译 —— 把内容数据翻译成 domain / problem，不再手写 pddl_preconditions。

来源（全是数据）：
  - content/<pack>/actions/*.json 的 params / requires / effects / enter_state
  - 物品的 effects_on（use_on 的效果）；spawn 出来的原型会递归带入
  - 世界快照（位置、持有、关系边、属性）

翻译规则：
  LOCATED_AT / HAS_ITEM / 其他关系边      → 谓词 located-at / has-item / <rel>
  会被效果改写的数值属性（add_prop …）     → 数值函数 (hp ?x)
  会被改写的布尔 / 枚举属性               → 谓词 is-<prop> / <prop>-is-<value>
  从不被改写的属性条件                     → 静态谓词，事实在 problem 里预先算好
  spawn_item                               → 预分配的「未出生」对象池（隐藏参数）
  any / when                               → 拆成多个动作变体
翻译不了的（count、读同一动作前面效果的 when …）丢掉并写进 report；
content/<pack>/planning.json 用来手动剪枝。另外按目标做相关性剪枝：
只保留能影响目标（及其前提）的动作和效果。
"""

from __future__ import annotations

import copy
import itertools
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

PlanStep = Tuple[str, List[str]]

TYPE_OF = {"Person": "person", "Item": "item", "Location": "location"}
_MEMBERS = {
    "person": {"person"},
    "item": {"item"},
    "location": {"location"},
    "thing": {"person", "item"},
    "object": {"person", "item", "location"},
}
_PROP_CONDS = {"prop_eq", "prop_neq", "prop_gte", "prop_lte", "prop_in"}
_NUM_NEG = {">=": "<", "<=": ">", ">": "<=", "<": ">="}
_RESERVED = {
    "and", "or", "not", "either", "object", "when", "forall", "exists", "increase",
    "decrease", "assign", "define", "domain", "problem", "thing", "person", "item", "location",
}
_ID_BAD = re.compile(r"[^A-Za-z0-9_-]")


class Unsupported(Exception):
    """这段数据没有 PDDL 形式。"""


def _ident(raw: str) -> str:
    s = _ID_BAD.sub("_", str(raw)).strip("_-") or "x"
    if not s[0].isalpha() or s.lower() in _RESERVED:
        s = "o_" + s
    return s


def _num(v: Any) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(f)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _widen(a: str, b: str) -> str:
    if a == b:
        return a
    if {a, b} <= {"person", "item", "thing"}:
        return "thing"
    return "object"


# ---------- 手动剪枝配置 ----------

@dataclass
class PlanningConfig:
    exclude_actions: Set[str] = field(default_factory=set)
    actions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    drop_props: Set[str] = field(default_factory=set)
    drop_relations: Set[str] = field(default_factory=set)
    exclude_items: Set[str] = field(default_factory=set)
    exclude_kinds: Set[str] = field(default_factory=set)
    people: Any = "all"
    spawn_pool: int = 2
    spawn_pool_by: Dict[str, int] = field(default_factory=dict)
    max_variants: int = 8
    relevance: bool = True
    fallback: List[str] = field(default_factory=list)
    narrow_types: bool = True
    negative_needs: str = "initial"
    prune_objects: bool = True
    max_hops: Optional[int] = None
    max_per_kind: Optional[int] = None

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "PlanningConfig":
        d = d or {}

        def strs(key: str) -> Set[str]:
            return {str(x).strip() for x in d.get(key) or [] if str(x).strip()}

        return PlanningConfig(
            exclude_actions=strs("exclude_actions"),
            actions={str(k): dict(v) for k, v in (d.get("actions") or {}).items() if isinstance(v, dict)},
            drop_props=strs("drop_props"),
            drop_relations={x.upper() for x in strs("drop_relations")},
            exclude_items=strs("exclude_items"),
            exclude_kinds={x.lower() for x in strs("exclude_kinds")},
            people=d.get("people", "all"),
            spawn_pool=int(d.get("spawn_pool", 2)),
            spawn_pool_by={str(k).lower(): int(v) for k, v in (d.get("spawn_pool_by") or {}).items()},
            max_variants=int(d.get("max_variants", 8)),
            relevance=bool(d.get("relevance", True)),
            fallback=[str(x) for x in d.get("fallback") or []],
            narrow_types=bool(d.get("narrow_types", True)),
            negative_needs=str(d.get("negative_needs", "initial")),
            prune_objects=bool(d.get("prune_objects", True)),
            max_hops=None if d.get("max_hops") is None else int(d["max_hops"]),
            max_per_kind=None if d.get("max_per_kind") is None else int(d["max_per_kind"]),
        )


def load_config(overrides: Optional[Dict[str, Any]] = None) -> PlanningConfig:
    """内容包 planning.json（按包顺序合并）+ 调用方临时覆盖。"""
    from content import _deep_merge, get_content

    merged = copy.deepcopy(get_content().planning)
    if overrides:
        merged = _deep_merge(merged, overrides)
    return PlanningConfig.from_dict(merged)


# ---------- 世界快照 ----------

@dataclass
class Snapshot:
    objects: Dict[str, Tuple[str, Dict[str, Any]]] = field(default_factory=dict)  # name -> (label, props)
    location: Dict[str, str] = field(default_factory=dict)
    holder: Dict[str, str] = field(default_factory=dict)
    edges: Dict[str, Set[Tuple[str, str]]] = field(default_factory=dict)


def load_snapshot(world) -> Snapshot:
    snap = Snapshot()
    for label in ("Location", "Person", "Item"):
        for r in world.run_query(
            f"MATCH (n:{label}) RETURN n.name AS name, properties(n) AS props"
        ) or []:
            if r.get("name"):
                snap.objects[str(r["name"])] = (label, dict(r.get("props") or {}))
    for r in world.run_query(
        "MATCH (n)-[:LOCATED_AT]->(l:Location) WHERE n:Person OR n:Item RETURN n.name AS n, l.name AS l"
    ) or []:
        snap.location[r["n"]] = r["l"]
    for r in world.run_query(
        "MATCH (p:Person)-[:HAS_ITEM]->(i:Item) RETURN p.name AS p, i.name AS i"
    ) or []:
        snap.holder[r["i"]] = r["p"]
    for r in world.run_query(
        """
        MATCH (a)-[r]->(b)
        WHERE (a:Person OR a:Item OR a:Location) AND (b:Person OR b:Item OR b:Location)
          AND NOT type(r) IN ['LOCATED_AT', 'HAS_ITEM', 'SAW', 'KNOWS_PLACE']
        RETURN type(r) AS t, a.name AS a, b.name AS b
        """
    ) or []:
        snap.edges.setdefault(str(r["t"]), set()).add((r["a"], r["b"]))
    return snap


# ---------- 中间表示 ----------

@dataclass(frozen=True)
class Lit:
    text: str
    sym: str
    neg: bool = False
    numeric: bool = False

    def negate(self) -> "Lit":
        return replace(self, neg=not self.neg)

    def args(self) -> List[str]:
        return self.text.strip("()").split()[1:]

    def needs(self) -> Set[str]:
        """满足这个条件需要哪种方向的变化：+ / - / inc / dec。"""
        if self.numeric:
            op = self.text[1:3].strip()
            if op.startswith("="):
                return {"inc", "dec"}
            up = op.startswith(">")
            return {"inc" if up != self.neg else "dec"}
        negative = self.neg != self.text.startswith("(not")
        return {"-" if negative else "+"}

    def pddl(self) -> str:
        if not self.neg:
            return self.text
        if self.numeric:
            m = re.match(r"\(\s*(>=|<=|>|<)", self.text)
            if m:
                return "(" + _NUM_NEG[m.group(1)] + self.text[m.end():]
        return f"(not {self.text})"


Alts = List[List[Lit]]  # 析取范式：任一合取成立即可
TRUE: Alts = [[]]
FALSE: Alts = []


def _and(a: Alts, b: Alts) -> Alts:
    return [x + y for x in a for y in b]


def _not(alts: Alts) -> Alts:
    out: Alts = [[]]
    for conj in alts:
        if not conj:
            return []
        out = [x + [lit.negate()] for x in out for lit in conj]
    return out


@dataclass(frozen=True)
class Term:
    text: str
    type: str
    const: bool = False


@dataclass(frozen=True)
class Eff:
    text: str
    sym: str
    reads: FrozenSet[str] = frozenset()

    def gives(self) -> Set[str]:
        head = self.text[1:].split(None, 1)[0] if self.text.startswith("(") else ""
        return {
            "not": {"-"}, "increase": {"inc"}, "decrease": {"dec"}, "assign": {"inc", "dec"},
        }.get(head, {"+"})


@dataclass
class Obj:
    id: str
    name: str
    type: str
    props: Dict[str, Any]
    pool: str = ""
    ptype: str = ""  # PDDL 类型：物品按类别细分为 k-<kind>，其余同 type


@dataclass
class Schema:
    name: str
    source: str
    params: List[Tuple[str, str]]
    decode: List[str]
    pre: List[Lit]
    eff: List[Eff]
    goal: bool = False


@dataclass
class Variant:
    pre: List[Lit]
    eff: List[Eff] = field(default_factory=list)
    params: List[Tuple[str, str]] = field(default_factory=list)

    def with_pre(self, lits: List[Lit]) -> "Variant":
        pre = list(self.pre)
        for lit in lits:
            if lit not in pre:
                pre.append(lit)
        return Variant(pre, list(self.eff), list(self.params))

    def consistent(self) -> bool:
        seen = {(l.text, l.neg) for l in self.pre}
        return not any((t, not n) in seen for t, n in seen)


@dataclass
class CompileReport:
    rows: List[Tuple[str, str, str]] = field(default_factory=list)

    def add(self, action: str, part: str, note: str) -> None:
        self.rows.append((action, part, note))

    def text(self) -> str:
        if not self.rows:
            return "(nothing dropped)"
        grouped: Dict[str, List[str]] = {}
        for action, part, note in self.rows:
            grouped.setdefault(action, []).append(f"  - {part}: {note}" if part else f"  - {note}")
        return "\n".join(f"{a}:\n" + "\n".join(lines) for a, lines in grouped.items())


def _selected(selectors: Any, idx: int, spec: Dict[str, Any]) -> bool:
    """planning.json 选择器：下标，或 "op" / "op:<prop|rel|archetype|state>"。"""
    for sel in selectors or []:
        if isinstance(sel, int):
            if sel == idx:
                return True
            continue
        op, _, key = str(sel).partition(":")
        if spec.get("op") != op:
            continue
        if not key:
            return True
        keys = {str(spec.get(k) or "").lower() for k in ("prop", "rel", "archetype", "item_kind", "state")}
        if key.lower() in keys:
            return True
    return False


def _eval_static(cond: Dict[str, Any], name: str, props: Dict[str, Any]) -> bool:
    """与 graph_ops 的属性条件同义，只是读快照里的 props。"""
    op = cond.get("op")
    if op == "any":
        return any(_eval_static(s, name, props) for s in cond.get("of") or [])
    prop = cond.get("prop")
    got = name if prop == "name" else props.get(prop)
    want = cond.get("value")

    def same(a: Any, b: Any) -> bool:
        if _is_number(a) and _is_number(b):
            return float(a) == float(b)
        return a == b

    if op == "prop_eq":
        return bool(got) == want if isinstance(want, bool) else same(got, want)
    if op == "prop_neq":
        if isinstance(want, bool) and not cond.get("strict"):
            return bool(got) != want
        return not same(got, want)
    if op in ("prop_gte", "prop_lte"):
        try:
            n = float(got if got is not None else 0)
        except (TypeError, ValueError):
            return False
        w = float(want or 0)
        return n >= w if op == "prop_gte" else n <= w
    if op == "prop_in":
        got_s = str(got or "").lower()
        values = [str(v).lower() for v in cond.get("values") or []]
        return any(got_s.startswith(v) for v in values) if cond.get("prefix") else got_s in values
    return False


def _static_name(cond: Dict[str, Any]) -> str:
    op = cond.get("op")
    if op == "any":
        return "any-" + "-or-".join(_static_name(s) for s in cond.get("of") or [])
    prop = str(cond.get("prop"))
    if op == "prop_in":
        tail = "-".join(str(v) for v in cond.get("values") or [])
        return f"{prop}-{'prefix' if cond.get('prefix') else 'in'}-{tail}"
    word = {"prop_eq": "is", "prop_neq": "not", "prop_gte": "gte", "prop_lte": "lte"}[op]
    return f"{prop}-{word}-{str(cond.get('value')).lower()}"


def _strip_cond(cond: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in cond.items() if k not in ("entity", "label", "message")}
    if cond.get("op") == "any":
        out["of"] = [_strip_cond(s) for s in cond.get("of") or []]
    return out


_FLUENT_RE = re.compile(r"\(\s*(?:increase|decrease|assign|>=|<=|>|<|=)\s+\(([^()]*)\)")


def _atom_args(text: str) -> List[str]:
    """`(p a b)` / `(not (p a b))` / `(increase (f a) …)` / `(>= (f a) 1)` → 参数列表。"""
    t = text.strip()
    if t.startswith("(not "):
        t = t[5:-1].strip()
    m = _FLUENT_RE.match(t)
    if m:
        return m.group(1).split()[1:]
    return t.strip("()").split()[1:]


def _effects_on(props: Dict[str, Any]) -> List[Dict[str, Any]]:
    from world_interactions import _parse_json_field, normalize_effect

    data = _parse_json_field(props.get("effects_on"))
    return [normalize_effect(e) for e in data or [] if isinstance(e, dict)]


# ---------- 编译器 ----------

class Compiler:
    def __init__(self, snap: Snapshot, character: str, cfg: PlanningConfig, content) -> None:
        self.snap = snap
        self.character = character
        self.cfg = cfg
        self.content = content
        self.report = CompileReport()
        self.objs: Dict[str, Obj] = {}
        self.by_name: Dict[str, str] = {}
        self.preds: Dict[str, List[str]] = {}
        self.fluents: Dict[str, Tuple[str, str]] = {}  # fluent -> (type, prop)
        self.prop_kind: Dict[str, str] = {}  # 会被改写的属性 -> num / bool / sym
        self.sym_values: Dict[str, Set[str]] = {}
        self.facts: Dict[str, Any] = {}  # 谓词 -> 事实来源
        self.pools: Dict[str, Dict[str, Any]] = {}
        self.entries: Dict[str, Tuple[Dict[str, Any], List[str]]] = {}
        self.accepts: Set[Tuple[str, str]] = set()
        self.schemas: List[Schema] = []
        self.goals: List[Schema] = []
        self._members: Dict[str, Optional[Set[str]]] = {}
        self._fact_cache: Dict[str, List[Tuple[str, ...]]] = {}
        self._ids = itertools.count(1)
        self._where = ""

    # ----- 入口 -----

    def compile(self, goal: List[Dict[str, Any]]) -> None:
        self._add_world_objects()
        self._discover(goal)
        self._add_pool_objects()
        self._collect_entries()
        for spec in self.content.actions.values():
            self._compile_action(spec)
        self.report.add("interact", "", "not compiled: its effects are LLM-written Cypher")
        self._compile_use_on()
        self._compile_goal(goal)
        if self.cfg.narrow_types:
            self._narrow()
        if self.cfg.relevance:
            self._prune()
            if self._simplify_numeric():
                self._prune()
        else:
            self.schemas = [s for s in self.schemas if s.eff]
        if self.cfg.prune_objects:
            self._prune_objects()

    # ----- 对象 -----

    def _add_obj(self, name: str, typ: str, props: Dict[str, Any], pool: str = "") -> str:
        base = _ident(name)
        oid, n = base, 1
        taken = {o.lower() for o in self.objs}
        while oid.lower() in taken:
            n += 1
            oid = f"{base}_{n}"
        kind = str(props.get("item_kind") or "").strip().lower() if typ == "item" else ""
        ptype = _ident(f"k-{kind}").lower() if kind else typ
        self.objs[oid] = Obj(oid, name, typ, props, pool, ptype)
        if not pool:
            self.by_name[name] = oid
        return oid

    def _add_world_objects(self) -> None:
        cfg = self.cfg
        allowed_people = None
        if cfg.people != "all":
            allowed_people = {self.character} | set(cfg.people if isinstance(cfg.people, list) else [])
        for name, (label, props) in sorted(self.snap.objects.items()):
            if label == "Item":
                kind = str(props.get("item_kind") or "").lower()
                if name in cfg.exclude_items or kind in cfg.exclude_kinds:
                    continue
            if label == "Person" and allowed_people is not None and name not in allowed_people:
                continue
            self._add_obj(name, TYPE_OF[label], props)
        if self.character not in self.by_name:
            raise ValueError(f"Unknown character: {self.character}")

    def _of_type(self, typ: str) -> List[Obj]:
        members = _MEMBERS[typ]
        return [o for o in self.objs.values() if o.type in members]

    def _kind(self, obj: Obj) -> str:
        return str(obj.props.get("item_kind") or "").strip().lower()

    # ----- 发现：哪些属性会变、要预留哪些 spawn 对象 -----

    def _active_actions(self) -> List[Dict[str, Any]]:
        return [s for n, s in self.content.actions.items() if not self._excluded(n)]

    def _discover(self, goal: List[Dict[str, Any]]) -> None:
        queue: List[Dict[str, Any]] = []
        conds: List[Dict[str, Any]] = list(goal)
        for spec in self._active_actions():
            queue += list(spec.get("effects") or [])
            if spec.get("enter_state"):
                queue.append({"op": "set_state", "entity": "@actor", "state": spec["enter_state"]})
            conds += list(spec.get("requires") or [])
        use_on = not self._excluded("use_on")
        if use_on:
            for obj in list(self.objs.values()):
                for e in _effects_on(obj.props):
                    queue += e.get("ops") or []
                    conds += e.get("preconditions") or []
        while queue:
            op = queue.pop()
            conds += op.get("when") or []
            self._scan_op(op)
            if op.get("op") != "spawn_item":
                continue
            key, props = self._pool_spec(op)
            if key in self.pools:
                continue
            self.pools[key] = props
            if use_on:
                for e in _effects_on(props):
                    queue += e.get("ops") or []
                    conds += e.get("preconditions") or []
        while conds:
            c = conds.pop()
            if c.get("op") == "any":
                conds += c.get("of") or []
            elif c.get("op") in ("prop_eq", "prop_neq") and self.prop_kind.get(c.get("prop")) == "sym":
                if c.get("value") not in (None, ""):
                    self.sym_values.setdefault(c["prop"], set()).add(str(c["value"]))
            elif c.get("op") == "prop_in" and self.prop_kind.get(c.get("prop")) == "sym":
                self.sym_values.setdefault(c["prop"], set()).update(str(v) for v in c.get("values") or [])

    def _scan_op(self, op: Dict[str, Any]) -> None:
        kind = op.get("op")
        prop = op.get("prop")
        if prop in self.cfg.drop_props:
            return
        if kind == "add_prop":
            self.prop_kind[prop] = "num"
        elif kind == "set_prop":
            value = op.get("value")
            if _is_number(value):
                self.prop_kind[prop] = "num"
            elif isinstance(value, bool):
                self.prop_kind.setdefault(prop, "bool")
            else:
                self.prop_kind.setdefault(prop, "sym")
                if value not in (None, "") and not str(value).startswith("@"):
                    self.sym_values.setdefault(prop, set()).add(str(value))
        elif kind == "set_state" and "state" not in self.cfg.drop_props:
            self.prop_kind.setdefault("state", "sym")
            if op.get("state"):
                self.sym_values.setdefault("state", set()).add(str(op["state"]))

    def _pool_spec(self, op: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        spec = dict(op)
        if spec.get("archetype"):
            from content import apply_archetype

            spec = apply_archetype(spec)
        key = _ident(str(spec.get("archetype") or spec.get("name") or "item")).lower()
        props: Dict[str, Any] = {"portable": True}
        for k in ("item_kind", "portable", "value", "description", "archetype", "effects_on", "accepts_from"):
            if spec.get(k) is not None:
                props[k] = spec[k]
        script = spec.get("script")
        for k, v in ((script or {}).get("initial_state") or {}).items() if isinstance(script, dict) else []:
            if k not in props and isinstance(v, (str, int, float, bool)):
                props[k] = v
        props["name"] = str(spec.get("name") or key)
        return key, props

    def _add_pool_objects(self) -> None:
        for key, props in self.pools.items():
            size = self.cfg.spawn_pool_by.get(key, self.cfg.spawn_pool)
            for i in range(1, size + 1):
                self._add_obj(f"new-{key}-{i}", "item", dict(props), pool=key)

    def _collect_entries(self) -> None:
        for obj in self.objs.values():
            if obj.type != "item":
                continue
            for e in _effects_on(obj.props):
                key = json.dumps(e, sort_keys=True, ensure_ascii=False)
                self.entries.setdefault(key, (e, []))[1].append(obj.id)

    # ----- 词汇表 -----

    def _declare(self, pred: str, types: List[str], facts: Any) -> None:
        old = self.preds.get(pred)
        self.preds[pred] = [_widen(a, b) for a, b in zip(old, types)] if old else list(types)
        self.facts.setdefault(pred, facts)

    def _rel(self, rel: str, a: Term, b: Term) -> Lit:
        rel = rel.upper()
        if rel == "LOCATED_AT":
            pred, types = "located-at", ["thing", "location"]
        elif rel == "HAS_ITEM":
            pred, types = "has-item", ["person", "item"]
        else:
            pred, types = _ident(rel.lower().replace("_", "-")), [a.type, b.type]
        self._declare(pred, types, ("rel", rel))
        return Lit(f"({pred} {a.text} {b.text})", pred)

    def _held(self, t: Term) -> Lit:
        self._declare("held", ["item"], ("held",))
        return Lit(f"(held {t.text})", "held")

    def _fluent(self, prop: str, typ: str) -> str:
        name = _ident(prop).lower()
        old = self.fluents.get(name)
        self.fluents[name] = (_widen(old[0], typ) if old else typ, prop)
        return name

    def _bool_lit(self, prop: str, t: Term) -> Lit:
        pred = _ident(f"is-{prop}").lower()
        self._declare(pred, [t.type], ("bool", prop))
        return Lit(f"({pred} {t.text})", pred)

    def _sym_lit(self, prop: str, value: str, t: Term) -> Lit:
        pred = _ident(f"{prop}-is-{value}").lower()
        self._declare(pred, [t.type], ("sym", prop, value))
        return Lit(f"({pred} {t.text})", pred)

    def _static(self, cond: Dict[str, Any], t: Term) -> Alts:
        key = _strip_cond(cond)
        if t.const:
            obj = self.objs[t.text]
            return TRUE if _eval_static(key, obj.name, obj.props) else FALSE
        pred = _ident(_static_name(key)).lower()[:60]
        while pred in self.facts and self.facts[pred] != ("static", key):
            pred = f"{pred}-{next(self._ids)}"
        self._declare(pred, [t.type], ("static", key))
        return [[Lit(f"({pred} {t.text})", pred)]]

    def _term(self, token: Any, env: Dict[str, Term]) -> Term:
        if isinstance(token, str) and token.startswith("@"):
            key = token[1:]
            if "." in key:
                raise Unsupported(f"{token} is a property, not an entity")
            if key in env:
                return env[key]
            raise Unsupported(f"unknown placeholder {token}")
        oid = self.by_name.get(str(token))
        if oid is None:
            raise Unsupported(f"unknown entity {token}")
        return Term(oid, self.objs[oid].type, const=True)

    # ----- 条件 -----

    def _cond(self, cond: Dict[str, Any], env: Dict[str, Term]) -> Alts:
        op = cond.get("op")
        cfg = self.cfg
        if op in _PROP_CONDS:
            prop = cond.get("prop")
            if prop in cfg.drop_props:
                return TRUE
            t = self._term(cond.get("entity"), env)
            kind = self.prop_kind.get(prop) if prop != "name" else None
            return self._static(cond, t) if kind is None else self._dyn_prop(cond, t, kind)
        if op == "any":
            subs = cond.get("of") or []
            if subs and all(
                s.get("op") in _PROP_CONDS
                and s.get("entity") == subs[0].get("entity")
                and self.prop_kind.get(s.get("prop")) is None
                for s in subs
            ):
                return self._static(cond, self._term(subs[0].get("entity"), env))
            out: Alts = []
            for s in subs:
                out += self._cond(s, env)
            return out
        if op == "has_item":
            return [[self._rel("HAS_ITEM", self._term(cond["person"], env), self._term(cond["item"], env))]]
        if op == "person_at":
            return [[self._rel("LOCATED_AT", self._term(cond["person"], env), self._term(cond["location"], env))]]
        if op == "item_at":
            return [[self._rel("LOCATED_AT", self._term(cond["entity"], env), self._term(cond["location"], env))]]
        if op == "not_held":
            return [[self._held(self._term(cond["item"], env)).negate()]]
        if op in ("has_rel", "not_has_rel"):
            rel = str(cond.get("rel") or "ADJACENT_TO").upper()
            if rel in cfg.drop_relations:
                return TRUE
            a, b = self._term(cond["a"], env), self._term(cond["b"], env)
            ab, ba = self._rel(rel, a, b), self._rel(rel, b, a)
            if op == "not_has_rel":
                return [[ab.negate(), ba.negate()]]
            return [[ab]] if cond.get("directed") else [[ab], [ba]]
        if op == "neq":
            a, b = self._term(cond["a"], env), self._term(cond["b"], env)
            if a.const and b.const:
                return TRUE if a.text != b.text else FALSE
            return [[Lit(f"(= {a.text} {b.text})", "=").negate()]]
        raise Unsupported(f"condition '{op}' has no PDDL form")

    def _dyn_prop(self, cond: Dict[str, Any], t: Term, kind: str) -> Alts:
        op, prop, want = cond["op"], cond["prop"], cond.get("value")
        if kind == "num":
            expr = f"({self._fluent(prop, t.type)} {t.text})"
            f = _ident(prop).lower()
            if op in ("prop_gte", "prop_lte"):
                sign = ">=" if op == "prop_gte" else "<="
                return [[Lit(f"({sign} {expr} {_num(want or 0)})", f, numeric=True)]]
            if op in ("prop_eq", "prop_neq") and _is_number(want):
                if op == "prop_eq":
                    return [[Lit(f"(= {expr} {_num(want)})", f, numeric=True)]]
                return [[Lit(f"(< {expr} {_num(want)})", f, numeric=True)],
                        [Lit(f"(> {expr} {_num(want)})", f, numeric=True)]]
            raise Unsupported(f"{op} on numeric property '{prop}'")
        if kind == "bool":
            lit = self._bool_lit(prop, t)
            if op == "prop_eq" and isinstance(want, bool):
                return [[lit if want else lit.negate()]]
            if op == "prop_neq" and isinstance(want, bool):
                return [[lit.negate() if want else lit]]
            raise Unsupported(f"{op} on boolean property '{prop}'")
        if op in ("prop_eq", "prop_neq"):
            if want in (None, ""):
                lits = [self._sym_lit(prop, v, t).negate() for v in sorted(self.sym_values.get(prop, ()))]
                return [lits] if op == "prop_eq" else [[l.negate()] for l in lits]
            lit = self._sym_lit(prop, str(want), t)
            return [[lit]] if op == "prop_eq" else [[lit.negate()]]
        if op == "prop_in" and not cond.get("prefix"):
            return [[self._sym_lit(prop, str(v), t)] for v in cond.get("values") or []]
        raise Unsupported(f"{op} on property '{prop}'")

    # ----- 效果 -----

    def _op(self, v: Variant, op: Dict[str, Any], env: Dict[str, Term]) -> Variant:
        kind = op.get("op")
        w = Variant(list(v.pre), list(v.eff), list(v.params))
        if kind in ("set_prop", "add_prop"):
            prop = op["prop"]
            if prop in self.cfg.drop_props:
                raise Unsupported(f"property '{prop}' is in drop_props")
            t = self._term(op["entity"], env)
            if kind == "add_prop":
                w.eff.append(self._add_prop(op, prop, t, env))
            else:
                self._set_prop(w, prop, op.get("value"), t)
            return w
        if kind == "set_state":
            if "state" in self.cfg.drop_props:
                raise Unsupported("property 'state' is in drop_props")
            self._set_sym(w, "state", op.get("state"), self._term(op["entity"], env))
            return w
        if kind in ("link", "unlink"):
            rel = str(op.get("rel") or "ADJACENT_TO").upper()
            if rel in self.cfg.drop_relations:
                raise Unsupported(f"relation {rel} is in drop_relations")
            a, b = self._term(op["a"], env), self._term(op["b"], env)
            pairs = [(a, b)] + ([(b, a)] if op.get("bidirectional") else [])
            for x, y in pairs:
                lit = self._rel(rel, x, y)
                w.eff.append(Eff(lit.text if kind == "link" else f"(not {lit.text})", lit.sym))
                if rel == "HAS_ITEM":
                    held = self._held(y)
                    w.eff.append(Eff(held.text if kind == "link" else f"(not {held.text})", "held"))
            return w
        if kind == "relocate":
            e, to = self._term(op["entity"], env), self._term(op["to"], env)
            old = [l.args()[1] for l in w.pre if not l.neg and l.sym == "located-at" and l.args()[0] == e.text]
            if not old and e.type == "person":
                var = f"?from{next(self._ids)}"
                w.params.append((var, "location"))
                w.pre.append(self._rel("LOCATED_AT", e, Term(var, "location")))
                old = [var]
            for loc in old:
                w.eff.append(Eff(f"(not (located-at {e.text} {loc}))", "located-at"))
            if e.type == "item":
                self._drop_holder(w, e)
            w.eff.append(Eff(self._rel("LOCATED_AT", e, to).text, "located-at"))
            return w
        if kind == "delete_entity":
            e = self._term(op["entity"], env)
            if e.type != "item":
                raise Unsupported("only items can be deleted")
            found = False
            for l in w.pre:
                if l.neg:
                    continue
                if l.sym == "located-at" and l.args()[0] == e.text:
                    w.eff.append(Eff(f"(not {l.text})", "located-at"))
                    found = True
                if l.sym == "has-item" and l.args()[1] == e.text:
                    w.eff.append(Eff(f"(not {l.text})", "has-item"))
                    found = True
            w.eff.append(Eff(f"(not {self._held(e).text})", "held"))
            if not found:
                self.report.add(self._where, "delete_entity", "approx: no known location/holder to clear")
            return w
        if kind == "spawn_item":
            key, _ = self._pool_spec(op)
            var = f"?new{next(self._ids)}"
            w.params.append((var, "item"))
            self._declare("unspawned", ["item"], ("unspawned",))
            self._declare(f"pool-{key}", ["item"], ("pool", key))
            w.pre += [Lit(f"(unspawned {var})", "unspawned"), Lit(f"(pool-{key} {var})", f"pool-{key}")]
            w.eff.append(Eff(f"(not (unspawned {var}))", "unspawned"))
            new = Term(var, "item")
            if op.get("held_by"):
                holder = self._term(op["held_by"], env)
                w.eff.append(Eff(self._rel("HAS_ITEM", holder, new).text, "has-item"))
                w.eff.append(Eff(self._held(new).text, "held"))
            elif op.get("located_at"):
                loc = self._term(op["located_at"], env)
                w.eff.append(Eff(self._rel("LOCATED_AT", new, loc).text, "located-at"))
            return w
        raise Unsupported(f"operation '{kind}' has no PDDL form")

    def _drop_holder(self, w: Variant, e: Term) -> None:
        holders = [l for l in w.pre if not l.neg and l.sym == "has-item" and l.args()[1] == e.text]
        for l in holders:
            w.eff.append(Eff(f"(not {l.text})", "has-item"))
        w.eff.append(Eff(f"(not {self._held(e).text})", "held"))
        if not holders and Lit(f"(held {e.text})", "held", neg=True) not in w.pre:
            self.report.add(self._where, "relocate", "approx: previous holder not cleared")

    def _add_prop(self, op: Dict[str, Any], prop: str, t: Term, env: Dict[str, Term]) -> Eff:
        f = self._fluent(prop, t.type)
        expr = f"({f} {t.text})"
        scale = float(op.get("scale", 1))
        delta = op.get("delta", 0)
        reads: Set[str] = set()
        if isinstance(delta, str) and delta.startswith("@"):
            ent, _, p2 = delta[1:].partition(".")
            if not p2:
                raise Unsupported(f"delta {delta} is not a property")
            src = self._term("@" + ent, env)
            g = self._fluent(p2, src.type)
            reads.add(g)
            amount = f"({g} {src.text})"
            if scale == 1:
                text = f"(increase {expr} {amount})"
            elif scale == -1:
                text = f"(decrease {expr} {amount})"
            else:
                text = f"(increase {expr} (* {_num(scale)} {amount}))"
        else:
            try:
                d = float(delta) * scale
            except (TypeError, ValueError):
                raise Unsupported(f"delta {delta!r} is not a number")
            text = f"(increase {expr} {_num(d)})" if d >= 0 else f"(decrease {expr} {_num(-d)})"
        if op.get("min") is not None or op.get("max") is not None:
            self.report.add(self._where, f"add_prop {prop}", "approx: min/max clamp ignored")
        return Eff(text, f, frozenset(reads))

    def _set_prop(self, w: Variant, prop: str, value: Any, t: Term) -> None:
        if isinstance(value, str) and value.startswith("@"):
            raise Unsupported(f"value {value} comes from a placeholder")
        kind = self.prop_kind[prop]
        if kind == "num":
            if not _is_number(value):
                raise Unsupported(f"non-numeric value for numeric '{prop}'")
            f = self._fluent(prop, t.type)
            w.eff.append(Eff(f"(assign ({f} {t.text}) {_num(value)})", f))
        elif kind == "bool":
            lit = self._bool_lit(prop, t)
            w.eff.append(Eff(lit.text if value else f"(not {lit.text})", lit.sym))
        else:
            self._set_sym(w, prop, value, t)

    def _set_sym(self, w: Variant, prop: str, value: Any, t: Term) -> None:
        for v in sorted(self.sym_values.get(prop, ())):
            if value is not None and str(v) == str(value):
                continue
            lit = self._sym_lit(prop, v, t)
            w.eff.append(Eff(f"(not {lit.text})", lit.sym))
        if value not in (None, ""):
            lit = self._sym_lit(prop, str(value), t)
            w.eff.append(Eff(lit.text, lit.sym))

    # ----- 动作 -----

    def _me(self, t: Term) -> Lit:
        self._declare("is-me", ["person"], ("me",))
        return Lit(f"(is-me {t.text})", "is-me")

    def _build(
        self,
        name: str,
        source: str,
        env: Dict[str, Term],
        params: List[Tuple[str, str]],
        decode: List[str],
        requires: List[Dict[str, Any]],
        effects: List[Dict[str, Any]],
        over: Dict[str, Any],
        extra: Optional[List[Lit]] = None,
    ) -> None:
        self._where = name
        variants = [Variant([self._me(env["actor"])] + list(extra or []))]
        for idx, cond in enumerate(requires):
            part = f"requires[{idx}] {cond.get('op')}"
            if _selected(over.get("drop_requires"), idx, cond):
                self.report.add(name, part, "dropped by planning.json")
                continue
            try:
                alts = self._cond(cond, env)
            except Unsupported as e:
                self.report.add(name, part, f"ignored (assumed true): {e}")
                continue
            variants = [x for v in variants for alt in alts for x in [v.with_pre(alt)] if x.consistent()]
        written: Set[str] = set()
        for idx, op in enumerate(effects):
            part = f"effects[{idx}] {op.get('op')}"
            if _selected(over.get("drop_effects"), idx, op):
                self.report.add(name, part, "dropped by planning.json")
                continue
            try:
                when = op.get("when")
                when_alts: Alts = TRUE
                for c in when or []:
                    when_alts = _and(when_alts, self._cond(c, env))
                if when and {l.sym for alt in when_alts for l in alt} & written:
                    raise Unsupported("`when` reads a value changed earlier in the same action")
                new: List[Variant] = []
                for v in variants:
                    for alt in when_alts:
                        x = v.with_pre(alt)
                        if x.consistent():
                            new.append(self._op(x, op, env))
                    if when:
                        for alt in _not(when_alts):
                            x = v.with_pre(alt)
                            if x.consistent():
                                new.append(x)
                if len(new) > self.cfg.max_variants:
                    raise Unsupported(f"too many variants ({len(new)} > max_variants)")
                for x in new:
                    written |= {e.sym for e in x.eff}
                variants = new
            except Unsupported as e:
                self.report.add(name, part, f"ignored: {e}")
        if not variants:
            self.report.add(name, "", "never applicable (conditions contradict)")
            return
        if not any(v.eff for v in variants):
            self.report.add(name, "", "no state change to plan with")
            return
        if source != name and self._excluded(name):
            self.report.add(name, "", "excluded by planning.json")
            return
        for i, v in enumerate(variants, 1):
            sname = name if len(variants) == 1 else f"{name}--v{i}"
            self.schemas.append(Schema(_ident(sname), source, params + v.params, decode, v.pre, v.eff))

    def _excluded(self, name: str) -> bool:
        from fnmatch import fnmatchcase

        return any(fnmatchcase(name, pat) for pat in self.cfg.exclude_actions)

    def _compile_action(self, spec: Dict[str, Any]) -> None:
        name = str(spec["name"])
        if self._excluded(name):
            self.report.add(name, "", "excluded by planning.json")
            return
        over = self.cfg.actions.get(name) or {}
        env: Dict[str, Term] = {}
        params: List[Tuple[str, str]] = []
        for p in spec["params"]:
            t = Term("?" + _ident(p["name"]).lower(), TYPE_OF[p["type"]])
            env[p["name"]] = t
            params.append((t.text, t.type))
        env["actor"] = env[spec["params"][0]["name"]]
        decode = [v for v, _ in params]
        effects = list(spec.get("effects") or [])
        if spec.get("enter_state"):
            effects.append({"op": "set_state", "entity": "@actor", "state": spec["enter_state"]})
        n = len(self.schemas)
        self._build(name, name, env, params, decode, list(spec.get("requires") or []), effects, over)
        if over.get("pddl"):
            # 自动版本只用来登记谓词词汇，随后换成手写的
            del self.schemas[n:]
            self.report.rows = [r for r in self.report.rows if r[0] != name]
            self._manual(name, params, decode, env, over["pddl"])

    def _manual(self, name: str, params, decode, env, pddl: Dict[str, Any]) -> None:
        """planning.json 里手写的 pre / eff（用本编译器的谓词名）。"""
        sym_re = re.compile(r"\(\s*(?:not\s*\(\s*)?([A-Za-z][\w-]*)")

        def sym(text: str) -> str:
            m = sym_re.match(text)
            return m.group(1) if m else ""

        pre = [self._me(env["actor"])] + [Lit(str(t), sym(str(t))) for t in pddl.get("pre") or []]
        eff = [Eff(str(t), sym(str(t))) for t in pddl.get("eff") or []]
        self.report.add(name, "", "hand-written PDDL from planning.json")
        self.schemas.append(Schema(_ident(name), name, list(params), decode, pre, eff))

    def _compile_use_on(self) -> None:
        if self._excluded("use_on"):
            self.report.add("use_on", "", "excluded by planning.json")
            return
        from world_interactions import _parse_json_field, _tool_accepted

        over = self.cfg.actions.get("use_on") or {}
        for n, (effect, tools) in enumerate(self.entries.values(), 1):
            target = str(effect.get("target") or "").strip()
            actor, tool, here = Term("?actor", "person"), Term("?tool", "item"), Term("?here", "location")
            params = [("?actor", "person"), ("?tool", "item")]
            tool_pred = f"tool-{n}"
            self._declare(tool_pred, ["item"], ("tools", set(tools)))
            extra = [Lit(f"({tool_pred} ?tool)", tool_pred)]
            if target.lower().startswith("kind:"):
                kind = target[5:].strip().lower()
                tgt = Term("?target", "item")
                params.append(("?target", "item"))
                extra += self._static({"op": "prop_in", "prop": "item_kind", "values": [kind]}, tgt)[0]
                targets = [o for o in self.objs.values() if o.type == "item" and self._kind(o) == kind]
                label = kind
            else:
                oid = self.by_name.get(target)
                if oid is None:
                    continue
                tgt = Term(oid, self.objs[oid].type, const=True)
                targets = [self.objs[oid]]
                label = target
            params.append(("?here", "location"))
            pairs = set()
            for t in targets:
                accepts = _parse_json_field(t.props.get("accepts_from"))
                accepts = [str(x) for x in accepts] if isinstance(accepts, list) else []
                if not accepts and t.props.get("key_item"):
                    accepts = [str(t.props["key_item"])]
                for tid in tools:
                    tool_obj = self.objs[tid]
                    if _tool_accepted(tool_obj.name, accepts, tool_kind=self._kind(tool_obj)):
                        pairs.add((t.id, tid))
            first = self.objs[tools[0]]
            name = f"use_on--{self._kind(first) or first.name}--{label}"
            if not pairs:
                self.report.add(name, "", "skipped: no target accepts these tools")
                continue
            self.accepts |= pairs
            self._declare("accepts", ["thing", "item"], ("accepts",))
            extra.append(Lit(f"(accepts {tgt.text} ?tool)", "accepts"))
            extra.append(self._rel("LOCATED_AT", tgt, here))
            env = {"actor": actor, "tool": tool, "target": tgt, "here": here, "target_loc": here}
            requires = [
                {"op": "has_item", "person": "@actor", "item": "@tool"},
                {"op": "prop_gte", "entity": "@actor", "label": "Person", "prop": "hp", "value": 1},
                {"op": "person_at", "person": "@actor", "location": "@here"},
            ] + list(effect.get("preconditions") or [])
            decode = ["?actor", "?tool", tgt.text, "?here", "?here"]
            self._build(name, "use_on", env, params, decode, requires, list(effect.get("ops") or []), over, extra)

    # ----- 目标 -----

    def _compile_goal(self, goal: List[Dict[str, Any]]) -> None:
        self._where = "goal"
        me = self.by_name[self.character]
        env: Dict[str, Term] = {"self": Term(me, "person", const=True), "actor": Term(me, "person", const=True)}
        params: List[Tuple[str, str]] = []
        extra: List[Lit] = []

        def bind(value: Any) -> Any:
            if isinstance(value, str) and value.lower().startswith("kind:"):
                var = f"g{len(params) + 1}"
                t = Term(f"?{var}", "item")
                env[var] = t
                params.append((t.text, "item"))
                extra.extend(self._static({"op": "prop_in", "prop": "item_kind", "values": [value[5:].strip().lower()]}, t)[0])
                return f"@{var}"
            return value

        conds = [{k: bind(v) for k, v in c.items()} for c in goal]
        alts: Alts = [list(extra)]
        for c in conds:
            try:
                alts = _and(alts, self._cond(c, env))
            except Unsupported as e:
                raise ValueError(f"Goal condition {c.get('op')} cannot be planned: {e}") from e
        alts = [a for a in alts if Variant(a).consistent()]
        if not alts:
            raise ValueError("Goal is impossible in this world")
        self._declare("goal-reached", [], ("goal",))
        for i, alt in enumerate(alts, 1):
            self.goals.append(Schema(
                f"reach-goal--v{i}" if len(alts) > 1 else "reach-goal",
                "", list(params), [], alt, [Eff("(goal-reached)", "goal-reached")], goal=True,
            ))

    # ----- 相关性剪枝 -----

    def _helps(self, s: Schema, e: Eff, needs: Dict[Tuple[str, str], Set[Tuple[str, ...]]]) -> bool:
        """效果 e 能否满足某个所需事实：谓词、方向一致，且参数能对上（常量相等 / 变量类型容得下且没被 ≠ 排除）。"""
        eargs = _atom_args(e.text)
        types: Dict[str, str] = {}
        for var, typ in s.params:
            types.setdefault(var, typ)
        neq = {tuple(l.args()) for l in s.pre if l.sym == "=" and l.neg}

        def match(nargs: Tuple[str, ...]) -> bool:
            if len(nargs) != len(eargs):
                return True
            for ea, na in zip(eargs, nargs):
                if na == "*":
                    continue
                if ea.startswith("?"):
                    obj = self.objs.get(na)
                    if (obj is None or not self._fits(obj, types.get(ea, "object"))
                            or (ea, na) in neq or (na, ea) in neq):
                        return False
                elif ea != na:
                    return False
            return True

        for p in e.gives():
            if not any(match(n) for n in needs.get((e.sym, p), ())):
                continue
            # 松弛：否定前提只为「初始就为真」的事实去找删除它的动作（不考虑先做出来再撤销）
            if p == "-" and self.cfg.negative_needs == "initial" and e.sym in self.facts:
                if not any(match(f) for f in self._facts(e.sym)):
                    continue
            return True
        return False

    @staticmethod
    def _add_needs(needs: Dict[Tuple[str, str], Set[Tuple[str, ...]]], lits: List[Lit]) -> None:
        for l in lits:
            args = tuple("*" if a.startswith("?") else a for a in _atom_args(l.text))
            for p in l.needs():
                needs.setdefault((l.sym, p), set()).add(args)

    def _prune(self) -> None:
        """只保留能把某个所需事实往需要的方向推的动作；保留动作里所有涉及相关符号的效果。"""
        needs: Dict[Tuple[str, str], Set[Tuple[str, ...]]] = {}
        for g in self.goals:
            self._add_needs(needs, g.pre)
        active: Set[int] = set()
        changed = True
        while changed:
            changed = False
            for i, s in enumerate(self.schemas):
                if i in active or not any(self._helps(s, e, needs) for e in s.eff):
                    continue
                active.add(i)
                changed = True
                self._add_needs(needs, s.pre)
                for e in s.eff:
                    for r in e.reads:
                        for p in ("inc", "dec"):
                            needs.setdefault((r, p), set()).add(("*",))
        relevant = {sym for sym, _ in needs}
        kept: List[Schema] = []
        for i, s in enumerate(self.schemas):
            if i not in active:
                self.report.add(s.name, "", "pruned: cannot help reach the goal")
                continue
            kept.append(replace(s, eff=[e for e in s.eff if e.sym in relevant]))
        self.schemas = kept

    # ----- 单调数值化简 -----

    _NUM_RE = re.compile(r"^\((>=|<=|>|<)\s+\((\S+)((?:\s+[^\s()]+)*)\)\s+(-?[\d.]+)\)$")

    def _numeric_verdict(self, lit: Lit, dirs: Dict[str, Set[str]]) -> Optional[bool]:
        """剩下的动作只会让它往有利方向变、且现在已成立 → 恒真；只会往不利方向变、且现在不成立 → 恒假。"""
        if not lit.numeric:
            return None
        m = self._NUM_RE.match(lit.pddl())
        if not m or m.group(2) not in self.fluents:
            return None
        op, f, args, bound = m.group(1), m.group(2), m.group(3).split(), float(m.group(4))
        if len(args) != 1:
            return None
        typ, prop = self.fluents[f]
        objs = [self.objs[args[0]]] if args[0] in self.objs else self._of_type(typ)

        def holds(o: Obj) -> bool:
            try:
                v = float(o.props.get(prop) or 0)
            except (TypeError, ValueError):
                v = 0.0
            return {">=": v >= bound, "<=": v <= bound, ">": v > bound, "<": v < bound}[op]

        truth = {holds(o) for o in objs}
        changes = dirs.get(f, set())
        helpful = changes & lit.needs()
        if truth == {True} and changes <= lit.needs():
            return True
        if truth == {False} and not helpful:
            return False
        return None

    def _simplify_numeric(self) -> bool:
        dirs: Dict[str, Set[str]] = {}
        for s in self.schemas:
            for e in s.eff:
                if e.sym in self.fluents:
                    dirs.setdefault(e.sym, set()).update(e.gives())
        changed = False

        def simplify(s: Schema) -> Optional[Schema]:
            nonlocal changed
            pre: List[Lit] = []
            for lit in s.pre:
                verdict = self._numeric_verdict(lit, dirs)
                if verdict is False:
                    return None
                if verdict is True:
                    changed = True
                    continue
                pre.append(lit)
            return replace(s, pre=pre)

        kept: List[Schema] = []
        for s in self.schemas:
            n = simplify(s)
            if n is None:
                changed = True
                self.report.add(s.name, "", "pruned: a numeric condition can never become true")
            else:
                kept.append(n)
        goals = [g for g in (simplify(g) for g in self.goals) if g is not None]
        if not goals:
            raise ValueError("Goal can never be reached (numeric condition)")
        self.schemas, self.goals = kept, goals
        return changed

    # ----- 类型收窄 -----

    def _fits(self, o: Obj, t: str) -> bool:
        return o.type in _MEMBERS[t] if t in _MEMBERS else o.ptype == t

    @staticmethod
    def _narrower(t: str, cur: str) -> bool:
        if t == cur or cur not in _MEMBERS:
            return False
        if t in _MEMBERS:
            return _MEMBERS[t] < _MEMBERS[cur]
        return "item" in _MEMBERS[cur]

    def _unary_members(self, pred: str) -> Optional[Set[str]]:
        """静态一元谓词成立的对象；会变的谓词返回 None。"""
        if pred in self._members:
            return self._members[pred]
        src = self.facts.get(pred)
        types = self.preds.get(pred) or []
        out: Optional[Set[str]] = None
        if src and len(types) == 1:
            if src[0] == "static":
                out = {o.id for o in self._of_type(types[0]) if _eval_static(src[1], o.name, o.props)}
            elif src[0] == "tools":
                out = set(src[1])
            elif src[0] == "pool":
                out = {o.id for o in self.objs.values() if o.pool == src[1]}
            elif src[0] == "me":
                out = {self.by_name[self.character]}
        self._members[pred] = out
        return out

    def _narrow(self) -> None:
        """静态一元条件 → 收窄参数类型（物品类别）/ 只剩一个对象就代入常量 / 删掉恒真条件。"""
        kept: List[Schema] = []
        for s in self.schemas:
            n = self._narrow_schema(s)
            if n is None:
                self.report.add(s.name, "", "pruned: no object satisfies its static conditions")
            else:
                kept.append(n)
        self.schemas = kept
        goals = [g for g in (self._narrow_schema(g) for g in self.goals) if g is not None]
        if not goals:
            raise ValueError("Goal is impossible in this world")
        self.goals = goals

    def _narrow_schema(self, s: Schema) -> Optional[Schema]:
        types: Dict[str, str] = {}
        for var, typ in s.params:
            types.setdefault(var, typ)
        static_on: Dict[str, List[Tuple[Lit, Set[str]]]] = {}
        rest: List[Lit] = []
        for lit in s.pre:
            args = lit.args()
            members = None
            if not lit.neg and len(args) == 1 and args[0] in types:
                members = self._unary_members(lit.sym)
            if members is None:
                rest.append(lit)
            else:
                static_on.setdefault(args[0], []).append((lit, members))
        subst: Dict[str, str] = {}
        for var, lits in static_on.items():
            allowed = {o.id for o in self.objs.values() if self._fits(o, types[var])}
            for _, members in lits:
                allowed &= members
            if not allowed:
                return None
            if len(allowed) == 1:
                subst[var] = next(iter(allowed))
                continue
            ptypes = {self.objs[i].ptype for i in allowed}
            if len(ptypes) == 1 and self._narrower(next(iter(ptypes)), types[var]):
                types[var] = ptypes.pop()
            fitting = {o.id for o in self.objs.values() if self._fits(o, types[var])}
            rest += [lit for lit, members in lits if not fitting <= members]

        def sub(text: str) -> str:
            for var, const in subst.items():
                text = re.sub(re.escape(var) + r"(?![\w-])", const, text)
            return text

        return replace(
            s,
            params=[(v, types[v]) for v in types if v not in subst],
            pre=[replace(l, text=sub(l.text)) for l in rest],
            eff=[replace(e, text=sub(e.text)) for e in s.eff],
            decode=[subst.get(t, t) for t in s.decode],
        )

    # ----- 对象剪枝 -----

    def _selective(self, pred: str) -> bool:
        """「属于某几类」这种挑对象的静态条件（不算 portable ≠ false 这类宽泛条件）。"""
        src = self.facts.get(pred) or ("",)
        if src[0] in ("tools", "pool"):
            return True

        def picks(c: Dict[str, Any]) -> bool:
            if c.get("op") == "any":
                return all(picks(x) for x in c.get("of") or [])
            return c.get("op") == "prop_in" or (c.get("op") == "prop_eq" and c.get("prop") == "item_kind")

        return src[0] == "static" and picks(src[1])

    def _hops(self, start: Optional[str]) -> Dict[str, int]:
        adj: Dict[str, List[str]] = {}
        for a, b in self.snap.edges.get("ADJACENT_TO", ()):
            adj.setdefault(a, []).append(b)
        dist = {start: 0} if start else {}
        queue = [start] if start else []
        while queue:
            cur = queue.pop(0)
            for nxt in adj.get(cur, []):
                if nxt not in dist:
                    dist[nxt] = dist[cur] + 1
                    queue.append(nxt)
        return dist

    def _prune_objects(self) -> None:
        """只留可能用得上的对象：被动作 / 目标点名的、类别被用到的、在范围内的。"""
        cfg = self.cfg
        schemas = self.all_schemas()
        consts: Set[str] = {self.by_name[self.character]}
        for s in schemas:
            for text in [l.text for l in s.pre] + [e.text for e in s.eff] + list(s.decode):
                consts |= {tok for tok in re.findall(r"[A-Za-z][\w-]*", text) if tok in self.objs}
        kinds: Set[str] = set()
        for s in schemas:
            kinds |= {t for _, t in s.params if t not in _MEMBERS}
            for l in s.pre:
                args = l.args()
                if not l.neg and len(args) == 1 and args[0].startswith("?") and self._selective(l.sym):
                    kinds |= {self.objs[i].ptype for i in self._unary_members(l.sym) or () if i in self.objs}

        snap = self.snap
        dist = self._hops(snap.location.get(self.character))
        far = 10 ** 6

        def item_dist(o: Obj) -> int:
            holder = snap.holder.get(o.name)
            if holder == self.character:
                return -1
            loc = snap.location.get(o.name) or snap.location.get(holder or "")
            return dist.get(loc, far)

        limit = far if cfg.max_hops is None else cfg.max_hops
        drop: Dict[str, str] = {}
        for o in self.objs.values():
            if o.id in consts:
                continue
            if o.type == "location":
                if dist.get(o.name, far) > limit:
                    drop[o.id] = "range"
            elif o.type == "person":
                if dist.get(snap.location.get(o.name), far) > limit:
                    drop[o.id] = "range"
            elif o.ptype not in kinds:
                drop[o.id] = "kind"
            elif not o.pool and item_dist(o) > limit:
                drop[o.id] = "range"
        if cfg.max_per_kind is not None:
            groups: Dict[str, List[Obj]] = {}
            for o in self.objs.values():
                if o.type == "item" and not o.pool and o.id not in consts and o.id not in drop:
                    groups.setdefault(o.ptype, []).append(o)
            for items in groups.values():
                items.sort(key=lambda o: (item_dist(o), o.name))
                for o in items[cfg.max_per_kind:]:
                    drop[o.id] = "cap"
        before = len(self.objs)
        for oid in drop:
            obj = self.objs.pop(oid)
            if not obj.pool:
                self.by_name.pop(obj.name, None)
        if drop:
            counts = {r: sum(1 for v in drop.values() if v == r) for r in ("kind", "range", "cap")}
            self.report.add(
                "objects", "",
                f"kept {len(self.objs)} of {before}: dropped {counts['kind']} of unused kinds, "
                f"{counts['range']} beyond max_hops, {counts['cap']} over max_per_kind",
            )

    # ----- 输出 -----

    def all_schemas(self) -> List[Schema]:
        return self.schemas + self.goals

    def _used(self) -> Set[str]:
        used: Set[str] = set()
        for s in self.all_schemas():
            used |= {l.sym for l in s.pre} | {e.sym for e in s.eff}
            for e in s.eff:
                used |= e.reads
        return used

    def domain(self, name: str = "aitown3-compiled") -> str:
        used = self._used()
        numeric = any(f in used for f in self.fluents)
        reqs = ":typing :negative-preconditions :equality" + (" :numeric-fluents" if numeric else "")
        by_type: Dict[str, List[str]] = {}
        for o in self.objs.values():
            by_type.setdefault(o.ptype, []).append(o.id)
        consts = "\n    ".join(f"{' '.join(ids)} - {t}" for t, ids in sorted(by_type.items()))
        kinds = set(by_type) | {t for s in self.all_schemas() for _, t in s.params}
        kind_types = " ".join(sorted(t for t in kinds if t not in _MEMBERS))
        types = "thing location - object person item - thing" + (f" {kind_types} - item" if kind_types else "")
        preds = "\n    ".join(
            f"({p}" + "".join(f" ?a{i} - {t}" for i, t in enumerate(types)) + ")"
            for p, types in sorted(self.preds.items()) if p in used
        )
        funcs = "\n    ".join(f"({f} ?x - {t})" for f, (t, _) in sorted(self.fluents.items()) if f in used)
        out = [
            f"(define (domain {name})",
            f"  (:requirements {reqs})",
            f"  (:types {types})",
            f"  (:constants\n    {consts})",
            f"  (:predicates\n    {preds})",
        ]
        if funcs:
            out.append(f"  (:functions\n    {funcs})")
        for s in self.all_schemas():
            seen: Dict[str, str] = {}
            for var, typ in s.params:
                seen.setdefault(var, typ)
            params = " ".join(f"{v} - {t}" for v, t in seen.items())
            pre = " ".join(l.pddl() for l in s.pre)
            eff = " ".join(dict.fromkeys(e.text for e in s.eff))
            out.append(
                f"  (:action {s.name}\n    :parameters ({params})\n"
                f"    :precondition (and {pre})\n    :effect (and {eff}))"
            )
        return "\n".join(out) + ")\n"

    def _facts(self, pred: str) -> List[Tuple[str, ...]]:
        """谓词在初始状态里成立的参数组（对象 id）。"""
        if pred in self._fact_cache:
            return self._fact_cache[pred]
        src = self.facts.get(pred) or ("",)
        kind = src[0]
        typ = self.preds[pred][0] if self.preds.get(pred) else ""
        snap, ids = self.snap, self.by_name
        out: List[Tuple[str, ...]] = []
        if kind == "rel":
            rel = src[1]
            if rel == "LOCATED_AT":
                pairs = list(snap.location.items())
            elif rel == "HAS_ITEM":
                pairs = [(h, i) for i, h in snap.holder.items()]
            else:
                pairs = list(snap.edges.get(rel, ()))
            out = [(ids[a], ids[b]) for a, b in sorted(pairs) if a in ids and b in ids]
        elif kind == "held":
            out = [(ids[i],) for i, h in sorted(snap.holder.items()) if i in ids and h in ids]
        elif kind == "bool":
            out = [(o.id,) for o in self._of_type(typ) if o.props.get(src[1])]
        elif kind == "sym":
            out = [(o.id,) for o in self._of_type(typ) if str(o.props.get(src[1]) or "").lower() == str(src[2]).lower()]
        elif kind == "static":
            out = [(o.id,) for o in self._of_type(typ) if _eval_static(src[1], o.name, o.props)]
        elif kind == "tools":
            out = [(i,) for i in sorted(src[1]) if i in self.objs]
        elif kind == "pool":
            out = [(o.id,) for o in self.objs.values() if o.pool == src[1]]
        elif kind == "unspawned":
            out = [(o.id,) for o in self.objs.values() if o.pool]
        elif kind == "accepts":
            out = [(t, i) for t, i in sorted(self.accepts) if t in self.objs and i in self.objs]
        elif kind == "me":
            out = [(ids[self.character],)]
        elif kind == "goal":
            out = []
        self._fact_cache[pred] = out
        return out

    def problem(self, domain_name: str = "aitown3-compiled") -> str:
        self._fact_cache.clear()
        used = self._used()
        init: List[str] = []
        for p in sorted(self.preds):
            if p in used:
                init += [f"({p} {' '.join(args)})" for args in self._facts(p)]
        for f, (typ, prop) in sorted(self.fluents.items()):
            if f not in used:
                continue
            for o in self._of_type(typ):
                try:
                    value = float(o.props.get(prop) or 0)
                except (TypeError, ValueError):
                    value = 0.0
                init.append(f"(= ({f} {o.id}) {_num(value)})")
        body = "\n    ".join(init)
        return (
            f"(define (problem {self.character.lower()}-goal)\n"
            f"  (:domain {domain_name})\n"
            f"  (:init\n    {body})\n"
            f"  (:goal (goal-reached)))\n"
        )

    def decode(self, plan: List[PlanStep]) -> List[PlanStep]:
        """规划结果 → 游戏动作；尚未出生的池对象写成 kind:<kind>，执行时绑定。"""
        by_name = {s.name.lower(): s for s in self.all_schemas()}
        by_id = {oid.lower(): o for oid, o in self.objs.items()}
        out: List[PlanStep] = []
        for name, args in plan:
            s = by_name.get(str(name).lower())
            if s is None or s.goal:
                continue
            bind: Dict[str, str] = {}
            for (var, _), arg in zip(dict.fromkeys(s.params), args):
                bind.setdefault(var, str(arg))
            real: List[str] = []
            for t in s.decode:
                obj = by_id.get(bind.get(t, t).lower())
                if obj is None:
                    real.append(bind.get(t, t))
                elif obj.pool:
                    real.append(f"kind:{self._kind(obj) or obj.name}")
                else:
                    real.append(obj.name)
            out.append((s.source, real))
        return out


# ---------- 对外 API ----------

@dataclass
class Compiled:
    domain: str
    problem: str
    report: CompileReport
    compiler: Compiler

    def decode(self, plan: List[PlanStep]) -> List[PlanStep]:
        return self.compiler.decode(plan)

    def action_names(self) -> List[str]:
        return [s.name for s in self.compiler.schemas]


def _as_goal(goal: Any) -> List[Dict[str, Any]]:
    if isinstance(goal, dict):
        return [goal]
    return [dict(c) for c in goal or []]


def compile_pddl(
    snap: Snapshot,
    character: str,
    goal: Any,
    *,
    config: Optional[PlanningConfig] = None,
    content=None,
) -> Compiled:
    from content import get_content

    comp = Compiler(snap, character, config or load_config(), content or get_content())
    comp.compile(_as_goal(goal))
    return Compiled(comp.domain(), comp.problem(), comp.report, comp)


def plan_compiled(
    world,
    character: str,
    goal: Any,
    *,
    overrides: Optional[Dict[str, Any]] = None,
    snapshot: Optional[Snapshot] = None,
) -> Dict[str, Any]:
    """读世界 → 编译 → 求解 → 还原为游戏动作。"""
    from solver import solve_pddl

    try:
        compiled = compile_pddl(
            snapshot or load_snapshot(world), character, goal, config=load_config(overrides)
        )
    except ValueError as e:
        return {"success": False, "error": str(e), "plan": [], "domain": "", "problem": "", "report": ""}
    result = solve_pddl(compiled.domain, compiled.problem, engines=["unified_planning"])
    return {
        "success": bool(result.get("success")),
        "error": result.get("error"),
        "engine": result.get("engine", ""),
        "plan": compiled.decode(result.get("plan") or []) if result.get("success") else [],
        "raw_plan": result.get("plan") or [],
        "domain": compiled.domain,
        "problem": compiled.problem,
        "report": compiled.report.text(),
    }
