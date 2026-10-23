"""
AITown3 playable client web server

Start:
    python AITown3/web/server.py --host 127.0.0.1 --port 8765

Open in browser:
    http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# path bootstrap
_WEB_DIR = Path(__file__).resolve().parent
_AITOWN3 = _WEB_DIR.parent
if str(_AITOWN3) not in sys.path:
    sys.path.insert(0, str(_AITOWN3))
for sub in ("neo4j", "actions", "planner", "agent", "utility", "memory", "test", "web"):
    p = str(_AITOWN3 / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from neo4j_world import Neo4jWorld  # noqa: E402
from graph_api import fetch_graph, inspect_entity, list_entity_names  # noqa: E402
from character_actions import list_characters, get_character_actions  # noqa: E402
from play_api import get_play_state, get_player, list_locations_graph  # noqa: E402
from character_profile import get_character_profile, get_character_dossier  # noqa: E402
from character_plans import (  # noqa: E402
    get_plan_manager,
    list_goal_terms,
    get_goal_target_options,
)
from llm_service import get_llm_status, get_character_memories, clear_character_memories, clear_all_memories  # noqa: E402
from content import get_content  # noqa: E402
from registry import get_registry  # noqa: E402
from world_simulator import get_world_simulator  # noqa: E402

_STATIC = _WEB_DIR / "static"

app = FastAPI(title="AITown3 Play", version="2.0.0")
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

_world: Optional[Neo4jWorld] = None


def get_world() -> Neo4jWorld:
    global _world
    if _world is None:
        Neo4jWorld._instance = None  # type: ignore[attr-defined]
        _world = Neo4jWorld.get_instance()
    return _world


class ExecuteActionRequest(BaseModel):
    action: str
    params: Dict[str, str] = Field(default_factory=dict)
    message: str = ""  # Player dialogue line for talk actions


class SetPlanRequest(BaseModel):
    goal: str = ""
    target_entity: Optional[str] = None
    reason: str = ""
    auto: bool = False
    use_llm: bool = False
    force_mock: bool = False


class DecideRequest(BaseModel):
    plan: bool = True
    force_mock: bool = False
    parent_goal: str = "explore the town and survive"


class TurnRequest(BaseModel):
    characters: Optional[List[str]] = None
    auto_plan: bool = False
    use_llm: bool = False
    force_mock: bool = False
    include_player: bool = False


class SimulationStartRequest(BaseModel):
    # Real-time interval between ticks; default is high-frequency continuous simulation
    interval_seconds: float = Field(default=0.25, ge=0.1, le=120.0)
    # Simulation seconds advanced per tick; default matches interval (1:1)
    sim_dt: Optional[float] = Field(default=None, ge=0.05, le=10.0)
    use_llm: bool = False
    force_mock: bool = False
    include_player: bool = False
    # Simulate only these characters; None/empty = all (player still gated by include_player)
    characters: Optional[List[str]] = None


class TimeAdvanceRequest(BaseModel):
    hours: float = Field(default=1.0, gt=0.0, le=48.0)


class DirectorPlanRequest(BaseModel):
    prompt: str = Field(
        default="A missing castle key mystery in a small town with rumors and secrets.",
        min_length=1,
    )
    force_mock: bool = False
    template_id: Optional[str] = None
    # Optional partial blueprint from the user; AI completes it
    draft: Optional[Dict[str, Any]] = None
    # 手动配置的地图（maps/ 或 worlds/ 的 id，或 "current"）；不填 = 当前地图
    map_id: Optional[str] = None


class DirectorBuildRequest(BaseModel):
    prompt: str = Field(
        default="A missing castle key mystery in a small town with rumors and secrets.",
        min_length=1,
    )
    clear: bool = True
    force_mock: bool = False
    template_id: Optional[str] = None
    blueprint: Optional[Dict[str, Any]] = None
    draft: Optional[Dict[str, Any]] = None
    map_id: Optional[str] = None


_sim_task: Optional[asyncio.Task] = None
_sim_lock = asyncio.Lock()
_sim_running = False
_sim_interval = 0.25
_sim_dt = 0.25
_sim_use_llm = False
_sim_force_mock = False
_sim_include_player = False
_sim_characters: Optional[List[str]] = None
_sim_started_at: Optional[str] = None
_sim_last_result: Optional[Dict[str, Any]] = None
# Simulation ticks may overlap with dialogue/planning; too few workers can block move completion
_executor = ThreadPoolExecutor(max_workers=4)


@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


@app.get("/inspect")
def llm_inspect_page():
    return FileResponse(_STATIC / "inspect.html")


@app.get("/api/llm/traces")
def api_llm_traces(limit: int = 40, kind: Optional[str] = None):
    from llm_trace import list_llm_traces

    return {"traces": list_llm_traces(limit=limit, kind=kind)}


@app.get("/api/llm/traces/latest")
def api_llm_trace_latest(kind: Optional[str] = None):
    from llm_trace import latest_llm_trace

    t = latest_llm_trace(kind=kind)
    if not t:
        raise HTTPException(status_code=404, detail="no traces")
    return t


@app.get("/api/llm/traces/{trace_id}")
def api_llm_trace_detail(trace_id: str):
    from llm_trace import get_llm_trace

    t = get_llm_trace(trace_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"trace not found: {trace_id}")
    return t


@app.delete("/api/llm/traces")
def api_llm_traces_clear():
    from llm_trace import clear_llm_traces

    return {"cleared": clear_llm_traces()}


@app.get("/api/health")
def health():
    try:
        world = get_world()
        world.run_query("RETURN 1 AS ok")
        return {"ok": True, "uri": world.uri}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.get("/api/graph")
def api_graph(limit: int = 800):
    try:
        return fetch_graph(get_world(), limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/entities")
def api_entities():
    try:
        return list_entity_names(get_world())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/inspect/{label}/{name}")
def api_inspect(label: str, name: str):
    try:
        data = inspect_entity(get_world(), label, name)
        if not data.get("found"):
            raise HTTPException(status_code=404, detail="Entity not found")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/player")
def api_player():
    """Current player character summary."""
    try:
        player = get_player(get_world())
        if player is None:
            raise HTTPException(status_code=404, detail="No player character in the current world")
        return {"player": player}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


class FactionJoinRequest(BaseModel):
    via: Optional[str] = None  # 向哪位在场成员开口；不填则自动找


def _faction_call(fn_name: str, faction: str, **kwargs):
    import sys as _sys
    agent_path = str(_AITOWN3 / "agent")
    if agent_path not in _sys.path:
        _sys.path.insert(0, agent_path)
    import factions as _factions
    from llm_service import get_web_memory

    world = get_world()
    player = get_player(world)
    if player is None:
        raise HTTPException(status_code=404, detail="No player character in the current world")
    fn = getattr(_factions, fn_name)
    return fn(world, player["name"], faction, memory=get_web_memory(), **kwargs)


@app.post("/api/factions/{name}/join")
def api_faction_join(name: str, req: FactionJoinRequest):
    """玩家向在场成员申请加入势力：joined | trial_started | refused。"""
    try:
        return _faction_call("request_join", name, via=(req.via or "").strip() or None)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/factions/{name}/leave")
def api_faction_leave(name: str):
    try:
        return _faction_call("leave_faction", name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/factions/tick")
def api_faction_tick(ticks: int = 1):
    """立刻推进势力历史若干轮（每轮 = FACTION_TICK_HOURS 游戏小时）。"""
    try:
        import faction_sim
        from llm_service import get_web_memory

        events = faction_sim.force_tick(get_world(), ticks=ticks, memory=get_web_memory())
        return {"ok": True, "events": events, "chronicle": faction_sim.chronicle(get_world(), 12)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/play/state")
def api_play_state():
    """Fetch full play state (player, scene, actions, map) in one call."""
    try:
        return get_play_state(get_world())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/locations")
def api_locations():
    """All locations and adjacency edges for room map layout."""
    try:
        return list_locations_graph(get_world())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/characters")
def api_characters():
    try:
        return {"characters": list_characters(get_world())}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/characters/{name}/profile")
def api_character_profile(name: str):
    try:
        data = get_character_profile(get_world(), name)
        if not data.get("found"):
            raise HTTPException(status_code=404, detail="Character not found")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/characters/{name}/dossier")
def api_character_dossier(name: str, memory_limit: int = 40):
    """Character overview: status, profile, current plan, memory log."""
    try:
        data = get_character_dossier(
            get_world(),
            name.strip(),
            memory_limit=max(1, min(memory_limit, 100)),
        )
        if not data.get("found"):
            raise HTTPException(status_code=404, detail="Character not found")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/characters/{name}/actions")
def api_character_actions(name: str):
    try:
        data = get_character_actions(get_world(), name)
        if not data.get("found"):
            raise HTTPException(status_code=404, detail=f"Character not found: {name}")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/actions")
def api_actions():
    reg = get_registry()
    out = []
    for action in reg.all():
        out.append(
            {
                "name": action.name,
                "description": action.description,
                "category": action.category,
                "parameters": [
                    {"name": p.name, "type": p.type} for p in action.parameters
                ],
            }
        )
    return {"actions": out}


@app.post("/api/actions/execute")
async def api_execute(req: ExecuteActionRequest):
    reg = get_registry()
    action = reg.get(req.action)
    if action is None:
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")

    kwargs = {}
    for p in action.parameters:
        if p.name not in req.params or not req.params[p.name].strip():
            raise HTTPException(
                status_code=400,
                detail=f"Missing parameter: {p.name} ({p.type})",
            )
        kwargs[p.name] = req.params[p.name].strip()

    try:
        world = get_world()
        ok, reason = reg.execute(world, req.action, **kwargs)
        dialogue = None
        capability = get_content().capability_of(req.action)
        # 按能力签名的参数位置取值（内容包可自由命名参数）
        positional = [kwargs.get(p.name, "") for p in reg.get(req.action).parameters] if ok else []
        if ok and capability == "converse":
            try:
                import sys as _sys
                agent_path = str(_AITOWN3 / "agent")
                if agent_path not in _sys.path:
                    _sys.path.insert(0, agent_path)
                from dialogue_service import handle_talk_dialogue
                from llm_service import get_web_memory

                a, b, loc = positional[:3]
                mem = get_web_memory()
                msg = (req.message or "").strip()

                def _talk():
                    return handle_talk_dialogue(
                        world, a, b, loc,
                        memory=mem,
                        force_mock=False,
                        player_message=msg,
                    )

                # Run dialogue LLM in thread pool to avoid blocking the event loop / connection pool
                loop = asyncio.get_running_loop()
                dialogue = await loop.run_in_executor(_executor, _talk)
            except Exception as e:
                dialogue = {
                    "summary": "dialogue failed",
                    "lines": [],
                    "error": str(e),
                }
        elif ok and capability == "give":
            # Giving gifts increases receiver trust toward the player
            try:
                import sys as _sys
                agent_path = str(_AITOWN3 / "agent")
                if agent_path not in _sys.path:
                    _sys.path.insert(0, agent_path)
                from npc_knowledge import adjust_trust, get_trust

                giver, _item, receiver = positional[:3]
                # Only increase trust when the giver is the player
                rows = world.run_query(
                    """
                    MATCH (p:Person {name: $n})
                    RETURN coalesce(p.is_player, false) AS is_player,
                           coalesce(p.role, '') AS role
                    """,
                    n=giver,
                )
                is_player = False
                if rows:
                    is_player = bool(rows[0].get("is_player")) or rows[0].get("role") == "player"
                if is_player and receiver:
                    adjust_trust(world, receiver, 2)
                    reason = f"{reason} ({receiver} trust → {get_trust(world, receiver)})"
            except Exception:
                pass
            try:
                from inner_life import settle_favors
                from llm_service import get_web_memory

                settled = settle_favors(world, memory=get_web_memory())
                if settled:
                    reason = f"{reason} (favor kept: {'; '.join(settled)})"
            except Exception:
                pass
        faction_changes = []
        if ok:
            try:
                import sys as _sys
                agent_path = str(_AITOWN3 / "agent")
                if agent_path not in _sys.path:
                    _sys.path.insert(0, agent_path)
                from factions import on_action
                from llm_service import get_web_memory

                faction_changes = on_action(world, capability, positional, memory=get_web_memory())
                if faction_changes:
                    reason = f"{reason} (factions: {'; '.join(faction_changes)})"
            except Exception:
                pass
        interaction = None
        if ok and req.action == "interact":
            try:
                import sys as _sys
                agent_path = str(_AITOWN3 / "agent")
                if agent_path not in _sys.path:
                    _sys.path.insert(0, agent_path)
                from item_interact import take_last_interact_result

                interaction = take_last_interact_result()
                if interaction and interaction.get("narrative"):
                    reason = interaction["narrative"]
            except Exception as e:
                interaction = {"ok": False, "error": str(e)}
        return {
            "success": ok,
            "message": reason,
            "action": req.action,
            "params": kwargs,
            "dialogue": dialogue,
            "interaction": interaction,
            "faction_changes": faction_changes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/items/{name}/interact/options")
async def api_interact_options(
    name: str, actor: Optional[str] = None, refresh: bool = False
):
    """Discover/refresh temporary Actions (Cypher) on an item."""
    try:
        import sys as _sys
        agent_path = str(_AITOWN3 / "agent")
        if agent_path not in _sys.path:
            _sys.path.insert(0, agent_path)
        from item_interact import generate_interaction_options
        from llm_service import get_web_memory

        world = get_world()
        chars = list_characters(world)
        who = (actor or "").strip()
        if not who:
            player = next((c["name"] for c in chars if c.get("is_player")), None)
            who = player or (chars[0]["name"] if chars else "")
        if not who:
            raise HTTPException(status_code=400, detail="actor required")

        def _gen():
            return generate_interaction_options(
                world,
                who,
                name,
                force_mock=False,
                memory=get_web_memory(),
                refresh=refresh,
            )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, _gen)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/goals")
def api_goals():
    return {"goals": list_goal_terms()}


@app.get("/api/llm/status")
def api_llm_status(refresh: bool = False):
    try:
        return get_llm_status(refresh=refresh)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/plans")
def api_all_plans():
    try:
        mgr = get_plan_manager()
        return {"turn": mgr.turn_count, "plans": mgr.list_states(get_world())}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/characters/{name}/memories")
def api_character_memories(name: str, limit: int = 50, query: Optional[str] = None):
    try:
        chars = {c["name"] for c in list_characters(get_world())}
        if name not in chars:
            raise HTTPException(status_code=404, detail=f"Character not found: {name}")
        return get_character_memories(name, limit=limit, query=query)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/characters/{name}/memories")
def api_clear_character_memories(name: str):
    """Clear Chroma memories for the specified character."""
    try:
        result = clear_character_memories(name.strip())
        if not result.get("ok"):
            raise HTTPException(status_code=503, detail=result.get("message", "Memory unavailable"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/memories/clear")
def api_clear_all_memories():
    """Clear all Chroma memories."""
    try:
        result = clear_all_memories()
        if not result.get("ok"):
            raise HTTPException(status_code=503, detail=result.get("message", "Memory unavailable"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/characters/{name}/plan")
def api_get_plan(name: str):
    try:
        world = get_world()
        mgr = get_plan_manager()
        state = mgr.get_state(name, world)
        if state is None:
            return mgr.ensure_character(name, world).to_dict()
        return state.to_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/characters/{name}/history")
def api_character_history(name: str, limit: int = 60):
    """Behaviour log: decisions (with reasons / triggers), actions and results."""
    return get_world_simulator().history(name, limit=limit)


@app.get("/api/characters/{name}/plan/targets")
def api_plan_targets(name: str, goal: str):
    try:
        world = get_world()
        if name not in world.list_entities("Person"):
            raise HTTPException(status_code=404, detail=f"Character not found: {name}")
        return get_goal_target_options(world, name, goal)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/characters/{name}/plan")
def api_set_plan(name: str, req: SetPlanRequest):
    try:
        world = get_world()
        if name not in world.list_entities("Person"):
            raise HTTPException(status_code=404, detail=f"Character not found: {name}")
        mgr = get_plan_manager()
        if req.auto:
            return mgr.auto_plan(
                world,
                name,
                use_llm=req.use_llm,
                force_mock=req.force_mock,
            )
        if not req.goal.strip():
            raise HTTPException(status_code=400, detail="Specify goal, or set auto=true")
        return mgr.set_goal_and_plan(
            world,
            name,
            req.goal.strip(),
            req.target_entity.strip() if req.target_entity else None,
            reason=req.reason,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/characters/{name}/decide")
def api_decide(name: str, req: DecideRequest):
    try:
        world = get_world()
        if name not in world.list_entities("Person"):
            raise HTTPException(status_code=404, detail=f"Character not found: {name}")
        mgr = get_plan_manager()
        if req.plan:
            return mgr.llm_decide_and_plan(
                world,
                name,
                force_mock=req.force_mock,
                parent_goal=req.parent_goal,
            )
        return mgr.llm_decide(
            world,
            name,
            force_mock=req.force_mock,
            parent_goal=req.parent_goal,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/characters/{name}/plan")
def api_clear_plan(name: str):
    try:
        mgr = get_plan_manager()
        return mgr.clear_plan(name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/turn")
def api_turn(req: TurnRequest):
    try:
        mgr = get_plan_manager()
        return mgr.run_turn(
            get_world(),
            characters=req.characters,
            auto_plan=req.auto_plan,
            use_llm=req.use_llm,
            force_mock=req.force_mock,
            include_player=req.include_player,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _run_simulation_tick() -> Dict[str, Any]:
    mgr = get_plan_manager()
    return mgr.tick(
        get_world(),
        dt=_sim_dt,
        auto_plan=True,
        use_llm=_sim_use_llm,
        force_mock=_sim_force_mock,
        instant=False,
        include_player=_sim_include_player,
        characters=_sim_characters,
    )


async def _simulation_loop() -> None:
    global _sim_running, _sim_last_result
    while _sim_running:
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(_executor, _run_simulation_tick)
            _sim_last_result = result
            if isinstance(result, dict) and result.get("missions_complete"):
                _sim_last_result = {
                    **result,
                    "stopped_reason": "missions_complete",
                    "message": (
                        f"{', '.join(_sim_characters)} reached long-term goals; simulation stopped"
                        if _sim_characters
                        else "All auto characters reached long-term goals; simulation stopped"
                    ),
                }
                _sim_running = False
                break
        except Exception as e:
            _sim_last_result = {"error": str(e)}
        await asyncio.sleep(_sim_interval)


async def _stop_simulation() -> None:
    global _sim_task, _sim_running
    _sim_running = False
    if _sim_task and not _sim_task.done():
        _sim_task.cancel()
        try:
            await _sim_task
        except asyncio.CancelledError:
            pass
    _sim_task = None


@app.post("/api/simulation/start")
async def api_simulation_start(req: SimulationStartRequest):
    global _sim_task, _sim_running, _sim_interval, _sim_dt, _sim_use_llm
    global _sim_force_mock, _sim_include_player, _sim_characters, _sim_started_at, _sim_last_result
    async with _sim_lock:
        await _stop_simulation()
        _sim_interval = req.interval_seconds
        _sim_dt = float(req.sim_dt) if req.sim_dt is not None else float(req.interval_seconds)
        _sim_use_llm = req.use_llm
        _sim_force_mock = req.force_mock
        chars = [c.strip() for c in (req.characters or []) if c and str(c).strip()]
        _sim_characters = chars or None
        # When a character list is given: everyone on the list auto-plans (including player)
        _sim_include_player = True if _sim_characters else bool(req.include_player)
        _sim_started_at = datetime.now(timezone.utc).isoformat()
        _sim_last_result = None
        _sim_running = True
        _sim_task = asyncio.create_task(_simulation_loop())
    return {
        "ok": True,
        "running": True,
        "interval_seconds": _sim_interval,
        "sim_dt": _sim_dt,
        "use_llm": _sim_use_llm,
        "force_mock": _sim_force_mock,
        "include_player": _sim_include_player,
        "characters": _sim_characters,
    }


@app.post("/api/time/advance")
async def api_time_advance(req: TimeAdvanceRequest):
    """Skip simulated time forward (for testing routines / day-night)."""
    try:
        from routine import clock_info

        async with _sim_lock:
            sim_time = get_world_simulator().advance_time(get_world(), req.hours)
        return {"ok": True, "sim_time": sim_time, "clock": clock_info(sim_time)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


class BuildRequest(BaseModel):
    archetype: str
    x: int
    y: int
    actor: str = ""


def _player_name(world) -> str:
    player = get_player(world)
    if player is None:
        raise HTTPException(status_code=404, detail="No player character in the current world")
    return player["name"]


@app.get("/api/build/blueprints")
def api_build_blueprints(actor: str = ""):
    """Blueprints the actor could place in their current zone (with availability / reason)."""
    from action import get_person_location
    from building import list_blueprints

    world = get_world()
    who = actor.strip() or _player_name(world)
    loc = get_person_location(world, who)
    if not loc:
        raise HTTPException(status_code=404, detail=f"{who} has no location")
    return {"actor": who, "location": loc, "blueprints": list_blueprints(world, who, loc)}


@app.post("/api/build")
async def api_build(req: BuildRequest):
    """Place a blueprint (construction site) on the actor's current zone grid."""
    from building import place_blueprint

    async with _sim_lock:
        world = get_world()
        who = req.actor.strip() or _player_name(world)
        result = place_blueprint(world, who, req.archetype.strip(), req.x, req.y)
        if result.get("success"):
            get_world_simulator().announce(result["message"])
    return result


@app.post("/api/simulation/stop")
async def api_simulation_stop():
    async with _sim_lock:
        await _stop_simulation()
    return {"ok": True, "running": False}


@app.get("/api/simulation/status")
def api_simulation_status():
    mgr = get_plan_manager()
    world = get_world()
    last = _sim_last_result or {}
    return {
        "running": _sim_running,
        "turn": mgr.turn_count,
        "tick": mgr.tick_count,
        "sim_time": mgr.sim_time,
        "interval_seconds": _sim_interval,
        "sim_dt": _sim_dt,
        "use_llm": _sim_use_llm,
        "force_mock": _sim_force_mock,
        "include_player": _sim_include_player,
        "characters": _sim_characters,
        "started_at": _sim_started_at,
        "last_result": _sim_last_result,
        "stopped_reason": last.get("stopped_reason"),
        "message": last.get("message"),
        "missions_complete": bool(last.get("missions_complete")),
        "npc_activity": mgr._sim.npc_activity_summary(world) if _sim_running or mgr.tick_count else [],
    }


def _clear_memories_on_rebuild() -> Dict[str, Any]:
    """Clear Chroma memories when rebuilding/clearing the world."""
    try:
        result = clear_all_memories()
        return {
            "memory_cleared": bool(result.get("ok")),
            "memory_available": bool(result.get("available")),
            "memory_message": result.get("message", ""),
        }
    except Exception as e:
        return {
            "memory_cleared": False,
            "memory_available": False,
            "memory_message": str(e),
        }


@app.get("/api/worlds")
def api_list_worlds():
    """List template worlds from worlds/*.json."""
    from director import default_world_id, list_worlds

    return {"worlds": list_worlds(), "default": default_world_id()}


@app.post("/api/worlds/{world_id}/load")
async def api_load_world(world_id: str, clear: bool = True):
    """Load a text-configured template world into Neo4j."""
    try:
        await _stop_simulation()
        from director import build_from_blueprint, load_world

        world = get_world()

        def _work():
            bp = load_world(world_id)
            return build_from_blueprint(world, bp, clear=clear)

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_executor, _work)
        get_plan_manager().reset_all()
        get_world_simulator().reset()
        mem = _clear_memories_on_rebuild() if clear else {
            "memory_cleared": False,
            "memory_available": True,
            "memory_message": "World not cleared; memories retained",
        }
        return {
            "ok": True,
            "message": f"Loaded template world: {result.get('title', world_id)}"
            + (" (memories cleared)" if clear else ""),
            "world_id": world_id,
            "title": result.get("title"),
            "counts": result.get("counts"),
            **result,
            **mem,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/maps")
def api_maps():
    """手动配置的地图（导演只在这些地图上排布事件）。"""
    try:
        from director.maps import list_maps

        return {"maps": list_maps(get_world())}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/director/plan")
async def api_director_plan(body: DirectorPlanRequest):
    """Plan/complete an event blueprint from user draft (does not write to world)."""
    try:
        from director import plan_event

        def _work():
            return plan_event(
                body.prompt,
                force_mock=body.force_mock,
                draft=body.draft,
                map_id=body.map_id,
                world=get_world(),
            ).to_dict()

        loop = asyncio.get_running_loop()
        # Four-step LLM (skeleton/world/cast/mechanics), default 110s
        deadline = float(os.getenv("DIRECTOR_PLAN_TIMEOUT", "110"))
        # Fresh thread pool each time so a stuck OpenAI call does not block the next click
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            try:
                blueprint = await asyncio.wait_for(
                    loop.run_in_executor(pool, _work),
                    timeout=deadline,
                )
                return {"ok": True, "blueprint": blueprint, "warning": ""}
            except asyncio.TimeoutError:
                return {
                    "ok": False,
                    "blueprint": None,
                    "warning": f"AI did not finish within {int(deadline)}s. Simplify the draft and retry, or use a template world from the top bar.",
                }
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/director/build")
async def api_director_build(body: DirectorBuildRequest):
    """Materialize confirmed blueprint (or replan) into Neo4j."""
    try:
        await _stop_simulation()
        from director import EventBlueprint, build_from_blueprint, plan_and_build

        world = get_world()

        def _work():
            if body.blueprint:
                bp = EventBlueprint.from_dict(body.blueprint, source="api")
                return build_from_blueprint(world, bp, clear=body.clear)
            return plan_and_build(
                world,
                body.prompt,
                clear=body.clear,
                force_mock=body.force_mock,
                draft=body.draft,
                map_id=body.map_id,
            )

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_executor, _work)
        get_plan_manager().reset_all()
        get_world_simulator().reset()
        mem = _clear_memories_on_rebuild() if body.clear else {
            "memory_cleared": False,
            "memory_available": True,
            "memory_message": "World not cleared; memories retained",
        }
        msg_suffix = " (memories cleared)" if body.clear else ""
        return {
            "ok": True,
            "message": f"Director built event: {result.get('title', '')}{msg_suffix}",
            **result,
            **mem,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/world/rebuild")
async def api_rebuild(clear: bool = True, world_id: Optional[str] = None):
    """Legacy endpoint: load default or specified template world."""
    from director import default_world_id

    wid = (world_id or "").strip() or default_world_id()
    return await api_load_world(wid, clear=clear)


@app.post("/api/world/rebuild-solo")
async def api_rebuild_solo(character: str = "Alice"):
    """Build a minimal single-character world for isolated planning tests."""
    try:
        await _stop_simulation()
        import build_solo_world as solo_builder

        world = get_world()
        world.clear_all()
        solo_builder.build_solo_world(world, character=character.strip() or "Alice")
        get_plan_manager().reset_all()
        get_world_simulator().reset()
        mem = _clear_memories_on_rebuild()
        return {
            "ok": True,
            "message": f"Solo world rebuilt (character: {character}, memories cleared)",
            "character": character,
            "locations": ["TownSquare", "Tavern", "Forest", "Castle"],
            **mem,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/world/clear")
async def api_clear():
    try:
        await _stop_simulation()
        get_world().clear_all()
        get_plan_manager().reset_all()
        get_world_simulator().reset()
        mem = _clear_memories_on_rebuild()
        return {"ok": True, "message": "Database and memories cleared", **mem}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="AITown3 Graph Explorer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    print(f"AITown3 Play: http://{args.host}:{args.port}")
    if args.reload:
        # Watch only web/ to avoid killing the process on agent/actions edits;
        # under debugpy, --reload stops on sys.exit from "Raised Exceptions".
        os.chdir(_WEB_DIR)
        uvicorn.run(
            "server:app",
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[str(_WEB_DIR)],
        )
    else:
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
