"""
内容包加载器 — 引擎与内容的边界。

引擎只认识「能力 (capability)」与通用原语；具体动作 / 状态 / 物品原型都来自
content/<pack>/ 下的 JSON：

  content/<pack>/pack.json          {"name": ..., "description": ...}
  content/<pack>/actions/*.json     数据动作（参数 + 条件 + 图操作）
  content/<pack>/states/*.json      角色持续状态（如 asleep）
  content/<pack>/archetypes/*.json  物品原型（如 campfire / stove）
  content/<pack>/planning.json      PDDL 自动编译的手动剪枝（见 planner/compiler.py）
  content/<pack>/gathering/*.json   采集点（展开成 gather_<id> 动作，见 rpg_content.py）
  content/<pack>/recipes/*.json     配方（展开成 craft_<id> 动作）

启用的包由环境变量 AITOWN_CONTENT_PACKS（逗号分隔）决定；未设置时加载 base
以及 content/ 下其余所有带 pack.json 的包（按名字）。后加载的包覆盖同名定义。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

CONTENT_ROOT = Path(__file__).resolve().parents[1] / "content"

# 引擎规划器依赖的能力 → 参数类型签名（内容动作必须按此顺序声明参数）
CAPABILITIES: Dict[str, List[str]] = {
    "travel": ["Person", "Location", "Location"],
    "take": ["Person", "Item", "Location"],
    "drop": ["Person", "Item", "Location"],
    "give": ["Person", "Item", "Person", "Location"],
    "converse": ["Person", "Person", "Location"],
    "attack": ["Person", "Person", "Location"],
    "consume": ["Person", "Item"],
    "idle": ["Person"],
}

# 能力缺省对应的动作名（内容包没声明时的回退，保证旧存档 / 旧计划可读）
DEFAULT_ACTION_NAMES: Dict[str, str] = {
    "travel": "move",
    "take": "pick_up",
    "drop": "drop",
    "give": "give",
    "converse": "talk",
    "attack": "attack",
    "consume": "use_item",
    "idle": "wait",
}


@dataclass
class StateDef:
    """角色持续状态：进入后引擎暂停其决策，直到被唤醒条件打断。"""

    name: str
    label: str = ""
    description: str = ""
    # 处于此状态时，其他人不能把他当作动作对象（对话 / 交易 …）
    targetable: bool = True
    # 只有优先级 ≥ 此值的触发器能打断该状态
    wake_priority: float = 1.0
    # 日程换段时自动结束
    ends_on_routine_change: bool = True
    # 结束时执行的图操作（@actor = 该角色）
    on_exit: List[Dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "StateDef":
        return StateDef(
            name=str(data["name"]),
            label=str(data.get("label") or ""),
            description=str(data.get("description") or data["name"]),
            targetable=bool(data.get("targetable", True)),
            wake_priority=float(data.get("wake_priority", 1.0)),
            ends_on_routine_change=bool(data.get("ends_on_routine_change", True)),
            on_exit=[dict(o) for o in data.get("on_exit") or [] if isinstance(o, dict)],
        )


@dataclass
class ContentRegistry:
    packs: List[str] = field(default_factory=list)
    actions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    states: Dict[str, StateDef] = field(default_factory=dict)
    archetypes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # PDDL 编译的手动剪枝（planner/compiler.py）；各包 planning.json 依次深合并
    planning: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def action_for(self, capability: str) -> str:
        for name, spec in self.actions.items():
            if spec.get("capability") == capability:
                return name
        return DEFAULT_ACTION_NAMES.get(capability, capability)

    def capability_of(self, action_name: str) -> Optional[str]:
        spec = self.actions.get(action_name)
        if spec and spec.get("capability"):
            return str(spec["capability"])
        for cap, name in DEFAULT_ACTION_NAMES.items():
            if name == action_name:
                return cap
        return None

    def state(self, name: Optional[str]) -> Optional[StateDef]:
        return self.states.get(name) if name else None

    def hidden_states(self) -> List[str]:
        return [s.name for s in self.states.values() if not s.targetable]

    def archetype(self, name: str) -> Optional[Dict[str, Any]]:
        return self.archetypes.get((name or "").strip().lower())

    @property
    def conservation(self) -> bool:
        """守恒：东西只能采集、制造出来（planning.json "conservation": true）。
        关掉自动补货、禁止任务 / 剧情凭空生成东西或给钱。"""
        return bool(self.planning.get("conservation"))


_lock = threading.Lock()
_content: Optional[ContentRegistry] = None


def active_packs() -> List[str]:
    """AITOWN_CONTENT_PACKS（逗号分隔）优先；否则 base + content/ 下其余所有包（按名字）。"""
    raw = os.environ.get("AITOWN_CONTENT_PACKS")
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    others = sorted(
        p.name for p in CONTENT_ROOT.iterdir()
        if p.is_dir() and p.name != "base" and (p / "pack.json").is_file()
    ) if CONTENT_ROOT.is_dir() else []
    return ["base"] + others


def get_content() -> ContentRegistry:
    global _content
    with _lock:
        if _content is None:
            _content = load_packs(active_packs())
        return _content


def reload_content(packs: Optional[List[str]] = None) -> ContentRegistry:
    global _content
    with _lock:
        _content = load_packs(packs or active_packs())
    try:
        from registry import reset_registry

        reset_registry()
    except Exception:
        pass
    return _content


def load_packs(packs: List[str], *, root: Path = CONTENT_ROOT) -> ContentRegistry:
    reg = ContentRegistry(packs=list(packs))
    for pack in packs:
        base = root / pack
        if not base.is_dir():
            reg.errors.append(f"content pack not found: {pack}")
            continue
        for path in _json_files(base / "actions"):
            for spec in _read_specs(path, reg):
                err = validate_action_spec(spec)
                if err:
                    reg.errors.append(f"{path.name}: {err}")
                    continue
                reg.actions[spec["name"]] = spec
        from rpg_content import expand_gather, expand_recipe

        for folder, expand in (("gathering", expand_gather), ("recipes", expand_recipe)):
            for path in _json_files(base / folder):
                for spec in _read_specs(path, reg):
                    action, err = expand(spec)
                    if action is None:
                        reg.errors.append(f"{folder}/{path.name}: {err}")
                        continue
                    reg.actions[action["name"]] = action
        for path in _json_files(base / "states"):
            for spec in _read_specs(path, reg):
                if not spec.get("name"):
                    reg.errors.append(f"{path.name}: state needs a name")
                    continue
                reg.states[str(spec["name"])] = StateDef.from_dict(spec)
        for path in _json_files(base / "archetypes"):
            for spec in _read_specs(path, reg):
                name = str(spec.get("archetype") or path.stem).strip().lower()
                body = {k: v for k, v in spec.items() if k != "archetype"}
                reg.archetypes[name] = body
        planning = base / "planning.json"
        if planning.is_file():
            for spec in _read_specs(planning, reg):
                reg.planning = _deep_merge(reg.planning, spec)
    for err in reg.errors:
        print(f"[CONTENT] {err}")
    return reg


def validate_action_spec(spec: Dict[str, Any]) -> Optional[str]:
    name = spec.get("name")
    if not name:
        return "action needs a name"
    params = spec.get("params")
    if not isinstance(params, list) or not params:
        return f"{name}: params must be a non-empty list"
    for p in params:
        if not isinstance(p, dict) or not p.get("name") or p.get("type") not in (
            "Person",
            "Item",
            "Location",
        ):
            return f"{name}: each param needs name and type Person/Item/Location"
    if params[0].get("type") != "Person":
        return f"{name}: first param must be the acting Person"
    cap = spec.get("capability")
    if cap:
        want = CAPABILITIES.get(cap)
        if want is None:
            return f"{name}: unknown capability '{cap}' (known: {', '.join(CAPABILITIES)})"
        got = [p["type"] for p in params]
        if got != want:
            return f"{name}: capability '{cap}' needs params {want}, got {got}"
    if spec.get("enter_state") and len(params) != 1:
        return f"{name}: enter_state actions take only the actor"
    return None


def apply_archetype(data: Dict[str, Any]) -> Dict[str, Any]:
    """把 {"archetype": "stove", ...} 展开：原型字段打底，实例字段覆盖（dict 深合并）。"""
    name = data.get("archetype") if isinstance(data, dict) else None
    if not name:
        return data
    proto = _resolve_archetype(str(name), depth=0)
    rest = {k: v for k, v in data.items() if k != "archetype"}
    if proto is None:
        print(f"[CONTENT] unknown archetype '{name}'")
        return rest
    merged = _deep_merge(proto, rest)
    merged["archetype"] = str(name)
    return merged


def _resolve_archetype(name: str, *, depth: int) -> Optional[Dict[str, Any]]:
    """原型可用 "extends" 继承另一个原型。"""
    proto = get_content().archetype(name)
    if proto is None or depth > 8:
        return None
    proto = copy.deepcopy(proto)
    parent = proto.pop("extends", None)
    if parent:
        base = _resolve_archetype(str(parent), depth=depth + 1)
        if base is not None:
            proto = _deep_merge(base, proto)
    return proto


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def _json_files(folder: Path) -> List[Path]:
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.json"))


def _read_specs(path: Path, reg: ContentRegistry) -> List[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        reg.errors.append(f"{path}: {e}")
        return []
    items = data if isinstance(data, list) else [data]
    return [x for x in items if isinstance(x, dict)]
