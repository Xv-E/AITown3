"""
规则书：把世界交给内容包的 Prolog 规则，再把规则的回答交回引擎。不认识任何玩法。

  rule_files()                          引擎核心 + 各启用内容包的 rules/*.pl（按包的加载顺序）
  ask(world, template, goal)            问规则：findall(template, goal) 的结果（转成 Python 值）
  after(world, action, params)          动作做完：规则推出的效果交给 graph_ops 写图；note / report 收集起来返回
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


def _reads(files: Tuple[str, ...]) -> Tuple[List[str], List[str]]:
    """规则声明要读的 (标签, 关系类型)。"""
    if files not in _reads_cache:
        rows = solve(files, [], "answers_json(X, reads(X), J)")
        decl = json.loads(rows[0]["J"]) if rows else []
        labels = sorted({d["args"][0] for d in decl if d.get("functor") == "label"})
        rels = sorted({d["args"][0] for d in decl if d.get("functor") == "rel"})
        _reads_cache[files] = (labels, rels)
    return _reads_cache[files]


def _value(v: Any) -> Optional[str]:
    """属性值 → Prolog；None 和嵌套结构不进规则。"""
    if v is None or isinstance(v, dict):
        return None
    if isinstance(v, (list, tuple)):
        return atom(list(v)) if all(isinstance(x, (str, int, float, bool)) for x in v) else None
    return atom(v)


def world_facts(world, files: Tuple[str, ...], *, at: Optional[float] = None,
                sees: Iterable[Tuple[str, str]] = ()) -> List[str]:
    labels, rels = _reads(files)
    facts: List[str] = []
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
    facts += [fact("sees", w, p) for w, p in sees]
    return facts


def ask(world, template: str, goal: str, *, at: Optional[float] = None,
        sees: Iterable[Tuple[str, str]] = ()) -> List[Any]:
    """findall(template, goal) 在当下的世界上；复合项变成 {"functor", "args"}，K-V 变成 [K, V]。"""
    files = rule_files()
    rows = solve(files, world_facts(world, files, at=at, sees=sees),
                 f"answers_json({template}, ({goal}), J)")
    return json.loads(rows[0]["J"]) if rows else []


def watched_by(world, actor: str) -> List[Tuple[str, str]]:
    """引擎的感知：现在看得见 actor 的人（作为 sees/2 交给规则）。"""
    from ownership import witnesses

    return [(w, actor) for w in witnesses(world, actor)]


def event(action: str, params: Dict[str, Any]) -> str:
    from content import get_content

    content = get_content()
    spec = content.actions.get(action) or {}
    names = [p.get("name") for p in spec.get("params") or []] or list(params)
    head = content.capability_of(action) or action
    return f"{atom(head)}({', '.join(atom(params.get(n, '')) for n in names)})"


def after(world, action: str, params: Dict[str, Any], *, at: float = 0.0) -> Dict[str, Any]:
    """动作做完之后，规则推出的效果：写图的交给 graph_ops；note 收进 notes，report 并进返回值。"""
    from graph_ops import apply_op

    actor = str(next(iter(params.values()), "")) if params else ""
    effects = ask(world, "E", f"after({event(action, params)}, E)", at=at,
                  sees=watched_by(world, actor) if actor else ())
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
