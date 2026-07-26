"""
导演系统 — 规划事件（人物/地点/物品/任务）并构建到 Neo4j。

模板世界见 worlds/*.json，用 load_world / list_worlds 加载。
"""

from .blueprint import (
    AdjacencySpec,
    EventBlueprint,
    FactSpec,
    ItemSpec,
    LocationSpec,
    PersonSpec,
    QuestSpec,
    RumorSpec,
)
from .builder import build_from_blueprint, plan_and_build
from .planner import plan_event
from .templates import TEMPLATES, list_templates, pick_template
from .solvability import SolvabilityResult, check_solvable
from .validate import (
    ensure_cast_quality,
    ensure_playable_mechanics,
    is_minimally_playable,
    validate_blueprint,
)
from .world_loader import default_world_id, list_worlds, load_world, pick_world

__all__ = [
    "EventBlueprint",
    "AdjacencySpec",
    "LocationSpec",
    "PersonSpec",
    "ItemSpec",
    "QuestSpec",
    "FactSpec",
    "RumorSpec",
    "plan_event",
    "build_from_blueprint",
    "plan_and_build",
    "list_worlds",
    "load_world",
    "pick_world",
    "default_world_id",
    "TEMPLATES",
    "list_templates",
    "pick_template",
    "validate_blueprint",
    "ensure_playable_mechanics",
    "ensure_cast_quality",
    "is_minimally_playable",
    "check_solvable",
    "SolvabilityResult",
]
