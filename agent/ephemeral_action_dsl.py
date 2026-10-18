"""
临时 Action — LLM 为物品当场发明的动作，挂到物品上再执行。

声明式（现行）：与内容包动作同一套 requires / effects 原语，执行走 graph_ops，
规划器（planner/compiler.py）能把它编译成 PDDL 动作::

    {"id": "chop", "label": "Chop for wood",
     "requires": [],
     "effects": [{"op": "spawn_item", "archetype": "firewood", "held_by": "@actor"}],
     "narrative": "@actor chops a dry log from @target"}

占位符：@actor（做动作的人）、@target（宿主物品）、@here（物品所在地点），
以及 @actor.<prop> / @target.<prop>。引擎自动加「人与物品同在 @here、人还活着」。

旧格式（只为兼容已存数据）：LLM 直接写 Cypher，规划器看不懂::

    action chop(actor, target):
        \"\"\"Chop for wood\"\"\"
        MATCH (p:Person {name: $actor})-[:LOCATED_AT]->(loc:Location)<-[:LOCATED_AT]-(t:Item {name: $target})
        CREATE (w:Item {
          name: 'Wood',
          item_kind: 'wood',
          value: 8,
          portable: true,
          description: 'A freshly chopped log'
        })
        CREATE (p)-[:HAS_ITEM]->(w)
        RETURN $actor + ' chops a dry log from ' + $target AS narrative,
               $actor + ' got wood from ' + $target AS memory

参数约定：执行时注入 $actor、$target、可选 $target_loc。
RETURN 列 narrative / memory（可选）用于叙事与记忆。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

logger = logging.getLogger(__name__)

# 极少数硬禁：整库清空 / 管理面。其余交给 Cypher。
_BANNED = re.compile(
    r"(?is)\b("
    r"DROP\s+(DATABASE|INDEX|CONSTRAINT|ROLE|USER)|"
    r"LOAD\s+CSV|"
    r"CALL\s+dbms\.|"
    r"CALL\s+db\.|"
    r"DETACH\s+DELETE\s+n\b"
    r")\b"
)


@dataclass
class EphemeralAction:
    id: str
    label: str
    source_code: str
    cypher: str = ""
    host_item: str = ""
    # 兼容旧字段（DSL 时代）；Cypher 模式下为空
    body_lines: List[str] = field(default_factory=list)
    # 声明式：与内容包动作同一套原语
    requires: List[Dict[str, Any]] = field(default_factory=list)
    effects: List[Dict[str, Any]] = field(default_factory=list)
    narrative: str = ""

    @property
    def declarative(self) -> bool:
        return not self.cypher.strip()

    def spec(self) -> Dict[str, Any]:
        """规划器 / 存盘用的声明式描述。"""
        return {
            "id": self.id,
            "label": self.label,
            "requires": self.requires,
            "effects": self.effects,
            "narrative": self.narrative,
        }

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "id": self.id,
            "label": self.label,
            "source_code": self.source_code,
            "cypher": self.cypher,
            "host_item": self.host_item,
        }
        if self.declarative:
            out.update(requires=self.requires, effects=self.effects, narrative=self.narrative)
        return out

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "EphemeralAction":
        cypher = str(data.get("cypher") or "")
        # 旧 DSL 存盘：body_lines → 已无用，忽略
        action = EphemeralAction(
            id=str(data.get("id") or ""),
            label=str(data.get("label") or ""),
            source_code=str(data.get("source_code") or ""),
            cypher=cypher,
            host_item=str(data.get("host_item") or ""),
            requires=[dict(c) for c in data.get("requires") or [] if isinstance(c, dict)],
            effects=[dict(e) for e in data.get("effects") or [] if isinstance(e, dict)],
            narrative=str(data.get("narrative") or ""),
        )
        if action.declarative and not action.source_code:
            action.source_code = json.dumps(action.spec(), ensure_ascii=False, indent=1)
        return action


@dataclass
class ExecResult:
    ok: bool
    narrative: str = ""
    memory: str = ""
    error: str = ""
    ops: List[Dict[str, Any]] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)


def parse_action_script(code: str, *, host_item: str = "") -> List[EphemeralAction]:
    """解析 LLM 输出的 action + Cypher 块。"""
    text = _strip_fences((code or "").strip())
    if not text:
        return []

    parts = re.split(r"(?m)^(?=action\s+)", text)
    actions: List[EphemeralAction] = []
    for part in parts:
        part = part.strip()
        if not part.lower().startswith("action "):
            continue
        m = re.match(
            r"action\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*:\s*\n(.*)",
            part,
            re.DOTALL,
        )
        if not m:
            continue
        aid = m.group(1)
        body = m.group(2)
        label, cypher = _parse_body_cypher(body)
        if not cypher.strip():
            continue
        if not label:
            label = aid.replace("_", " ")
        src = f"action {aid}(actor, target):\n"
        src += f'    """{label}"""\n'
        for line in cypher.strip().splitlines():
            src += f"    {line}\n"
        actions.append(
            EphemeralAction(
                id=aid,
                label=label,
                source_code=src.strip(),
                cypher=cypher.strip(),
                host_item=host_item,
            )
        )
    return actions


def validate_cypher(cypher: str) -> Tuple[bool, str]:
    if not (cypher or "").strip():
        return False, "empty cypher"
    if _BANNED.search(cypher):
        return False, "cypher contains banned admin/wipe patterns"
    return True, "ok"


def execute_ephemeral(
    world: "Neo4jWorld",
    action: EphemeralAction,
    *,
    actor: str,
    target: str,
) -> ExecResult:
    if action.declarative:
        return _execute_declarative(world, action, actor=actor, target=target)
    cypher = (action.cypher or "").strip()
    ok, reason = validate_cypher(cypher)
    if not ok:
        return ExecResult(ok=False, error=reason)

    target_loc = ""
    rows_loc = world.run_query(
        "MATCH (i:Item {name:$n})-[:LOCATED_AT]->(l:Location) RETURN l.name AS loc LIMIT 1",
        n=target,
    )
    if rows_loc:
        target_loc = rows_loc[0].get("loc") or ""

    try:
        rows = world.run_query(
            cypher,
            actor=actor,
            target=target,
            target_loc=target_loc,
        )
    except Exception as e:
        return ExecResult(ok=False, error=str(e), ops=[{"op": "cypher", "error": str(e)}])

    narrative = ""
    memory = ""
    if rows:
        first = rows[0] or {}
        if first.get("narrative") is not None:
            narrative = str(first["narrative"])
        if first.get("memory") is not None:
            memory = str(first["memory"])

    if not narrative:
        narrative = f"{actor} performs {action.label or action.id} on {target}."
    if not memory:
        memory = narrative

    try:
        from world_interactions import refresh_interactions_from_world

        refresh_interactions_from_world(world)
    except Exception:
        pass

    return ExecResult(
        ok=True,
        narrative=narrative,
        memory=memory,
        ops=[{"op": "cypher", "action_id": action.id}],
        rows=rows or [],
    )


def store_actions_on_item(world: "Neo4jWorld", item: str, actions: List[EphemeralAction]) -> None:
    from action import set_property

    payload = [a.to_dict() for a in actions]
    set_property(
        world,
        item,
        "Item",
        "ephemeral_actions",
        json.dumps(payload, ensure_ascii=False),
    )


def authored_actions(world: "Neo4jWorld", item: str) -> List[EphemeralAction]:
    """世界 / 原型作者写在物品脚本 script.actions 里的动作（同一套声明式格式）。"""
    from item_scripts import load_item_script

    specs = (load_item_script(world, item) or {}).get("actions")
    if not isinstance(specs, list):
        return []
    out: List[EphemeralAction] = []
    for raw in specs:
        action, err = validate_action_spec(raw, host_item=item)
        if action is None:
            logger.warning("authored action on %s rejected: %s", item, err)
            continue
        out.append(action)
    return out


def load_actions_from_item(world: "Neo4jWorld", item: str) -> List[EphemeralAction]:
    """物品能做的动作：作者写好的优先；否则是之前发明并存在物品上的。"""
    from action import get_property

    authored = authored_actions(world, item)
    if authored:
        return authored
    raw = get_property(world, item, "Item", "ephemeral_actions")
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:
            return []
    elif isinstance(raw, list):
        data = raw
    else:
        return []
    out: List[EphemeralAction] = []
    for row in data:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        a = EphemeralAction.from_dict(row)
        # 既无 Cypher 也无声明式内容（更早的 DSL 缓存）：跳过，强制重新合成
        if a.declarative and "effects" not in row and "requires" not in row:
            continue
        a.host_item = item
        out.append(a)
    return out


def find_action(actions: List[EphemeralAction], action_id: str) -> Optional[EphemeralAction]:
    aid = (action_id or "").strip()
    for a in actions:
        if a.id == aid:
            return a
    for a in actions:
        if aid and (aid in a.id or a.id in aid):
            return a
    return None


# ----- 声明式：校验 / 执行 -----

# LLM 能用的原语：graph_ops 能执行、planner/compiler.py 能编译成 PDDL 的那部分
EFFECT_OPS = {
    "set_prop": ("entity", "prop"),
    "add_prop": ("entity", "prop", "delta"),
    "spawn_item": (),
    "delete_entity": ("entity",),
    "relocate": ("entity", "to"),
    "link": ("a", "b", "rel"),
    "unlink": ("a", "b", "rel"),
}
CONDITION_OPS = {
    "prop_eq": ("entity", "prop"),
    "prop_neq": ("entity", "prop"),
    "prop_gte": ("entity", "prop", "value"),
    "prop_lte": ("entity", "prop", "value"),
    "prop_in": ("entity", "prop", "values"),
    "has_item": ("person", "item"),
    "person_at": ("person", "location"),
    "item_at": ("entity", "location"),
    "not_held": ("item",),
    "has_rel": ("a", "b", "rel"),
    "not_has_rel": ("a", "b", "rel"),
    "feels_gte": ("a", "b", "value"),
    "not_hostile": ("a", "b"),
    "any": ("of",),
}
PLACEHOLDERS = ("actor", "target", "here")
# 引擎自动加的：人还活着、人与宿主物品同在 @here
IMPLICIT_REQUIRES: List[Dict[str, Any]] = [
    {"op": "prop_gte", "entity": "@actor", "label": "Person", "prop": "hp", "value": 1,
     "message": "@actor is dead"},
    {"op": "item_at", "entity": "@target", "location": "@here"},
    {"op": "person_at", "person": "@actor", "location": "@here"},
]
MAX_ACTIONS = 4
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# 实体位置上出现的键（其余键是属性名 / 数值，不按实体检查）
_ENTITY_KEYS = ("entity", "person", "item", "location", "a", "b", "to", "held_by", "located_at")
# 位置是关系不是属性；名字不会变 —— 拿它们当属性条件只会让动作永远做不了
_NOT_PROPS = {"location", "name", "located_at", "holder"}
# label 写错（例如写成物品名）时属性读写会悄悄落空
_LABELS = {"Item", "Person", "Location"}


def parse_action_json(
    text: str,
    *,
    host_item: str,
    entity_names: Optional[set] = None,
    archetypes: Optional[set] = None,
) -> Tuple[List[EphemeralAction], List[str]]:
    """LLM 的 JSON 输出 → 校验过的声明式动作；返回 (actions, 丢弃原因)。"""
    data = _loads_json(_strip_fences(text or ""))
    if isinstance(data, dict):
        data = data.get("actions")
    if not isinstance(data, list):
        return [], ["no JSON list of actions found"]
    actions: List[EphemeralAction] = []
    errors: List[str] = []
    seen: set = set()
    for raw in data:
        if len(actions) >= MAX_ACTIONS:
            break
        action, err = validate_action_spec(
            raw, host_item=host_item, entity_names=entity_names, archetypes=archetypes
        )
        if action is None or action.id in seen:
            errors.append(err or f"duplicate id {getattr(action, 'id', '?')}")
            continue
        seen.add(action.id)
        actions.append(action)
    return actions, errors


class OpsChecker:
    """条件 / 效果的通用校验（物品动作、任务线的完成条件与奖励共用）。

    placeholders：允许的 @占位符；entity_names / archetypes 为 None 时不查这两项；
    anyone_actor：不许对这个占位符的属性设条件（物品动作要对任何人都成立）。
    返回值都是错误说明，空串 = 通过。
    """

    def __init__(
        self,
        *,
        placeholders: Tuple[str, ...],
        entity_names: Optional[set] = None,
        archetypes: Optional[set] = None,
        anyone_actor: Optional[str] = None,
    ) -> None:
        self.placeholders = placeholders
        self.entity_names = entity_names
        self.archetypes = archetypes
        self.anyone_actor = anyone_actor

    def _tokens(self, spec: Dict[str, Any], where: str) -> str:
        for key in _ENTITY_KEYS:
            val = spec.get(key)
            if val is None:
                continue
            if not isinstance(val, str) or not val:
                return f"{where}: {key} must be a name or placeholder"
            if val.startswith("@"):
                if val[1:] not in self.placeholders:
                    return f"{where}: unknown placeholder {val}"
            elif self.entity_names is not None and val not in self.entity_names:
                return f"{where}: unknown entity {val!r}"
        for key in ("value", "delta"):
            val = spec.get(key)
            if isinstance(val, str) and val.startswith("@"):
                head, _, prop = val[1:].partition(".")
                if head not in self.placeholders or not prop:
                    allowed = "/".join(f"@{p}" for p in self.placeholders)
                    return f"{where}: {key} {val} must be {allowed}.<prop>"
        return ""

    def _value(self, spec: Dict[str, Any], where: str, *, condition: bool = False) -> str:
        prop = spec.get("prop")
        if condition and self.anyone_actor and spec.get("entity") == f"@{self.anyone_actor}" and prop:
            return (f"{where}: the object's actions must work for anyone — don't require "
                    f"something about @{self.anyone_actor}'s '{prop}' (mood, personality, ...)")
        label = spec.get("label")
        if label is not None and label not in _LABELS:
            return f"{where}: label must be one of {sorted(_LABELS)} (got {label!r}); it is the node type, not a name"
        if prop in _NOT_PROPS:
            return (f"{where}: '{prop}' is not a property — use item_at / person_at for places"
                    + ("; the engine already checks that @actor stands next to @target"
                       if self.anyone_actor else ""))
        for key in ("value", "delta"):
            if isinstance(spec.get(key), (dict, list)):
                return (f"{where}: {key} must be a plain number / string / boolean "
                        "(there are no expressions; use add_prop for arithmetic)")
        values = spec.get("values")
        if values is not None and not (
            isinstance(values, list) and all(isinstance(v, (str, int, float, bool)) for v in values)
        ):
            return f"{where}: values must be a list of plain values"
        return ""

    def condition(self, cond: Any, where: str) -> str:
        if not isinstance(cond, dict) or cond.get("op") not in CONDITION_OPS:
            return f"{where}: unsupported condition {cond.get('op') if isinstance(cond, dict) else cond!r}"
        missing = [k for k in CONDITION_OPS[cond["op"]] if cond.get(k) in (None, "")]
        if missing:
            return f"{where}: {cond['op']} needs {', '.join(missing)}"
        if cond["op"] == "any":
            for i, sub in enumerate(cond.get("of") or []):
                err = self.condition(sub, f"{where}.of[{i}]")
                if err:
                    return err
            return ""
        return self._value(cond, where, condition=True) or self._tokens(cond, where)

    def effect(self, op: Any, where: str) -> str:
        if not isinstance(op, dict) or op.get("op") not in EFFECT_OPS:
            return f"{where}: unsupported effect {op.get('op') if isinstance(op, dict) else op!r}"
        missing = [k for k in EFFECT_OPS[op["op"]] if op.get(k) in (None, "")]
        if missing:
            return f"{where}: {op['op']} needs {', '.join(missing)}"
        if op["op"] == "spawn_item":
            if not (op.get("held_by") or op.get("located_at")):
                return f"{where}: spawn_item needs held_by or located_at"
            arch = op.get("archetype")
            if arch and self.archetypes is not None and str(arch).lower() not in self.archetypes:
                return f"{where}: unknown archetype {arch!r}"
            if not (arch or op.get("item_kind") or op.get("name")):
                return f"{where}: spawn_item needs an archetype or item_kind"
        for j, cond in enumerate(op.get("when") or []):
            err = self.condition(cond, f"{where}.when[{j}]")
            if err:
                return err
        return self._value(op, where) or self._tokens(op, where)

    def conditions(self, conds: Any, where: str) -> str:
        if not isinstance(conds, list):
            return f"{where} must be a list"
        for i, cond in enumerate(conds):
            err = self.condition(cond, f"{where}[{i}]")
            if err:
                return err
        return ""

    def effects(self, ops: Any, where: str) -> str:
        if not isinstance(ops, list):
            return f"{where} must be a list"
        for i, op in enumerate(ops):
            err = self.effect(op, f"{where}[{i}]")
            if err:
                return err
        return ""


def validate_action_spec(
    raw: Any,
    *,
    host_item: str,
    entity_names: Optional[set] = None,
    archetypes: Optional[set] = None,
) -> Tuple[Optional[EphemeralAction], str]:
    if not isinstance(raw, dict):
        return None, "action is not an object"
    aid = str(raw.get("id") or "").strip().lower()
    if not _ID_RE.match(aid):
        return None, f"bad id {aid!r} (use snake_case)"
    check = OpsChecker(
        placeholders=PLACEHOLDERS,
        entity_names=set(entity_names) if entity_names is not None else None,
        archetypes=archetypes,
        anyone_actor="actor",
    )
    requires = raw.get("requires") or []
    effects = raw.get("effects") or []
    if not isinstance(requires, list) or not isinstance(effects, list):
        return None, f"{aid}: requires / effects must be lists"
    err = check.conditions(requires, f"{aid}.requires") or check.effects(effects, f"{aid}.effects")
    if err:
        return None, err
    label = str(raw.get("label") or aid.replace("_", " "))[:60]
    action = EphemeralAction(
        id=aid,
        label=label,
        source_code="",
        host_item=host_item,
        requires=[dict(c) for c in requires],
        effects=[dict(e) for e in effects],
        narrative=str(raw.get("narrative") or "")[:200],
    )
    action.source_code = json.dumps(action.spec(), ensure_ascii=False, indent=1)
    return action, ""


def ephemeral_ctx(world: "Neo4jWorld", *, actor: str, target: str) -> Dict[str, str]:
    from action import get_item_location

    return {"actor": actor, "target": target, "here": get_item_location(world, target) or ""}


def check_ephemeral(
    world: "Neo4jWorld", action: EphemeralAction, *, actor: str, target: str
) -> Tuple[bool, str]:
    """声明式动作此刻能不能做（旧 Cypher 动作没有可检查的条件，恒为可做）。"""
    if not action.declarative:
        return True, "ok"
    from graph_ops import check_all

    ctx = ephemeral_ctx(world, actor=actor, target=target)
    return check_all(world, IMPLICIT_REQUIRES + action.requires, ctx)


def check_requires(
    world: "Neo4jWorld", action: EphemeralAction, *, actor: str, target: str
) -> Tuple[bool, str]:
    """只查动作自己的条件（不含「人要站在物品旁」——规划时人可能还在路上）。"""
    if not action.declarative:
        return True, "ok"
    from graph_ops import check_all

    return check_all(world, action.requires, ephemeral_ctx(world, actor=actor, target=target))


def _execute_declarative(
    world: "Neo4jWorld", action: EphemeralAction, *, actor: str, target: str
) -> ExecResult:
    from graph_ops import apply_all, check_all, render_text

    ctx = ephemeral_ctx(world, actor=actor, target=target)
    ok, reason = check_all(world, IMPLICIT_REQUIRES + action.requires, ctx)
    if not ok:
        return ExecResult(ok=False, error=reason)
    try:
        apply_all(world, action.effects, ctx)
    except Exception as e:
        return ExecResult(ok=False, error=str(e), ops=list(action.effects))
    narrative = render_text(action.narrative, ctx) if action.narrative else ""
    narrative = narrative or f"{actor} performs {action.label or action.id} on {target}."
    try:
        from world_interactions import refresh_interactions_from_world

        refresh_interactions_from_world(world)
    except Exception:
        pass
    return ExecResult(ok=True, narrative=narrative, memory=narrative, ops=list(action.effects))


def _loads_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"[\[{][\s\S]*[\]}]", text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ----- helpers -----

def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:cypher|python|actions?|json)?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _parse_body_cypher(body: str) -> Tuple[str, str]:
    lines = body.splitlines()
    label = ""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines):
        s = lines[i].strip()
        if s.startswith('"""') or s.startswith("'''"):
            quote = s[:3]
            if s.endswith(quote) and len(s) > 6:
                label = s[3:-3].strip()
                i += 1
            else:
                chunks = [s[3:]]
                i += 1
                while i < len(lines):
                    if quote in lines[i]:
                        chunks.append(lines[i].split(quote)[0])
                        i += 1
                        break
                    chunks.append(lines[i])
                    i += 1
                label = "\n".join(chunks).strip()

    cypher_lines: List[str] = []
    for j in range(i, len(lines)):
        raw = lines[j]
        if not raw.strip():
            cypher_lines.append("")
            continue
        # 去掉统一缩进；保留相对缩进意义不大，Cypher 不敏感
        cypher_lines.append(raw.strip())
    # 去掉尾部空行
    while cypher_lines and not cypher_lines[-1].strip():
        cypher_lines.pop()
    return label, "\n".join(cypher_lines)
