"""
临时 Action — LLM 直接写 Cypher，挂到物品上再执行。

示例::

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
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "source_code": self.source_code,
            "cypher": self.cypher,
            "host_item": self.host_item,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "EphemeralAction":
        cypher = str(data.get("cypher") or "")
        # 旧 DSL 存盘：body_lines → 已无用，忽略
        return EphemeralAction(
            id=str(data.get("id") or ""),
            label=str(data.get("label") or ""),
            source_code=str(data.get("source_code") or ""),
            cypher=cypher,
            host_item=str(data.get("host_item") or ""),
        )


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


def load_actions_from_item(world: "Neo4jWorld", item: str) -> List[EphemeralAction]:
    from action import get_property

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
        if isinstance(row, dict) and row.get("id") and (row.get("cypher") or row.get("source_code")):
            a = EphemeralAction.from_dict(row)
            # 旧缓存无 cypher：跳过，强制重新合成
            if not a.cypher.strip():
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


# ----- helpers -----

def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:cypher|python|actions?)?\s*", "", t, flags=re.I)
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
