"""
规则书：把世界交给内容包的 Prolog 规则，再把规则的回答交回引擎。不认识任何玩法。

  rule_files()                          引擎核心 + 各启用内容包的 rules/*.pl（按包的加载顺序）
  ask(world, template, goal)            问规则：findall(template, goal) 的结果（转成 Python 值）
  after(world, action, params)          动作做完：规则推出的效果交给 graph_ops 写图；note / report 收集起来返回
  tick(world, prev, now)                时间过去：tick/1 的效果同样写图（只在跨过规则声明的周期时才问）
  event(action, params)                 事件项：能力名（没有就用动作名）+ 参数（按动作声明的顺序）
  want(world, me) / context(world, me[, other])   NPC 想做的事 / 提示词里的几行
  const(name, default)                  规则里的常量

效果除了 graph_ops 的写图 op，还有：note / report / announce（新闻）/ remember（记忆）/ extend（Python 扩展）。

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


# 一拍之内（world.read_cache() 作用域里）：每个标签 / 关系类型的事实各自记着，
# 只有写过这个标签 / 关系类型（或全局的写）之后才重读这一份（缓存代数见 Neo4jWorld.invalidate_cache）
_parts_memo: Dict[Any, Tuple[Any, List[str]]] = {}


def world_facts(world, files: Tuple[str, ...], *, at: Optional[float] = None,
                sees: Iterable[Tuple[str, str]] = (), extra: Iterable[str] = ()) -> List[str]:
    return _graph_facts(world, files, at) + [fact("g_sees", w, p) for w, p in sees] + list(extra)


def _version(world, token: str) -> Any:
    tok_gen, global_gen = getattr(world, "_tok_gen", None), getattr(world, "_global_gen", None)
    if tok_gen is None or global_gen is None or not getattr(world, "_cache_depth", 0):
        return None
    return (id(world), global_gen, tok_gen.get(token, 0))


def _part(world, key: Tuple[str, str], build) -> List[str]:
    version = _version(world, key[1])
    if version is not None:
        hit = _parts_memo.get(key)
        if hit is not None and hit[0] == version:
            return hit[1]
    facts = build()
    if version is not None:
        _parts_memo[key] = (version, facts)
    return facts


def _graph_facts(world, files: Tuple[str, ...], at: Optional[float]) -> List[str]:
    labels, partial, rels = _reads(files)
    facts: List[str] = []
    for lab, keys in partial.items():
        facts += _part(world, ("some", lab), lambda lab=lab, keys=keys: _some_facts(world, lab, keys))
    for lab in labels:
        facts += _part(world, ("label", lab), lambda lab=lab: _label_facts(world, lab, labels))
    for t in rels:
        facts += _part(world, ("rel", t), lambda t=t: _rel_facts(world, t))
    if at is None:
        rows = world.run_query("MATCH (m:WorldMeta {name: 'WorldMeta'}) RETURN m.sim_time AS t LIMIT 1") or []
        at = rows[0]["t"] if rows and rows[0].get("t") is not None else None
    if at is not None:
        facts.append(fact("g_now", float(at)))
    facts.append(_day_fact())
    return facts


def _some_facts(world, lab: str, keys: List[str]) -> List[str]:
    out: List[str] = []
    for r in world.run_query(
        f"MATCH (n:`{lab}`) WHERE n.name IS NOT NULL RETURN n.name AS id, [k IN $keys | [k, n[k]]] AS kv", keys=keys,
    ) or []:
        out.append(fact("g_node", lab, r["id"]))
        for k, v in r["kv"] or []:
            pv = _value(v)
            if pv is not None:
                out.append(f"g_prop({atom(r['id'])}, {atom(k)}, {pv})")
    return out


def _label_facts(world, lab: str, labels: List[str]) -> List[str]:
    """整个读的标签：节点和全部属性（一个节点有好几个要读的标签时，属性只在排第一的标签下记一次）。"""
    out: List[str] = []
    for r in world.run_query(
        f"MATCH (n:`{lab}`) WHERE n.name IS NOT NULL RETURN labels(n) AS ls, n.name AS id, properties(n) AS props"
    ) or []:
        out.append(fact("g_node", lab, r["id"]))
        if min(x for x in r["ls"] if x in labels) != lab:
            continue
        for k, v in (r["props"] or {}).items():
            pv = _value(v)
            if pv is not None and k != "name":
                out.append(f"g_prop({atom(r['id'])}, {atom(k)}, {pv})")
    return out


def _rel_facts(world, t: str) -> List[str]:
    out: List[str] = []
    for r in world.run_query(
        f"MATCH (a)-[r:`{t}`]->(b) WHERE a.name IS NOT NULL AND b.name IS NOT NULL "
        "RETURN a.name AS a, b.name AS b, properties(r) AS props"
    ) or []:
        pairs = [f"{atom(k)}-{_value(v)}" for k, v in (r["props"] or {}).items() if _value(v) is not None]
        out.append(f"g_rel({atom(t)}, {atom(r['a'])}, {atom(r['b'])}, [{', '.join(pairs)}])")
    return out


def _day_fact() -> str:
    try:
        from routine import DAY_LENGTH_SECONDS

        return fact("g_day_length", float(DAY_LENGTH_SECONDS))
    except Exception:
        return fact("g_day_length", 300.0)


# 记忆（remember 效果写到这里）；游戏循环 / 服务启动时交进来
_memory: List[Any] = [None]


def use_memory(memory) -> None:
    _memory[0] = memory


# extend 效果的处理函数：name -> fn(world, effect) -> 更多效果（可以没有）
EXTENSIONS: Dict[str, Any] = {}


def extension(name: str):
    def deco(fn):
        EXTENSIONS[name] = fn
        return fn
    return deco


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


def happen(world, name: str, *args: Any, at: Optional[float] = None) -> Dict[str, Any]:
    """不是动作的事（谈完一场话、兑现了人情……）：问 after(name(args...), E)，效果照常生效。"""
    term = f"{atom(name)}({', '.join(atom(a) for a in args)})" if args else atom(name)
    return apply_effects(world, ask(world, "E", f"after({term}, E)", at=at))


def _periods(files: Tuple[str, ...]) -> List[float]:
    key = ("tick_period",) + files
    if key not in _reads_cache:
        rows = solve(files, [_day_fact()], "answers_json(P, tick_period(P), J)")
        _reads_cache[key] = ([float(p) for p in json.loads(rows[0]["J"])] if rows else [], [])
    return _reads_cache[key][0]


MAX_CATCHUP = 8  # 跳时间时最多补算几段（每段跨过一个周期边界）


def tick(world, prev: float, now: float) -> Dict[str, Any]:
    """时间从 prev 走到 now：问规则 tick/1，再按 tick_phase 逐步问 tick/2，效果照常写图。
    规则声明了周期时，没跨过任何周期就不问；一下跨过好几个边界（跳时间）就逐段补算，最多 MAX_CATCHUP 段。"""
    import math

    periods = [p for p in _periods(rule_files()) if p > 0]
    if periods:
        marks = sorted({k * p for p in periods
                        for k in range(math.floor(prev / p) + 1, math.floor(now / p) + 1)})
        if not marks:
            return {}
        marks = marks[-MAX_CATCHUP:]
        starts = [prev] + marks[:-1]
        ends = marks[:-1] + [now]
    else:
        starts, ends = [prev], [now]
    from contextlib import nullcontext

    out: Dict[str, Any] = {}
    read_cache = getattr(world, "read_cache", None)
    with read_cache() if read_cache else nullcontext():  # 没有写的步骤之间复用同一份事实
        for a, b in zip(starts, ends):
            _merge(out, _tick_once(world, a, b))
    return out


def _tick_once(world, prev: float, now: float) -> Dict[str, Any]:
    window = [fact("g_tick", float(prev), float(now))]
    out = apply_effects(world, ask(world, "E", "tick(E)", at=now, extra=window))
    for phase in _phases(rule_files()):  # 每一步之前重读世界：后一步看得到前一步的结果
        _merge(out, apply_effects(world, ask(world, "E", f"tick({atom(phase)}, E)", at=now, extra=window)))
    return out


def _merge(out: Dict[str, Any], more: Dict[str, Any]) -> None:
    for k, v in more.items():
        if isinstance(v, list) and isinstance(out.get(k), list):
            out[k] += v
        else:
            out[k] = v


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


def want(world, me: str, *, at: Optional[float] = None) -> List[Dict[str, Any]]:
    """这个 NPC 想做的事（规则 want/2）：[{"goal", "target_entity", "reason", "priority", "source"}, ...]。"""
    out = []
    for g in ask(world, "G", f"want({atom(me)}, G)", at=at):
        if isinstance(g, dict) and g.get("goal"):
            g = dict(g)
            g.setdefault("target_entity", g.pop("target", ""))
            out.append(g)
    return out


def context(world, me: str, other: Optional[str] = None) -> List[str]:
    """规则给 me 的提示词加的几行：context(Me, Text)，或面对 other 时的 context(Me, Other, Text)。"""
    goal = f"context({atom(me)}, T)" if other is None else f"context({atom(me)}, {atom(other)}, T)"
    seen: List[str] = []
    for t in ask(world, "T", goal):
        if str(t) not in seen:
            seen.append(str(t))
    return seen


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
            _merge(out, {k: v for k, v in e.items() if k != "op"})
        elif op == "announce":
            out.setdefault("events", []).append({"type": "announce", "text": str(e.get("text") or ""),
                                                 "source": str(e.get("source") or "rules")})
        elif op == "remember":
            _remember(e)
        elif op == "extend":
            fn = EXTENSIONS.get(str(e.get("name") or ""))
            if fn is None:
                _load_extensions()
                fn = EXTENSIONS.get(str(e.get("name") or ""))
            if fn is not None:
                _merge(out, apply_effects(world, list(fn(world, e) or [])))
        else:
            apply_op(world, e, {})
    return out


def _remember(e: Dict[str, Any]) -> None:
    memory = _memory[0]
    who = str(e.get("who") or "")
    if memory is None or not who:
        return
    meta = {k: v for k, v in e.items() if k not in ("op", "who", "text") and isinstance(v, (str, int, float, bool))}
    meta.setdefault("type", "event")
    try:
        memory.add(who, str(e.get("text") or ""), **meta)
    except Exception:
        pass


def _load_extensions() -> None:
    """扩展写在 agent/rule_extensions.py（引擎能力，如 LLM 写故事）；用到时才导入。"""
    try:
        import rule_extensions  # noqa: F401
    except ImportError:
        pass


def const(name: str, default: Any = None) -> Any:
    files = rule_files()
    rows = solve(files, [], f"answers_json(V, const({atom(name)}, V), J)")
    values = json.loads(rows[0]["J"]) if rows else []
    return values[0] if values else default
