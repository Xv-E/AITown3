"""
RPG 系统的内容展开：采集点、配方写成简单的 JSON，加载时展开成普通的数据动作。

展开后的动作和 move / pick_up 一样只用通用的条件 / 图操作，所以：
  - 玩家菜单、LLM、routine 都能调用；
  - planner/compiler.py 自动把它们编译进 PDDL —— NPC 也会自己采集、制造。

content/<pack>/gathering/*.json
  {"id": "chop", "label": "Chop logs", "node": "tree", "tool": "axe", "stock": "stock",
   "yield": {"archetype": "log"}, "duration": 2}
  → 动作 gather_chop(c, node, [tool], l)：站在采集点旁、（拿着工具）、采集点还有存量 →
    存量 -1，产物到手上。存量怎么恢复写在采集点原型的 script.on_tick 里。

content/<pack>/recipes/*.json
  {"id": "stone_axe", "label": "Stone axe", "inputs": [{"kind": "stone"}, {"kind": "firewood"}],
   "station": "workbench", "output": {"archetype": "axe"}, "duration": 3}
  → 动作 craft_stone_axe(c, in1, in2, [station], l)：手上有这些材料（各是不同的物品）、
    （在工作台旁）→ 材料用掉，产物到手上。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

GATHER_PREFIX = "gather_"


def _a(word: str) -> str:
    return f"an {word}" if word[:1].lower() in "aeiou" else f"a {word}"


CRAFT_PREFIX = "craft_"
MAX_INPUTS = 6


def _here(person: str, location: str) -> Dict[str, Any]:
    return {"op": "person_at", "person": person, "location": location}


def _kind_is(entity: str, kind: str, message: str = "") -> Dict[str, Any]:
    cond = {"op": "prop_eq", "entity": entity, "prop": "item_kind", "value": kind}
    if message:
        cond["message"] = message
    return cond


def expand_gather(spec: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    gid = str(spec.get("id") or "").strip()
    node = str(spec.get("node") or "").strip().lower()
    out = spec.get("yield") or {}
    if not gid or not node or not (out.get("archetype") or out.get("item_kind")):
        return None, "gathering needs id, node (kind) and yield.archetype"
    label = str(spec.get("label") or gid.replace("_", " ").title())
    tool = str(spec.get("tool") or "").strip().lower()
    stock = str(spec.get("stock") or "").strip()
    params = [
        {"name": "c", "type": "Person", "source": "self"},
        {"name": "node", "type": "Item", "source": "ground"},
    ]
    requires: List[Dict[str, Any]] = [
        _here("@c", "@l"),
        {"op": "item_at", "entity": "@node", "location": "@l"},
        _kind_is("@node", node, f"@node is not {_a(node)}"),
    ]
    if stock:
        requires.append({"op": "prop_gte", "entity": "@node", "prop": stock, "value": 1,
                         "message": str(spec.get("empty_message") or "@node is used up for now; come back later")})
    if tool:
        params.append({"name": "tool", "type": "Item", "source": "held"})
        requires += [
            {"op": "has_item", "person": "@c", "item": "@tool", "message": f"You need {_a(tool)}"},
            _kind_is("@tool", tool, f"You need {_a(tool)}"),
        ]
    params.append({"name": "l", "type": "Location", "source": "here"})
    effects: List[Dict[str, Any]] = []
    if stock:
        effects.append({"op": "add_prop", "entity": "@node", "prop": stock, "delta": -1, "min": 0})
    spawn = {"op": "spawn_item", "held_by": "@c"}
    spawn.update({k: v for k, v in out.items() if k in ("archetype", "name", "item_kind")})
    effects.append(spawn)
    return {
        "name": GATHER_PREFIX + gid,
        "category": "gather",
        "label": label,
        "description": str(spec.get("description") or f"{label} from a {node}"),
        "duration": float(spec.get("duration", 2.0)),
        "params": params,
        "requires": requires,
        "effects": effects,
        "narrative": str(spec.get("narrative") or f"@c: {label.lower()}."),
        "rpg": {"system": "gather", "id": gid, "node": node, "tool": tool, "stock": stock,
                "yield": dict(out), "label": label},
    }, ""


def recipe_inputs(spec: Dict[str, Any]) -> List[str]:
    """材料展开成一件一件的类别：[{"kind": "stone", "count": 2}] → ["stone", "stone"]。"""
    kinds: List[str] = []
    for inp in spec.get("inputs") or []:
        if isinstance(inp, str):
            inp = {"kind": inp}
        kind = str(inp.get("kind") or "").strip().lower()
        kinds += [kind] * max(1, int(inp.get("count", 1) or 1)) if kind else []
    return kinds


def expand_recipe(spec: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    rid = str(spec.get("id") or "").strip()
    out = spec.get("output") or {}
    kinds = recipe_inputs(spec)
    if not rid or not (out.get("archetype") or out.get("item_kind")):
        return None, "recipe needs id and output.archetype"
    if not kinds or len(kinds) > MAX_INPUTS:
        return None, f"recipe {rid}: 1-{MAX_INPUTS} input items"
    label = str(spec.get("label") or rid.replace("_", " ").title())
    station = str(spec.get("station") or "").strip().lower()
    params = [{"name": "c", "type": "Person", "source": "self"}]
    requires: List[Dict[str, Any]] = []
    effects: List[Dict[str, Any]] = []
    names = [f"in{i + 1}" for i in range(len(kinds))]
    for name, kind in zip(names, kinds):
        params.append({"name": name, "type": "Item", "source": "held"})
        requires += [
            {"op": "has_item", "person": "@c", "item": f"@{name}", "message": f"You need {kind}"},
            _kind_is(f"@{name}", kind, f"You need {kind}"),
        ]
    # 同类的材料要是不同的物品（两块石头不能是同一块）
    for i, a in enumerate(names):
        for j in range(i + 1, len(names)):
            if kinds[i] == kinds[j]:
                requires.append({"op": "neq", "a": f"@{a}", "b": f"@{names[j]}",
                                 "message": f"You need {kinds.count(kinds[i])} {kinds[i]}"})
    if station:
        params.append({"name": "station", "type": "Item", "source": "ground"})
        requires += [
            {"op": "item_at", "entity": "@station", "location": "@l", "message": f"You need to be at {_a(station)}"},
            _kind_is("@station", station, f"You need to be at {_a(station)}"),
        ]
    params.append({"name": "l", "type": "Location", "source": "here"})
    requires.insert(0, _here("@c", "@l"))
    effects += [{"op": "delete_entity", "entity": f"@{n}"} for n in names]
    spawn = {"op": "spawn_item", "held_by": "@c"}
    spawn.update({k: v for k, v in out.items() if k in ("archetype", "name", "item_kind")})
    effects.append(spawn)
    return {
        "name": CRAFT_PREFIX + rid,
        "category": "craft",
        "label": label,
        "description": str(spec.get("description") or f"Make {label}"),
        "duration": float(spec.get("duration", 3.0)),
        "params": params,
        "requires": requires,
        "effects": effects,
        "narrative": str(spec.get("narrative") or f"@c makes {label.lower()}."),
        "rpg": {"system": "craft", "id": rid, "inputs": kinds, "station": station, "output": dict(out),
                "label": label},
    }, ""


# ---------------------------------------------------------------------
# 自动填参数：玩家只说「做斧头」「采这棵树」「买这个」，具体用哪几件东西由这里挑
# ---------------------------------------------------------------------

def _held(world, person: str) -> List[Dict[str, Any]]:
    return world.run_query(
        """
        MATCH (:Person {name: $p})-[:HAS_ITEM]->(i:Item)
        RETURN i.name AS name, toLower(coalesce(i.item_kind, '')) AS kind
        ORDER BY i.name
        """,
        p=person,
    ) or []


def _ground(world, location: str) -> List[Dict[str, Any]]:
    return world.run_query(
        """
        MATCH (i:Item)-[:LOCATED_AT]->(:Location {name: $l})
        RETURN i.name AS name, toLower(coalesce(i.item_kind, '')) AS kind
        ORDER BY i.name
        """,
        l=location,
    ) or []


def _location_of(world, person: str) -> str:
    rows = world.run_query(
        "MATCH (:Person {name: $p})-[:LOCATED_AT]->(l:Location) RETURN l.name AS l LIMIT 1", p=person
    ) or []
    return str(rows[0]["l"]) if rows else ""


def bind_params(world, actor: str, action: Dict[str, Any], target: str = "") -> Tuple[Optional[Dict[str, str]], str]:
    """采集 / 制造动作：按配方挑出手上的材料、身边的工作台 / 采集点。返回 (参数, 缺什么)。"""
    rpg = action.get("rpg") or {}
    here = _location_of(world, actor)
    held = _held(world, actor)
    ground = _ground(world, here) if here else []
    params: Dict[str, str] = {"c": actor, "l": here}
    if rpg.get("system") == "gather":
        nodes = [g["name"] for g in ground if g["kind"] == rpg["node"] and (not target or g["name"] == target)]
        if not nodes:
            return None, f"There is no {rpg['node']} here"
        params["node"] = nodes[0]
        if rpg.get("tool"):
            tools = [h["name"] for h in held if h["kind"] == rpg["tool"]]
            if not tools:
                return None, f"You need {_a(rpg['tool'])}"
            params["tool"] = tools[0]
        return params, ""
    if rpg.get("system") == "craft":
        free = list(held)
        missing: List[str] = []
        for i, kind in enumerate(rpg["inputs"]):
            pick = next((h for h in free if h["kind"] == kind), None)
            if pick is None:
                missing.append(kind)
                continue
            free.remove(pick)
            params[f"in{i + 1}"] = pick["name"]
        if rpg.get("station"):
            stations = [g["name"] for g in ground if g["kind"] == rpg["station"]]
            if not stations:
                missing.append(f"{_a(rpg['station'])} nearby")
            else:
                params["station"] = stations[0]
        if missing:
            return None, "Missing: " + ", ".join(missing)
        return params, ""
    return None, "not a gathering or crafting action"


def crafting_view(world, actor: str) -> List[Dict[str, Any]]:
    """制造面板：每个配方要什么、你手上有几样、差什么、能不能现在做。"""
    from content import get_content

    held = _held(world, actor)
    here = _location_of(world, actor)
    ground_kinds = {g["kind"] for g in _ground(world, here)} if here else set()
    out = []
    for name, spec in sorted(get_content().actions.items()):
        rpg = spec.get("rpg") or {}
        if rpg.get("system") != "craft":
            continue
        need: Dict[str, int] = {}
        for kind in rpg["inputs"]:
            need[kind] = need.get(kind, 0) + 1
        have = {k: sum(1 for h in held if h["kind"] == k) for k in need}
        inputs = [{"kind": k, "need": n, "have": min(have[k], n)} for k, n in need.items()]
        station = rpg.get("station") or ""
        at_station = not station or station in ground_kinds
        ready = all(i["have"] >= i["need"] for i in inputs) and at_station
        out.append({
            "action": name,
            "id": rpg["id"],
            "label": rpg.get("label") or spec.get("label") or name,
            "description": spec.get("description") or "",
            "inputs": inputs,
            "station": station,
            "at_station": at_station,
            "ready": ready,
            "output": rpg.get("output", {}).get("archetype") or rpg.get("output", {}).get("item_kind") or "",
        })
    out.sort(key=lambda r: (not r["ready"], r["label"]))
    return out


def gather_node_kinds() -> set:
    from content import get_content

    return {
        (spec.get("rpg") or {}).get("node")
        for spec in get_content().actions.values()
        if (spec.get("rpg") or {}).get("system") == "gather"
    }


def gather_options(world, actor: str, node: str) -> List[Dict[str, Any]]:
    """一个采集点能做的采集动作（给客户端的物品菜单）。"""
    from content import get_content
    from graph_ops import check_all

    rows = world.run_query("MATCH (i:Item {name: $n}) RETURN toLower(coalesce(i.item_kind, '')) AS k", n=node) or []
    kind = rows[0]["k"] if rows else ""
    out = []
    for name, spec in sorted(get_content().actions.items()):
        rpg = spec.get("rpg") or {}
        if rpg.get("system") != "gather" or rpg.get("node") != kind:
            continue
        params, why = bind_params(world, actor, spec, target=node)
        ok = params is not None
        if ok:
            ok, why = check_all(world, spec.get("requires") or [], {**params, "actor": actor})
        out.append({"id": f"do:{name}", "label": rpg.get("label") or name, "available": bool(ok),
                    "reason": "" if ok else why})
    return out
