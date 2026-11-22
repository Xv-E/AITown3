"""
规则书：把世界交给内容包的 Prolog 规则，再把规则的回答交回引擎。不认识任何玩法。

  rule_files()                          引擎核心 + 各启用内容包的 rules/*.pl（按包的加载顺序）
  ask(world, template, goal)            问规则：findall(template, goal) 的结果（转成 Python 值）
  after(world, action, params)          动作做完：规则推出的效果交给 graph_ops 写图；note / report 收集起来返回
  tick(world, prev, now)                时间过去：tick/1 的效果同样写图（只在跨过规则声明的周期时才问）
  event(action, params)                 事件项：能力名（没有就用动作名）+ 参数（按动作声明的顺序）
  const(name, default)                  规则里的常量

世界怎么变成事实见 agent/rules_core.pl 开头：读哪些标签 / 关系由规则自己声明（reads/1）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from prolog_engine import atom, fact, solve

CORE = Path(__file__).resolve().with_name("rules_core.pl")


def rule_files() -> Tuple[str, ...]:
    from content import CONTENT_ROOT, get_content

    files = [CORE.as_posix()]
    for pack in get_content().packs:
        files += [p.as_posix() for p in sorted((CONTENT_ROOT / pack / "rules").glob("*.pl"))]
    return tuple(files)


_reads_cache: Dict[Tuple[str, ...], Tuple[List[str], List[str]]] = {}


def _reads(files: Tuple[str, ...]) -> Tuple[List[str], Dict[str, List[str]], List[str]]:
    """规则声明要读的 (整个读的标签, {只读部分属性的标签: 属性}, 关系类型)。
    reads(label(L)) 读全部属性；reads(label(L, [K, ...])) 只读这些属性（如物品只要 damage / armor）。"""
    if files not in _reads_cache:
        rows = solve(files, [], "answers_json(X, reads(X), J)")
        decl = [d for d in (json.loads(rows[0]["J"]) if rows else []) if isinstance(d, dict)]
        full = {d["args"][0] for d in decl if d.get("functor") == "label" and len(d["args"]) == 1}
        some: Dict[str, set] = {}
        for d in decl:
            if d.get("functor") == "label" and len(d["args"]) == 2 and d["args"][0] not in full:
                some.setdefault(d["args"][0], set()).update(d["args"][1])
        rels = sorted({d["args"][0] for d in decl if d.get("functor") == "rel"})
        _reads_cache[files] = (sorted(full), {k: sorted(v) for k, v in some.items()}, rels)
    return _reads_cache[files]


def _value(v: Any) -> Optional[str]:
    """属性值 → Prolog；None 和嵌套结构不进规则。"""
    if v is None or isinstance(v, dict):
        return None
    if isinstance(v, (list, tuple)):
        return atom(list(v)) if all(isinstance(x, (str, int, float, bool)) for x in v) else None
    return atom(v)


def world_facts(world, files: Tuple[str, ...], *, at: Optional[float] = None,
                sees: Iterable[Tuple[str, str]] = (), extra: Iterable[str] = ()) -> List[str]:
    labels, partial, rels = _reads(files)
    facts: List[str] = []
    for lab, keys in partial.items():
        for r in world.run_query(
            f"MATCH (n:`{lab}`) WHERE n.name IS NOT NULL RETURN n.name AS id, [k IN $keys | [k, n[k]]] AS kv",
            keys=keys,
        ) or []:
            facts.append(fact("g_node", lab, r["id"]))
            for k, v in r["kv"] or []:
                pv = _value(v)
                if pv is not None:
                    facts.append(f"g_prop({atom(r['id'])}, {atom(k)}, {pv})")
    if labels:
        for r in world.run_query(
            "MATCH (n) WHERE n.name IS NOT NULL AND any(l IN labels(n) WHERE l IN $labels) "
            "RETURN labels(n) AS ls, n.name AS id, properties(n) AS props",
            labels=labels,
        ) or []:
            for lab in r["ls"]:
                if lab in labels:
                    facts.append(fact("g_node", lab, r["id"]))
            for k, v in (r["props"] or {}).items():
                pv = _value(v)
                if pv is not None and k != "name":
                    facts.append(f"g_prop({atom(r['id'])}, {atom(k)}, {pv})")
    if rels:
        for r in world.run_query(
            "MATCH (a)-[r]->(b) WHERE type(r) IN $types AND a.name IS NOT NULL AND b.name IS NOT NULL "
            "RETURN type(r) AS t, a.name AS a, b.name AS b, properties(r) AS props",
            types=rels,
        ) or []:
            pairs = [f"{atom(k)}-{_value(v)}" for k, v in (r["props"] or {}).items() if _value(v) is not None]
            facts.append(f"g_rel({atom(r['t'])}, {atom(r['a'])}, {atom(r['b'])}, [{', '.join(pairs)}])")
    if at is None:
        rows = world.run_query("MATCH (m:WorldMeta {name: 'WorldMeta'}) RETURN m.sim_time AS t LIMIT 1") or []
        at = rows[0]["t"] if rows and rows[0].get("t") is not None else None
    if at is not None:
        facts.append(fact("g_now", float(at)))
    facts += [fact("g_sees", w, p) for w, p in sees]
    facts += list(extra)
    return facts


def ask(world, template: str, goal: str, *, at: Optional[float] = None,
        sees: Iterable[Tuple[str, str]] = (), extra: Iterable[str] = ()) -> List[Any]:
    """findall(template, goal) 在当下的世界上；复合项变成 {"functor", "args"}，K-V 变成 [K, V]。
    sees：指定谁看得见谁（不给就按需问引擎的感知 perceive）。"""
    files = rule_files()
    rows = solve(files, world_facts(world, files, at=at, sees=sees, extra=extra),
                 f"answers_json({template}, ({goal}), J)", callbacks={"sees": lambda p: perceive(world, p)})
    return json.loads(rows[0]["J"]) if rows else []


def perceive(world, actor: str) -> List[str]:
    """引擎的感知：现在看得见 actor 的人（规则的 sees(W, actor) 问到时才算）。"""
    from ownership import witnesses

    return witnesses(world, actor)


def event(action: str, params: Dict[str, Any]) -> str:
    from content import get_content

    content = get_content()
    spec = content.actions.get(action) or {}
    names = [p.get("name") for p in spec.get("params") or []] or list(params)
    head = content.capability_of(action) or action
    return f"{atom(head)}({', '.join(atom(params.get(n, '')) for n in names)})"


def after(world, action: str, params: Dict[str, Any], *, at: float = 0.0) -> Dict[str, Any]:
    """动作做完之后，规则推出的效果：写图的交给 graph_ops；note 收进 notes，report 并进返回值。"""
    effects = ask(world, "E", f"after({event(action, params)}, E)", at=at)
    return apply_effects(world, effects)


def _periods(files: Tuple[str, ...]) -> List[float]:
    key = ("tick_period",) + files
    if key not in _reads_cache:
        rows = solve(files, [], "answers_json(P, tick_period(P), J)")
        _reads_cache[key] = ([float(p) for p in json.loads(rows[0]["J"])] if rows else [], [])
    return _reads_cache[key][0]


def tick(world, prev: float, now: float) -> Dict[str, Any]:
    """时间从 prev 走到 now：问规则 tick/1，效果照常写图。规则声明了周期时，没跨过任何周期就不问。"""
    import math

    periods = _periods(rule_files())
    if periods and not any(math.floor(now / p) > math.floor(prev / p) for p in periods if p > 0):
        return {}
    window = [fact("g_tick", float(prev), float(now))]
    out = apply_effects(world, ask(world, "E", "tick(E)", at=now, extra=window))
    for phase in _phases(rule_files()):  # 每一步之前重读世界：后一步看得到前一步的结果
        out.update(apply_effects(world, ask(world, "E", f"tick({atom(phase)}, E)", at=now, extra=window)))
    return out


def _phases(files: Tuple[str, ...]) -> List[str]:
    key = ("tick_phase",) + files
    if key not in _reads_cache:
        rows = solve(files, [], "answers_json([O, P], tick_phase(O, P), J)")
        _reads_cache[key] = ([p for _, p in sorted(json.loads(rows[0]["J"]) if rows else [])], [])
    return _reads_cache[key][0]


def react(world, me: str, *, at: Optional[float] = None, busy_since: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """这个 NPC 现在要不要丢下手上的事、马上行动（规则 react/2）：
    {"goal", "target", "why", "text", "source", "plan": [(动作名, 参数), ...]}；None = 不用。
    计划里写的是能力名（travel、attack……），这里换成当前内容包的动作名。"""
    from registry import act

    extra = [fact("busy_since", me, float(busy_since))] if busy_since is not None else []
    rows = ask(world, "C", f"once(react({atom(me)}, C))", at=at, extra=extra)
    if not rows or not isinstance(rows[0], dict):
        return None
    choice = dict(rows[0])
    choice["plan"] = [(act(str(cap)) or str(cap), [str(a) for a in args]) for cap, args in choice.get("plan") or []]
    return choice


def holds(world, goal: str) -> bool:
    """规则里的 goal 现在成立吗（动作条件的 holds op 用）。"""
    return bool(ask(world, "x", goal))


def pure(template: str, goal: str) -> List[Any]:
    """只用规则、不读世界的提问（如把数值说成词）。"""
    rows = solve(rule_files(), [], f"answers_json({template}, ({goal}), J)")
    return json.loads(rows[0]["J"]) if rows else []


def apply_effects(world, effects: List[Any]) -> Dict[str, Any]:
    from graph_ops import apply_op

    out: Dict[str, Any] = {}
    for e in effects:
        if not isinstance(e, dict):
            continue
        op = e.get("op")
        if op == "note":
            out.setdefault("notes", []).append(str(e.get("text") or ""))
        elif op == "report":
            out.update({k: v for k, v in e.items() if k != "op"})
        else:
            apply_op(world, e, {})
    return out


def const(name: str, default: Any = None) -> Any:
    files = rule_files()
    rows = solve(files, [], f"answers_json(V, const({atom(name)}, V), J)")
    values = json.loads(rows[0]["J"]) if rows else []
    return values[0] if values else default
