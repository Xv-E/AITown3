"""
游戏主循环 — 持续时钟 tick：决策 → 规划 → 带耗时执行 → 评估/重规划。
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from action_timing import get_action_duration
from agent_profile import AgentProfile
from goal_feasibility import assess_goal, pick_best_feasible, sanitize_goal_target
from goal_planner import plan_for_goal, wait_plan
from registry import act

if TYPE_CHECKING:
    from neo4j_world import Neo4jWorld

PlanStep = Tuple[str, List[str]]

HARD_INTERRUPT_HP = 20
DECISION_COOLDOWN = 5.0
REPLAN_BACKOFF = 3.0
# LLM 模式下闲置多久（游戏小时）没有任何触发，就主动想一次
IDLE_REDECIDE_HOURS = 2.0
# 决策阶段并行规划的 NPC 数（LLM 选目标 + PDDL 求解）；1 = 串行
PLAN_WORKERS = int(os.environ.get("AITOWN3_PLAN_WORKERS", "4"))
# 一拍最多等决策（PDDL / LLM）这么多真实秒；默认不等：决策在后台算，做完后的下一拍再收，别人照常行动
DECISION_WAIT_SEC = float(os.environ.get("AITOWN3_DECISION_WAIT", "0"))

PERSON_FLAGS_QUERY = """
MATCH (p:Person {name: $n})
RETURN coalesce(p.is_player, false) AS is_player,
       coalesce(p.role, '') AS role,
       coalesce(p.dead, false) AS dead, coalesce(p.hp, 0) AS hp,
       coalesce(p.brain, 'llm') AS brain,
       coalesce(p.follows, '') AS follows
LIMIT 1
"""
# 物品被别人动过多久之内（模拟秒），自己因它失败算作「被抢」；同一冲突在此期间不重复记
CONFLICT_MEMORY = 30.0
# 客户端拿得到的最近对话条数
CONVERSATION_LIMIT = 12
# 被搭话的人：等对方走到身边的余量；对方手头有事时最多等多久（秒，模拟时间）
ENGAGE_MARGIN = 3.0
ENGAGE_WAIT = 20.0
# 和玩家聊天：NPC 站着等玩家回话（每说一句重新计时；玩家关掉对话框就放开）
PLAYER_REPLY_WAIT = 45.0
# 聊完之后要不要改主意：玩家说的话能打断计划（≥ INTERRUPT_PRIORITY），NPC 之间的闲聊等手上这步做完
TALK_PRIORITY = 0.55
PLAYER_TALK_PRIORITY = 0.85
# 决策时「刚才说过的话」看多久以内的（模拟秒；一天 = 300）
RECENT_TALK_WINDOW = 25.0
# 两种人：llm（用大模型想要做什么）/ routine（背景 NPC：只照日程行动，从不调用大模型做决策）
BRAINS = ("llm", "routine")
# 卡住：日程 / 店里要做的事连着做不成几次就挂委托、再多就临时请大模型想办法
REQUEST_AFTER = 2
STUCK_LIMIT = 3
# 背景 NPC 临时用大模型想事情多久（模拟秒；两个游戏小时）
ESCALATE_FOR = 25.0


def normalize_brain(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "routine" if text in ("routine", "npc", "rules", "rule") else "llm"


def reading_seconds(text: str) -> float:
    """读完一句台词要几秒（真实时间）；客户端 speech.rs 的 line_secs + 间隔与此一致。"""
    return min(6.0, max(2.0, 1.2 + len(text) * 0.055)) + 0.25


def _llm_configured() -> bool:
    try:
        from model_instances import is_primary_configured

        return is_primary_configured()
    except Exception:
        return False


@dataclass
class ActionResult:
    success: bool
    message: str
    agent_name: str
    action_type: str
    target: Optional[str] = None
    should_interrupt: bool = False
    interrupt_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingAction:
    name: str
    args: List[str]
    started_at: float
    duration: float
    completes_at: float
    # 动作前要走的路（世界格子，含起点）；客户端照着它在 duration 内走完
    path: Optional[List[Tuple[int, int]]] = None
    travel: bool = False
    # 路上要走多久（模拟秒）；客户端按它匀速走，不按整个动作时长压缩
    walk_seconds: float = 0.0
    # 只是站着把话说完：到点就结束，不执行动作、不推进计划
    hold: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "args": list(self.args),
            "started_at": self.started_at,
            "duration": self.duration,
            "completes_at": self.completes_at,
            "path": [list(c) for c in self.path] if self.path else None,
            "walk_seconds": self.walk_seconds,
        }


@dataclass
class AgentState:
    agent: AgentProfile
    current_plan: Optional[List[PlanStep]] = None
    plan_index: int = 0
    last_action_result: Optional[ActionResult] = None
    last_goal: Optional[Dict[str, Any]] = None
    is_interrupted: bool = False
    interrupt_count: int = 0
    plan_engine: str = ""
    plan_error: Optional[str] = None
    # 持续时钟字段
    sim_busy_until: float = 0.0
    pending_action: Optional[PendingAction] = None
    replan_backoff_until: float = 0.0
    decision_cooldown_until: float = 0.0
    status: str = "idle"  # idle | acting | deciding | planning | failed
    tried_goal_keys: List[str] = field(default_factory=list)
    replan_reason: Optional[str] = None
    mission_complete: bool = False
    mission_complete_reason: str = ""
    # UI overlay 缓存
    decision_mode: Optional[str] = None
    decision_process: Optional[Dict[str, Any]] = None
    candidate_goals: List[Dict[str, Any]] = field(default_factory=list)
    decision_steps: List[Dict[str, Any]] = field(default_factory=list)
    # 日常日程 / 社交打断
    routine_slot_key: Optional[str] = None
    last_social_at: float = -1e9
    recent_talk_targets: List[str] = field(default_factory=list)
    # once 槽位已完成："<day>:<slot key>"
    routine_done: List[str] = field(default_factory=list)
    # 持续状态（内容包 states/，如 asleep）：名字 / 进入时的日程段 / 进入时刻
    held_state: Optional[str] = None
    state_slot: Optional[str] = None
    state_since: float = 0.0
    # 决策触发器（日程换段 / 突发事件）
    observation: Optional[Any] = None
    pending_triggers: List[Any] = field(default_factory=list)
    last_triggers: List[Dict[str, Any]] = field(default_factory=list)
    next_scan_at: float = 0.0
    last_decision_at: float = -1e9
    # 被人叫住：有人正朝这边走来说话，在此之前原地等着（不开始新步骤 / 不做新决策）
    engaged_by: Optional[str] = None
    engaged_until: float = 0.0
    # 想找谁说话但对方正忙：在等谁（给 UI 看）
    waiting_for: Optional[str] = None
    # 卡住了几次、卡在什么上；背景 NPC 临时改用大模型想事情到什么时候
    stuck_count: int = 0
    stuck_on: str = ""
    escalated_until: float = 0.0
    # 上次观察（knowledge.observe）时所在地点；换地点时才更新「知道的地点」
    seen_loc: Optional[str] = None
    # 行为日志：决策 / 动作开始与结果 / 打断（最近 HISTORY_LIMIT 条）
    history: List[Dict[str, Any]] = field(default_factory=list)


HISTORY_LIMIT = 120


def _goal_text(goal_data: Optional[Dict[str, Any]]) -> str:
    if not goal_data:
        return "(no goal)"
    goal = goal_data.get("goal") or goal_data.get("final_goal") or "?"
    target = goal_data.get("target_entity")
    ia = goal_data.get("interact_action") or (goal_data.get("metadata") or {}).get("interact_action")
    text = f"{goal} -> {target}" if target else str(goal)
    return f"{text} [{ia}]" if ia else text


def _step_text(step: Any) -> str:
    try:
        name, args = step
        return f"{name}({', '.join(str(a) for a in args)})"
    except (TypeError, ValueError):
        return str(step)


def _goal_key(goal_data: Optional[Dict[str, Any]]) -> str:
    if not goal_data:
        return ""
    ia = (
        goal_data.get("interact_action")
        or (goal_data.get("metadata") or {}).get("interact_action")
        or ""
    )
    return f"{goal_data.get('goal')}|{goal_data.get('target_entity')}|{ia}"


class GameLoop:
    def __init__(
        self,
        world: "Neo4jWorld",
        *,
        decider_factory: Optional[Callable] = None,
        memory=None,
        max_turns: Optional[int] = None,
        verbose: bool = True,
        plan_engines: Optional[List[str]] = None,
        decision_cooldown: float = DECISION_COOLDOWN,
        replan_backoff: float = REPLAN_BACKOFF,
        plan_workers: int = PLAN_WORKERS,
    ):
        self.world = world
        self.memory = memory
        self.agents: List[AgentState] = []
        self.turn_count = 0  # 兼容：等同 tick_count
        self.tick_count = 0
        self.sim_time = 0.0
        self.max_turns = max_turns
        self.verbose = verbose
        self.plan_engines = plan_engines or ["unified_planning", "bfs"]
        self.decider_factory = decider_factory or self._default_decider_factory
        self.force_mock = False
        self.decision_cooldown = decision_cooldown
        self.replan_backoff = replan_backoff
        # 世界大事记：物品脚本 rule 的 announce（建成、解锁……）
        self.chronicle: List[Dict[str, Any]] = []
        self.plan_workers = max(1, int(plan_workers))
        self._plan_pool: Optional[ThreadPoolExecutor] = None
        # 还在后台决策的角色：name -> (state, future)；决策线程独占这个 AgentState，别的阶段先跳过
        self._inflight: Dict[str, Tuple[AgentState, Future]] = {}
        # 计划冲突：谁抢走了谁要的东西（最近 HISTORY_LIMIT 条，给 UI / Storyteller）
        self.conflicts: List[Dict[str, Any]] = []
        # 并行决策线程里发现的冲突先排队，主线程统一生效（会改别人的状态）
        self._conflict_queue: List[Dict[str, Any]] = []
        self._conflict_lock = threading.Lock()
        # 物品最近被谁动过：item -> (agent, action, sim_time)
        self._touched: Dict[str, Tuple[str, str, float]] = {}
        # 可选：定期推动剧情（见 storyteller.py）
        self.storyteller = None
        self._story_future: Optional[Future] = None
        # 每拍回调（例如任务线进度）：对象实现 on_tick(loop)；出错只记日志，不影响模拟
        self.hooks: List[Any] = []
        # 最近的对话（逐句台词），给客户端在头顶一句一句地放气泡
        self.conversations: List[Dict[str, Any]] = []
        # 实时运行时，真实 1 秒对应多少模拟秒（服务端按模拟节奏设置），用来让说话的人站够时间
        self.sim_per_wall: float = 1.0

    def _record_announcements(self, world_report: Dict[str, Any]) -> None:
        from routine import format_clock
        from triggers import Trigger

        for ev in world_report.get("events") or []:
            if not isinstance(ev, dict) or ev.get("type") != "announce" or not ev.get("text"):
                continue
            clock = format_clock(self.sim_time)
            self.chronicle.append({"t": round(self.sim_time, 2), "clock": clock, "text": ev["text"]})
            del self.chronicle[:-HISTORY_LIMIT]
            if self.verbose:
                print(f"[NEWS] {ev['text']}")
            for state in self.agents:
                self._log(state, "news", ev["text"])
                self._push_triggers(state, [Trigger(
                    "event", "news", f"{clock}: {ev['text']}", 0.5, self.sim_time,
                )])

    def _default_decider_factory(self, world, memory=None, force_mock=False):
        from multi_step_decider import MultiStepDecider

        return MultiStepDecider(world, memory=memory, force_mock=force_mock)

    def add_agent(
        self,
        agent: AgentProfile,
        decider=None,
        *,
        force_mock: bool = False,
    ) -> None:
        if self.get_agent_state(agent.name) is not None:
            return
        if self.memory:
            agent.set_memory(self.memory)
        if decider is None:
            decider = self.decider_factory(self.world, memory=self.memory, force_mock=force_mock)
        agent._decider = decider  # noqa: SLF001
        self.agents.append(AgentState(agent=agent))
        if self.verbose:
            print(f"[+] Added agent: {agent.name}")

    def get_agent_state(self, name: str) -> Optional[AgentState]:
        for state in self.agents:
            if state.agent.name == name:
                return state
        return None

    def _person_flags(self, state: AgentState) -> Optional[Dict[str, Any]]:
        # 同一条查询：tick 读缓存下 _is_player / _is_dead 共用一次读取
        rows = self.world.run_query(PERSON_FLAGS_QUERY, n=state.agent.name)
        return rows[0] if rows else None

    def preload_tick_reads(self) -> None:
        """tick 开头两条批量查询，把每个人 / 每件物品最常读的几条单条查询预先填进读缓存。

        要在 world.read_cache() 作用域里调用（WorldSimulator.tick）。之后谁写了 Person / Item，
        对应的缓存按标签失效，再读就照常单条查库。失败只打印，不影响这一拍。
        """
        try:
            self._preload_tick_reads()
        except Exception as e:
            if self.verbose:
                print(f"[WARN] tick preload: {e}")

    def _preload_tick_reads(self) -> None:
        seed = getattr(self.world, "seed_cache", None)
        if seed is None:
            return
        from action import properties_query
        from agent_profile import HP_QUERY, LOCATION_QUERY
        from neo4j_world import entity_query

        person_qs = (entity_query("Person"), properties_query("Person"), HP_QUERY, LOCATION_QUERY, PERSON_FLAGS_QUERY)
        ver = {q: self.world.cache_version(q) for q in (*person_qs, properties_query("Item"))}
        people = self.world.run_query(
            """
            MATCH (p:Person)
            OPTIONAL MATCH (p)-[:LOCATED_AT]->(l:Location)
            WITH p, collect(l.name) AS locs
            RETURN p.name AS name, properties(p) AS props, locs
            """
        )
        items = self.world.run_query("MATCH (n:Item) RETURN n.name AS name, properties(n) AS props")

        def _or(props: Dict[str, Any], key: str, default: Any) -> Any:
            v = props.get(key)
            return default if v is None else v

        for row in people:
            name, props = row["name"], row["props"] or {}
            if name is None:
                continue
            seed(entity_query("Person"), [{"n": props}], version=ver[entity_query("Person")], name=name)
            seed(properties_query("Person"), [{"p": props}], version=ver[properties_query("Person")], name=name)
            seed(HP_QUERY, [{"hp": _or(props, "hp", 0)}], version=ver[HP_QUERY], n=name)
            locs = row["locs"] or []
            seed(LOCATION_QUERY, [{"loc": locs[0]}] if locs else [], version=ver[LOCATION_QUERY], n=name)
            seed(PERSON_FLAGS_QUERY, [{
                "is_player": _or(props, "is_player", False),
                "role": _or(props, "role", ""),
                "dead": _or(props, "dead", False),
                "hp": _or(props, "hp", 0),
                "brain": _or(props, "brain", "llm"),
                "follows": _or(props, "follows", ""),
            }], version=ver[PERSON_FLAGS_QUERY], n=name)
        for row in items:
            if row["name"] is not None:
                seed(properties_query("Item"), [{"p": row["props"] or {}}],
                     version=ver[properties_query("Item")], name=row["name"])

    def _routine_only(self, state: AgentState) -> bool:
        """背景 NPC：只照日程行动（Person.brain = routine）——除非遇到急事或卡住了，暂时请大模型想办法。"""
        row = self._person_flags(state)
        if not (bool(row) and normalize_brain(row.get("brain")) == "routine"):
            return False
        if self.sim_time < state.escalated_until:
            return False
        from triggers import INTERRUPT_PRIORITY

        urgent = [t for t in state.pending_triggers if t.priority >= INTERRUPT_PRIORITY]
        if urgent and not self.force_mock and _llm_configured():
            self._escalate(state, f"something urgent: {urgent[0].text}")
            return False
        return True

    def _escalate(self, state: AgentState, why: str) -> None:
        """背景 NPC 临时改用大模型做决策（急事 / 卡住），过一阵自动回到只照日程。"""
        if self.sim_time < state.escalated_until:
            return
        state.escalated_until = self.sim_time + ESCALATE_FOR
        self._log(state, "state", f"Thinks it over properly ({why})", reason=why)
        from triggers import Trigger

        self._push_triggers(state, [Trigger("event", "stuck", f"You need to think: {why}", 0.7, self.sim_time)])

    def _note_progress(self, state: AgentState, wanted: Optional[Dict[str, Any]], picked: Any) -> None:
        """想做的事（日程 / 店 / 大模型的目标）做不成：记一次；够次数就挂委托，再多就请大模型。"""
        if wanted is None or self._is_player(state):
            return
        if picked is not None and _goal_key(picked.goal_data) == _goal_key(wanted):
            state.stuck_count, state.stuck_on = 0, ""
            return
        what = _goal_text(wanted)
        state.stuck_count = state.stuck_count + 1 if state.stuck_on == what else 1
        state.stuck_on = what
        target = str(wanted.get("target_entity") or "")
        if state.stuck_count >= REQUEST_AFTER and target.startswith("kind:"):
            self._post_request(state, target[len("kind:"):], str(wanted.get("reason") or what))
        if state.stuck_count >= STUCK_LIMIT and not self.force_mock and _llm_configured():
            row = self._person_flags(state) or {}
            if normalize_brain(row.get("brain")) == "routine":
                self._escalate(state, f"stuck on {what}")

    def _post_request(self, state: AgentState, kind: str, why: str) -> None:
        from job_board import post

        try:
            req = post(self.world, state.agent.name, kind, at=self.sim_time, reason=why)
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"[WARN] request {state.agent.name}: {e}")
            return
        if not req:
            return
        pay = f", paying {req['reward']} coins" if req["reward"] else ""
        text = f"{state.agent.name} is asking for {kind}{pay}."
        self._log(state, "event", f"Posted a request: need {kind}{pay}", reason=why)
        # 公告：所有人（包括玩家）都听得到，谁能帮就来
        self._record_announcements({"events": [{"type": "announce", "text": text}]})

    def _leader(self, state: AgentState) -> str:
        """组队：这个人在跟着谁走（没有 = ""）。"""
        row = self._person_flags(state)
        return str(row.get("follows") or "") if row else ""

    def _rule_based(self, state: AgentState) -> bool:
        """这个人这一拍不用大模型做决策：规则模式，或者他本来就是只照日程的 NPC。"""
        return self.force_mock or self._routine_only(state)

    def _rule_based_name(self, agent: AgentProfile) -> bool:
        state = self.get_agent_state(agent.name)
        return self.force_mock if state is None else self._rule_based(state)

    def _is_player(self, state: AgentState) -> bool:
        row = self._person_flags(state)
        if not row:
            return False
        return bool(row.get("is_player")) or str(row.get("role") or "") == "player"

    def _is_dead(self, state: AgentState) -> bool:
        row = self._person_flags(state)
        if not row:
            return True
        return bool(row.get("dead")) or int(row.get("hp") or 0) <= 0

    def run_turn(
        self,
        *,
        auto_plan: bool = True,
        characters: Optional[List[str]] = None,
        include_player: bool = False,
    ) -> Dict[str, Any]:
        """兼容旧回合 API：一次 tick，动作立即完成（instant）。"""
        return self.tick(
            1.0,
            auto_plan=auto_plan,
            characters=characters,
            instant=True,
            include_player=include_player,
        )

    def tick(
        self,
        dt: float = 0.25,
        *,
        auto_plan: bool = True,
        characters: Optional[List[str]] = None,
        instant: bool = False,
        include_player: bool = False,
        max_decisions_per_tick: Optional[int] = None,
    ) -> Dict[str, Any]:
        # 一拍内各角色反复读同样的图数据；写操作会自动让缓存失效
        read_cache = getattr(self.world, "read_cache", None)
        with read_cache() if read_cache else nullcontext():
            return self._tick(
                dt,
                auto_plan=auto_plan,
                characters=characters,
                instant=instant,
                include_player=include_player,
                max_decisions_per_tick=max_decisions_per_tick,
            )

    def _tick(
        self,
        dt: float,
        *,
        auto_plan: bool,
        characters: Optional[List[str]],
        instant: bool,
        include_player: bool,
        max_decisions_per_tick: Optional[int],
    ) -> Dict[str, Any]:
        self.sim_time += max(0.0, float(dt))
        self.tick_count += 1
        self.turn_count = self.tick_count
        world_report: Dict[str, Any] = {}
        try:
            from world_systems import tick_world_systems

            world_report = tick_world_systems(self.world, float(dt), self.sim_time) or {}
        except Exception as e:
            if self.verbose:
                print(f"[WARN] world_systems: {e}")
        self._record_announcements(world_report)
        self._story_step()
        self._relations_step()
        self._economy_step()
        self._run_hooks()
        if self.verbose:
            print(f"\n{'=' * 60}")
            clock = world_report.get("clock") or f"t={self.sim_time:.2f}"
            print(f"[TICK] #{self.tick_count} {clock} dt={dt}")
            alerts = world_report.get("alerts") or []
            if alerts:
                a0 = alerts[0]
                print(f"   script alert: [{a0.get('signal')}] {a0.get('item')}: {a0.get('message')}")
            print(f"{'=' * 60}\n")

        results = {
            "turn": self.tick_count,
            "tick": self.tick_count,
            "sim_time": self.sim_time,
            "world": world_report,
            "agents": [],
        }
        targets = self.agents
        if characters is not None:
            char_set = set(characters)
            targets = [s for s in self.agents if s.agent.name in char_set]

        if auto_plan:
            for state in targets:
                if state.agent.name in self._inflight:
                    continue
                if not self._may_auto_plan(state, include_player=include_player):
                    continue
                try:
                    self._scan_triggers(state, world_report.get("alerts") or [])
                except Exception as e:
                    if self.verbose:
                        print(f"[WARN] trigger scan {state.agent.name}: {e}")

        # 两阶段：先推进所有到期动作/计划步骤，再做决策。
        # 避免某个角色的 LLM/PDDL 卡住整拍，导致其他人的 move 永远完不成。
        by_name: Dict[str, Dict[str, Any]] = {}
        for state in targets:
            if state.agent.name in self._inflight:
                continue
            try:
                by_name[state.agent.name] = self._process_agent_tick(
                    state,
                    auto_plan=False,
                    instant=instant,
                    include_player=include_player,
                )
            except Exception as e:
                if self.verbose:
                    print(f"[ERR] Agent {state.agent.name} tick failed: {e}")
                state.status = "failed"
                # 防止执行异常后 pending 永远挂起
                if state.pending_action and self.sim_time >= state.pending_action.completes_at:
                    state.pending_action = None
                    state.sim_busy_until = 0.0
                by_name[state.agent.name] = {
                    "agent": state.agent.name,
                    "error": str(e),
                }

        # 刚落地的动作可能让别人的计划失效：先通知 / 打断他们，本拍就能重规划
        self._flush_conflicts()

        if auto_plan:
            limit = self.plan_workers if max_decisions_per_tick is None else max(0, int(max_decisions_per_tick))
            if not instant and self.plan_workers > 1:
                # 后台还在算的占着线程；没有空位就这拍不派新决策（_pick_deciders 的 limit=0 是不限）
                free = self.plan_workers - len(self._inflight)
                limit = min(limit, free) if limit else free
                deciders = (
                    self._pick_deciders(targets, include_player=include_player, limit=limit)
                    if limit > 0
                    else []
                )
            else:
                deciders = self._pick_deciders(targets, include_player=include_player, limit=limit)
            by_name.update(self._run_decisions(deciders, instant=instant, include_player=include_player))
        self._flush_conflicts()

        results["agents"] = [by_name[s.agent.name] for s in targets if s.agent.name in by_name]
        return results

    def _pick_deciders(
        self,
        targets: List[AgentState],
        *,
        include_player: bool,
        limit: int,
    ) -> List[AgentState]:
        """本拍需要决策 / 重规划的角色（最多 limit 个；0 = 不限）。"""
        out: List[AgentState] = []
        for state in targets:
            if limit and len(out) >= limit:
                break
            if state.agent.name in self._inflight:
                continue
            if state.pending_action or self._engaged(state) or self._leader(state):
                continue
            if self.__dict__.get("_combat_until", {}).get(state.agent.name, -1.0) > self.sim_time:
                continue  # 正在打 / 逃：战斗反应说了算
            in_plan = bool(state.current_plan and state.plan_index < len(state.current_plan))
            # 第一轮里下一步预检失败（status=failed）的计划也要在这一轮重规划
            if in_plan and not self._wants_interrupt(state) and state.status != "failed":
                continue
            if not in_plan and not self._wants_decision(state):
                continue
            if not self._may_auto_plan(state, include_player=include_player):
                continue
            urgent = self._wants_interrupt(state) or in_plan
            if self.sim_time < state.replan_backoff_until and not urgent:
                continue
            if (
                self.sim_time < state.decision_cooldown_until
                and not state.is_interrupted
                and not urgent
            ):
                continue
            out.append(state)
        return out

    def _decide_one(self, state: AgentState, *, instant: bool, include_player: bool) -> Dict[str, Any]:
        try:
            return self._process_agent_tick(
                state,
                auto_plan=True,
                instant=instant,
                include_player=include_player,
            )
        except Exception as e:
            if self.verbose:
                print(f"[ERR] Agent {state.agent.name} decision tick failed: {e}")
            state.status = "failed"
            return {"agent": state.agent.name, "error": str(e)}

    def _run_decisions(
        self,
        deciders: List[AgentState],
        *,
        instant: bool,
        include_player: bool,
    ) -> Dict[str, Dict[str, Any]]:
        """各角色的决策 + PDDL 规划互不依赖，并行跑；每个线程只改自己的 AgentState。

        instant 模式下决策后会立刻执行动作（写世界），为避免两人同时改同一物品，仍串行。
        非 instant 模式只启动动作，真正落地在下一拍第一阶段串行完成，冲突由预检 + 重规划处理。
        """
        if instant or self.plan_workers <= 1:
            return {
                s.agent.name: self._decide_one(s, instant=instant, include_player=include_player)
                for s in deciders
            }
        # 慢的规划（ENHSP 子进程要 1–3 秒）不卡整拍：最多等 DECISION_WAIT_SEC，
        # 没做完的留在 _inflight，之后哪一拍做完了就在哪一拍收结果
        pool = self._pool()
        for s in deciders:
            self._inflight[s.agent.name] = (
                s,
                pool.submit(self._decide_one, s, instant=instant, include_player=include_player),
            )
        if DECISION_WAIT_SEC > 0:
            wait([f for _, f in self._inflight.values()], timeout=DECISION_WAIT_SEC)
        out: Dict[str, Dict[str, Any]] = {}
        for name, (_, f) in list(self._inflight.items()):
            if f.done():
                del self._inflight[name]
                out[name] = f.result()  # _decide_one 自己兜住异常
        return out

    def wait_decisions(self, timeout: Optional[float] = None) -> None:
        """等后台决策做完（存档 / 读档前调用，免得存下改了一半的 AgentState）；结果仍由下一拍收。"""
        if self._inflight:
            wait([f for _, f in self._inflight.values()], timeout=timeout)

    def close(self) -> None:
        self._inflight.clear()
        if self._plan_pool is not None:
            self._plan_pool.shutdown(wait=False)
            self._plan_pool = None

    def run(self, max_turns: Optional[int] = None) -> Dict[str, Any]:
        limit = max_turns if max_turns is not None else self.max_turns
        game_result = {"total_turns": 0, "turns": [], "final_state": {}}
        while True:
            if limit and self.tick_count >= limit:
                if self.verbose:
                    print(f"\n[END] Reached max tick count {limit}, stopping")
                break
            turn = self.run_turn()
            game_result["turns"].append(turn)
            game_result["total_turns"] = self.tick_count
        game_result["final_state"] = {
            s.agent.name: {
                "location": s.agent.location,
                "hp": s.agent.hp,
                "plan_remaining": len(s.current_plan or []) - s.plan_index
                if s.current_plan
                else 0,
                "status": s.status,
                "sim_busy_until": s.sim_busy_until,
            }
            for s in self.agents
        }
        return game_result

    def _may_auto_plan(self, state: AgentState, *, include_player: bool) -> bool:
        if self._is_dead(state):
            return False
        if self._is_player(state) and not include_player:
            return False
        return True

    def _process_agent_tick(
        self,
        state: AgentState,
        *,
        auto_plan: bool = True,
        instant: bool = False,
        include_player: bool = False,
    ) -> Dict[str, Any]:
        agent = state.agent
        result: Dict[str, Any] = {
            "agent": agent.name,
            "character": agent.name,
            "phase": None,
            "goal": None,
            "action": None,
            "interrupted": False,
            "skipped": False,
            "sim_time": self.sim_time,
            "status": state.status,
        }
        can_plan = auto_plan and self._may_auto_plan(state, include_player=include_player)

        if not state.pending_action and self._state_gate(state):
            state.status = state.held_state or "idle"
            result["skipped"] = True
            result["reason"] = f"state:{state.held_state}"
            result["status"] = state.status
            return result

        if not auto_plan and not state.current_plan and not state.pending_action:
            result["skipped"] = True
            result["reason"] = "idle"
            state.status = "idle"
            return result

        agent.refresh_from_world()

        # 战斗：挨打了 / 敌人就在眼前 → 先打或先逃（规则反应，不等大模型）
        if (
            not instant
            and not self._is_player(state)
            and not self._is_dead(state)
            and self._may_auto_plan(state, include_player=include_player)
            and self._combat_step(state)
        ):
            result["phase"] = "combat"
            result["goal"] = state.last_goal
            result["status"] = state.status
            return result

        # 硬中断：死亡 / 极低 HP — 可取消进行中动作（不 apply）
        if self._hard_interrupt(state):
            result["interrupted"] = True
            result["phase"] = "hard_interrupt"
            if can_plan and not self._is_dead(state):
                self._failure_replan(state, reason=state.replan_reason or "Hard interrupt", instant=instant)
                result["goal"] = state.last_goal
            result["status"] = state.status
            return result

        # 软打断（规则模式）：日程换段 / 同地遇见他人 → 改计划（不取消进行中动作）
        # LLM 模式下这些都是触发器，交给 LLM 决策
        if (
            can_plan
            and self._rule_based(state)
            and not state.pending_action
            and not self._is_dead(state)
            and not self._engaged(state)
            and self._soft_opportunity_interrupt(state, instant=instant)
        ):
            result["interrupted"] = True
            result["phase"] = "opportunity_interrupt"
            result["goal"] = state.last_goal
            result["replan_reason"] = state.replan_reason
            result["status"] = state.status
            # 打断后立刻开跑新计划第一步
            if state.current_plan and state.plan_index < len(state.current_plan):
                started = self._start_next_step(state, instant=instant)
                if started:
                    result["phase"] = "started"
                    result["action"] = started
                    if instant and state.pending_action:
                        action_entry = self._complete_pending(state)
                        result["phase"] = "executing"
                        result["action"] = action_entry
                        result["success"] = action_entry.get("success") if action_entry else None
                        result["message"] = action_entry.get("message") if action_entry else None
            return result

        # 完成到期动作
        if state.pending_action and self.sim_time >= state.pending_action.completes_at:
            result["phase"] = "completing"
            action_entry = self._complete_pending(state)
            result["action"] = action_entry
            result["success"] = action_entry.get("success") if action_entry else None
            result["message"] = action_entry.get("message") if action_entry else None
            if action_entry and action_entry.get("dialogue"):
                result["dialogue"] = action_entry["dialogue"]

            if state.last_action_result and not state.last_action_result.success:
                if can_plan:
                    self._failure_replan(
                        state,
                        reason=state.last_action_result.message,
                        instant=instant,
                    )
                    result["goal"] = state.last_goal
                    result["interrupted"] = True
                result["status"] = state.status
                return result

        # 仍在忙碌
        if state.pending_action and self.sim_time < state.pending_action.completes_at:
            state.status = "acting"
            result["skipped"] = True
            result["reason"] = "busy"
            result["phase"] = "acting"
            result["pending"] = state.pending_action.to_dict()
            result["busy_remaining"] = max(0.0, state.sim_busy_until - self.sim_time)
            result["status"] = state.status
            return result

        # 组队：跟着队长走，不自己做决定
        leader = self._leader(state)
        if leader and not instant and not self._is_player(state) and self.get_agent_state(leader) is not None:
            self._follow_step(state, leader)
            result["phase"] = "following"
            result["status"] = state.status
            return result

        # 被叫住：等来找自己说话的人走过来
        if self._engaged(state):
            state.status = "waiting"
            result["skipped"] = True
            result["reason"] = "engaged"
            result["phase"] = "waiting"
            result["waiting_for"] = state.engaged_by
            result["status"] = state.status
            return result

        # 有剩余计划：步骤间隙若有紧急事件，先让 LLM 决定要不要改主意
        if (
            state.current_plan
            and state.plan_index < len(state.current_plan)
            and self._wants_interrupt(state)
        ):
            if not can_plan:
                state.status = "deciding"
                result["skipped"] = True
                result["reason"] = "awaiting_decision"
                result["status"] = state.status
                return result
            decided = self._decide_and_plan(state, instant=instant)
            result["decided"] = True
            result["interrupted"] = not decided.get("kept")
            result["goal"] = state.last_goal

        # 有剩余计划：启动下一步
        if state.current_plan and state.plan_index < len(state.current_plan):
            started = self._start_next_step(state, instant=instant)
            if started is None:
                # 预检失败 → 重规划
                if can_plan:
                    self._failure_replan(
                        state,
                        reason=state.replan_reason or "Next step precheck failed",
                        instant=instant,
                    )
                    result["goal"] = state.last_goal
                    result["interrupted"] = True
                result["status"] = state.status
                return result
            result["phase"] = "started"
            result["action"] = started
            # instant：同 tick 内立即完成
            if instant and state.pending_action:
                action_entry = self._complete_pending(state)
                result["phase"] = "executing"
                result["action"] = action_entry
                result["success"] = action_entry.get("success") if action_entry else None
                result["message"] = action_entry.get("message") if action_entry else None
                if action_entry and action_entry.get("dialogue"):
                    result["dialogue"] = action_entry["dialogue"]
                if state.last_action_result and not state.last_action_result.success:
                    if can_plan:
                        self._failure_replan(
                            state,
                            reason=state.last_action_result.message,
                            instant=instant,
                        )
                        result["interrupted"] = True
            result["status"] = state.status
            result["goal"] = state.last_goal
            return result

        # 空闲：决策 + 规划（默认跳过玩家，除非 include_player）
        if not auto_plan:
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "idle"
            result["status"] = state.status
            return result

        if self._is_dead(state):
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "player_or_dead"
            result["status"] = state.status
            return result

        if self._is_player(state) and not include_player:
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "player_or_dead"
            result["status"] = state.status
            return result

        # 任务节点仅软同步 UI；不因「任务完成」硬停自动规划
        self._sync_quest_ui(state)

        if not self._wants_decision(state):
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "awaiting_trigger"
            result["status"] = state.status
            return result

        urgent = self._wants_interrupt(state)
        if self.sim_time < state.replan_backoff_until and not urgent:
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "backoff"
            result["status"] = state.status
            return result

        if self.sim_time < state.decision_cooldown_until and not state.is_interrupted and not urgent:
            state.status = "idle"
            result["skipped"] = True
            result["reason"] = "decision_cooldown"
            result["status"] = state.status
            return result

        decided = self._decide_and_plan(state, instant=instant)
        result["phase"] = "deciding"
        result["goal"] = state.last_goal
        result.update(decided)
        result["status"] = state.status

        # 决策后立即启动第一步（若有计划）
        if state.current_plan and state.plan_index < len(state.current_plan):
            started = self._start_next_step(state, instant=instant)
            if started:
                result["phase"] = "started"
                result["action"] = started
                if instant and state.pending_action:
                    action_entry = self._complete_pending(state)
                    result["phase"] = "executing"
                    result["action"] = action_entry
                    result["success"] = action_entry.get("success") if action_entry else None
                    result["message"] = action_entry.get("message") if action_entry else None
        return result

    def _hard_interrupt(self, state: AgentState) -> bool:
        agent = state.agent
        if self._is_dead(state):
            self._cancel_pending(state, apply=False)
            state.current_plan = None
            state.plan_index = 0
            state.is_interrupted = True
            state.interrupt_count += 1
            state.status = "failed"
            state.replan_reason = "Character is dead"
            return True
        if agent.hp < HARD_INTERRUPT_HP and (state.pending_action or state.current_plan):
            # 进行中才硬打断
            if state.pending_action:
                self._cancel_pending(state, apply=False)
            state.current_plan = None
            state.plan_index = 0
            state.is_interrupted = True
            state.interrupt_count += 1
            state.replan_reason = "HP too low"
            state.status = "failed"
            return True
        return False

    def _cancel_pending(self, state: AgentState, *, apply: bool) -> None:
        if apply and state.pending_action:
            self._complete_pending(state)
            return
        state.pending_action = None
        state.sim_busy_until = 0.0

    def _start_next_step(
        self,
        state: AgentState,
        *,
        instant: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if not state.current_plan or state.plan_index >= len(state.current_plan):
            return None
        action_name, args = state.current_plan[state.plan_index]
        if any(str(a).startswith("kind:") for a in args):
            from item_chain import resolve_step_args

            args = resolve_step_args(self.world, state.agent.name, list(args))
            state.current_plan[state.plan_index] = (action_name, args)
        ok, reason = self._check_plan_step(state.agent, action_name, args)
        if not ok:
            state.replan_reason = reason
            state.plan_error = reason
            state.status = "failed"
            if self.verbose:
                print(f"[WARN] [{state.agent.name}] precheck failed: {reason}")
            self._queue_failure_conflict(state, action_name, args, reason)
            return None

        partner = None if instant else self._talk_partner(state, action_name, list(args))
        if partner is not None and (partner.pending_action or self._engaged(partner, besides=state.agent.name)):
            return self._wait_for_partner(state, partner)

        duration = get_action_duration(
            action_name, instant=instant, args=list(args), world=self.world
        )
        walk, travel = self._plan_walk(state.agent.name, action_name, list(args))
        if walk is not None and not instant:
            # 赶路的动作就是走路本身；其余动作先走到目标旁边再做
            duration = max(0.3, walk.seconds) if travel else duration + walk.seconds
        pending = PendingAction(
            name=action_name,
            args=list(args),
            started_at=self.sim_time,
            duration=duration,
            completes_at=self.sim_time + duration,
            path=list(walk.path) if walk else None,
            travel=travel,
            walk_seconds=min(walk.seconds, duration) if walk is not None and not instant else 0.0,
        )
        state.pending_action = pending
        state.sim_busy_until = pending.completes_at
        state.status = "acting"
        state.waiting_for = None
        if partner is not None:
            # 对方站着等我走过去，说完（或超时）就放开
            partner.engaged_by = state.agent.name
            partner.engaged_until = pending.completes_at + ENGAGE_MARGIN
        if action_name != act("idle"):
            self._log(
                state,
                "start",
                _step_text((action_name, args)),
                goal=_goal_text(state.last_goal) if state.last_goal else None,
                step=f"{state.plan_index + 1}/{len(state.current_plan)}",
                action=action_name,
                args=list(args),
            )
        if self.verbose:
            print(
                f"[START] [{state.agent.name}] {action_name}({', '.join(args)}) "
                f"duration={duration:.2f} until={pending.completes_at:.2f}"
            )
        return {
            "action": action_name,
            "args": args,
            "started": True,
            "duration": duration,
            "completes_at": pending.completes_at,
            "step": state.plan_index + 1,
        }

    def _complete_pending(self, state: AgentState) -> Optional[Dict[str, Any]]:
        pending = state.pending_action
        if not pending:
            return None
        if pending.hold:
            state.pending_action = None
            state.sim_busy_until = 0.0
            state.status = "idle"
            if pending.path:
                # 只是走过去（跟着队长）：走到了就站在那里
                from spatial import set_person_cell

                set_person_cell(self.world, state.agent.name, tuple(pending.path[-1]))
            return None
        agent = state.agent
        action_name, args = pending.name, pending.args
        if self.verbose:
            print(
                f"[EXEC] [{agent.name}] completed {state.plan_index + 1}/"
                f"{len(state.current_plan or [])}: {action_name}({', '.join(args)})"
            )
        inventory_before = None
        if state.observation is not None:
            from triggers import held_items

            inventory_before = held_items(self.world, agent.name)
        from conflicts import item_names

        item_args = item_names(self.world, args)
        try:
            action_result = self._execute_plan_step(agent, action_name, args)
        except Exception as e:
            action_result = ActionResult(
                success=False,
                message=f"Execution error: {e}",
                agent_name=agent.name,
                action_type=action_name,
            )
        finally:
            # 无论 apply 是否抛错，都必须清掉 pending，否则会永远卡在「执行 move」
            state.pending_action = None
            state.sim_busy_until = 0.0
        try:
            self._absorb_own_changes(state, inventory_before)
        except Exception:
            pass
        if action_result.success:
            self._feelings_after(agent, action_name, args)
            self._list_new_goods(state)
        if pending.path and (action_result.success or not pending.travel):
            # 走到了（赶路失败的人还留在原区域，不挪）
            from spatial import set_person_cell

            set_person_cell(self.world, agent.name, tuple(pending.path[-1]))
        self._observe(state, places=True)

        state.last_action_result = action_result
        state.plan_index += 1
        self._remember_action(agent, action_name, args, action_result)
        if action_result.success:
            self._note_items_taken(agent.name, action_name, item_args)
        else:
            self._queue_failure_conflict(state, action_name, args, action_result.message)

        entry = {
            "action": action_name,
            "args": args,
            "type": action_result.action_type,
            "target": action_result.target,
            "success": action_result.success,
            "message": action_result.message,
            "step": state.plan_index,
        }
        if action_result.metadata.get("dialogue"):
            entry["dialogue"] = action_result.metadata["dialogue"]
        if action_result.metadata.get("interaction"):
            entry["interaction"] = action_result.metadata["interaction"]
        dialogue = action_result.metadata.get("dialogue")
        lines = dialogue.get("lines") if isinstance(dialogue, dict) else None
        said = [
            f"{ln.get('speaker')}: {ln.get('text')}"
            for ln in lines or []
            if isinstance(ln, dict) and ln.get("text")
        ]
        if action_name != act("idle") or not action_result.success:
            self._log(
                state,
                "done" if action_result.success else "failed",
                _step_text((action_name, args)),
                message=action_result.message,
                dialogue=said,
                action=action_name,
                args=list(args),
            )
        if action_name == act("converse"):
            for name in args[:2]:
                talker = self.get_agent_state(name)
                if talker is not None and talker.engaged_by in args[:2]:
                    talker.engaged_by, talker.engaged_until = None, 0.0
        if action_name == act("converse") and action_result.success and len(args) >= 2 and lines:
            self.record_conversation(args[0], args[1], args[2] if len(args) > 2 else "", lines)
            if pending.duration > 0:  # instant 模式（旧回合 API）不等
                self._hold_while_talking([args[0], args[1]], lines)
        if action_name == act("converse") and action_result.success and len(args) >= 2:
            # 被搭话的人也记下这段对话（他的历史里同样要看得到说了什么）
            listener = self.get_agent_state(args[1] if args[0] == agent.name else args[0])
            if listener is not None and listener is not state:
                self._log(
                    listener, "done", _step_text((action_name, args)),
                    message=action_result.message, dialogue=said, action=action_name, args=list(args),
                    own_step=False, why=f"{agent.name} came over to talk",
                )

        if action_name == act("converse") and action_result.success and len(args) >= 2:
            # 两个人聊完都想一想要不要改主意（对方是玩家时等玩家回话，回了再说）
            a_name, b_name = args[0], args[1]
            b_state = self.get_agent_state(b_name)
            to_player = b_state is not None and self._is_player(b_state)
            if not to_player:
                self.heard(a_name, b_name, lines or [])
                self.heard(b_name, a_name, lines or [])
        if action_name == act("converse") and action_result.success and len(args) >= 2:
            other = args[1] if args[0] == agent.name else args[0]
            if other and other != agent.name:
                state.recent_talk_targets = [
                    t for t in state.recent_talk_targets if t != other
                ][-4:]
                state.recent_talk_targets.append(other)
                state.last_social_at = self.sim_time

        if self.verbose:
            icon = "OK" if action_result.success else "FAIL"
            print(f"   {icon} {action_result.message}")

        if action_result.success and state.current_plan and state.plan_index >= len(state.current_plan):
            if self.verbose:
                print(f"[DONE] [{agent.name}] plan complete")
            self._mark_routine_done(state)
            agent.complete_current_goal()
            last_actions = {a for a, _ in state.current_plan}
            if self.trigger_driven and last_actions != {"wait"}:
                from triggers import Trigger

                goal = state.last_goal or {}
                self._push_triggers(state, [Trigger(
                    "event", "plan_done",
                    f"You finished: {goal.get('goal')} -> {goal.get('target_entity')}.",
                    0.4, self.sim_time,
                )])
            state.current_plan = None
            state.plan_index = 0
            state.status = "idle"
            state.decision_cooldown_until = self.sim_time + self.decision_cooldown
            state.tried_goal_keys.clear()
            state.replan_reason = None
            self._sync_quest_ui(state)
        elif action_result.success:
            state.status = "idle"
            self._sync_quest_ui(state)
        else:
            state.status = "failed"
            state.is_interrupted = True

        return entry

    def _sync_quest_ui(self, state: AgentState) -> None:
        """更新 Quest stage 供 UI；不设置 mission_complete 硬停。"""
        agent = state.agent
        try:
            from mission import sync_quest_progress

            note = sync_quest_progress(self.world, agent.name)
            if note and self.verbose:
                print(f"[QUEST] [{agent.name}] {note}")
        except Exception:
            pass
        # 兼容字段：不再用于跳过 tick
        state.mission_complete = False
        state.mission_complete_reason = ""

    def all_auto_agents_mission_complete(
        self,
        *,
        include_player: bool = False,
        characters: Optional[List[str]] = None,
    ) -> bool:
        """不再自动停仿真；由玩家停止，或由角色自行改目标。"""
        return False

    def _refresh_mission_complete(self, state: AgentState) -> bool:
        """兼容旧名：只做 UI 同步，永不返回「应停规划」。"""
        self._sync_quest_ui(state)
        return False

    def _plan_walk(self, actor: str, action_name: str, args: List[str]):
        """(Walk | None, 是否赶路动作)；没有布局 / 不用走 / 出错都当不用走。"""
        try:
            from content import get_content
            from spatial import plan_walk

            travel = get_content().capability_of(action_name) == "travel"
            return plan_walk(self.world, actor, action_name, args), travel
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"[WARN] plan_walk {actor} {action_name}: {e}")
            return None, False

    def _check_plan_step(
        self,
        agent: AgentProfile,
        action_name: str,
        args: List[str],
    ) -> Tuple[bool, str]:
        from registry import get_registry

        registry = get_registry()
        action = registry.get(action_name)
        if action is None:
            if action_name in ("noop",):
                return True, "ok"
            return False, f"Unknown action: {action_name}"
        kwargs = {p.name: v for p, v in zip(action.parameters, args)}
        return action.check(self.world, **kwargs)

    def _soft_opportunity_interrupt(
        self,
        state: AgentState,
        *,
        instant: bool = False,
    ) -> bool:
        """篝火告急 / 日程换段 / 同地遇见 → 清空当前计划并立刻采纳新目标。"""
        from routine import (
            current_slot,
            load_person_routine,
            pick_social_target,
            routine_goal_candidate,
            should_social_interrupt,
            social_interrupt_goal,
        )
        from world_systems import urgent_alert_goal

        agent = state.agent

        # 0) 物品脚本告警（缺货 / 有订单 / 燃料不足…）；是否分内事由 alert.audience 决定
        fire_goal = urgent_alert_goal(self.world, agent.name, sim_time=self.sim_time)
        if fire_goal:
            same = (
                state.last_goal
                and state.last_goal.get("goal") == fire_goal.get("goal")
                and state.last_goal.get("target_entity") == fire_goal.get("target_entity")
                and state.current_plan
            )
            if not same:
                assessed = assess_goal(
                    self.world, agent.name, fire_goal, engines=self.plan_engines
                )
                if assessed.ok and not assessed.already_satisfied and assessed.plan:
                    if self.verbose:
                        print(f"[SCRIPT] [{agent.name}] {fire_goal.get('reason')}")
                    self._adopt_planned_goal(
                        state, assessed, reason=fire_goal.get("reason") or "Item script alert"
                    )
                    return True

        slots = load_person_routine(self.world, agent.name)
        if not slots:
            return False
        slot = current_slot(slots, self.sim_time)
        if slot is None:
            return False

        slot_changed = state.routine_slot_key is not None and state.routine_slot_key != slot.key
        state.routine_slot_key = slot.key

        # 1) 同地社交机会（优先级高于「继续旧日程」）
        social_target = pick_social_target(
            self.world,
            agent.name,
            agent.location,
            recent_talk_targets=state.recent_talk_targets,
        )
        if should_social_interrupt(
            slot=slot,
            current_goal=state.last_goal,
            social_target=social_target,
            last_social_at=state.last_social_at,
            sim_time=self.sim_time,
        ):
            goal = social_interrupt_goal(social_target or "", activity=slot.activity)
            assessed = assess_goal(self.world, agent.name, goal, engines=self.plan_engines)
            if assessed.ok and not assessed.already_satisfied and assessed.plan:
                if self.verbose:
                    print(f"[MEET] [{agent.name}] met {social_target}; switching to talk")
                self._adopt_planned_goal(state, assessed, reason=goal.get("reason") or "Met someone to talk to")
                state.last_social_at = self.sim_time
                return True

        # 2) 日程时段切换：离开旧计划，跟随新槽位
        if not slot_changed:
            return False
        if not state.current_plan and not state.last_goal:
            return False
        if self._routine_slot_done(state, slot):
            return False

        cand = routine_goal_candidate(
            slot,
            character=agent.name,
            current_location=agent.location,
        )
        # 社交槽位已到场但尚无对象：不必打断
        if cand.get("goal") == "Gain information" and not cand.get("target_entity"):
            social_target = pick_social_target(
                self.world,
                agent.name,
                agent.location,
                recent_talk_targets=state.recent_talk_targets,
            )
            if social_target:
                cand = social_interrupt_goal(social_target, activity=slot.activity)
            else:
                cand = {
                    "goal": "Go to location",
                    "target_entity": slot.location,
                    "reason": f"Routine slot change: go to {slot.location}",
                    "priority": 0.72,
                    "confidence": 0.8,
                    "source": "routine",
                }

        # 与当前目标相同则不打断
        if (
            state.last_goal
            and state.last_goal.get("goal") == cand.get("goal")
            and state.last_goal.get("target_entity") == cand.get("target_entity")
        ):
            return False

        assessed = assess_goal(self.world, agent.name, cand, engines=self.plan_engines)
        if not assessed.ok or assessed.already_satisfied:
            return False
        if not assessed.plan:
            return False
        if self.verbose:
            print(
                f"[ROUTINE] [{agent.name}] routine slot change → "
                f"{cand.get('goal')}->{cand.get('target_entity')} ({slot.activity})"
            )
        self._adopt_planned_goal(
            state,
            assessed,
            reason=cand.get("reason") or "Routine slot change",
        )
        return True

    # ----- 决策触发器 -----

    @property
    def trigger_driven(self) -> bool:
        """LLM 模式：只在触发器出现时决策；规则模式沿用每拍规则判断。"""
        return not self.force_mock

    def _wants_decision(self, state: AgentState) -> bool:
        if self._rule_based(state):
            return True
        return bool(state.pending_triggers)

    def _wants_interrupt(self, state: AgentState) -> bool:
        if self._rule_based(state):
            return False
        from triggers import INTERRUPT_PRIORITY

        return any(t.priority >= INTERRUPT_PRIORITY for t in state.pending_triggers)

    def _push_triggers(self, state: AgentState, new: List[Any]) -> None:
        from triggers import MAX_QUEUE

        if not new:
            return
        queue = {t.key(): t for t in state.pending_triggers}
        for t in new:
            queue[t.key()] = t
            if self.verbose:
                print(f"[TRIGGER] [{state.agent.name}] ({t.source}/{t.kind}) {t.text}")
        state.pending_triggers = sorted(queue.values(), key=lambda t: (-t.priority, -t.at))[:MAX_QUEUE]

    def _scan_triggers(self, state: AgentState, alerts: List[Dict[str, Any]]) -> None:
        from routine import DAY_LENGTH_SECONDS, format_clock
        from triggers import SCAN_INTERVAL, Trigger, diff_triggers, observe

        if self.sim_time < state.next_scan_at:
            return
        state.next_scan_at = self.sim_time + SCAN_INTERVAL
        agent = state.agent
        agent.refresh_from_world()
        self._observe(state, agent.location)
        clock = format_clock(self.sim_time)
        obs, mine, slot = observe(
            self.world,
            agent.name,
            location=agent.location,
            hp=agent.hp,
            sim_time=self.sim_time,
            alerts=alerts,
        )
        new = diff_triggers(
            state.observation, obs, alerts=mine, slot=slot, clock=clock, sim_time=self.sim_time
        )
        held = self._held_state_def(state)
        if held is not None and state.observation is not None:
            from dataclasses import replace

            new = [t for t in new if t.priority >= held.wake_priority]
            # 状态中错过的换段 / 信号，结束后再触发
            obs = replace(obs, slot=state.observation.slot, alerts=state.observation.alerts)
        state.observation = obs
        idle_for = self.sim_time - state.last_decision_at
        if (
            self.trigger_driven
            and not new
            and not state.pending_triggers
            and not state.current_plan
            and not state.pending_action
            and held is None
            and idle_for > IDLE_REDECIDE_HOURS * DAY_LENGTH_SECONDS / 24.0
        ):
            new = [Trigger("event", "idle", f"{clock}: nothing has happened for a while.",
                           0.3, self.sim_time)]
        self._push_triggers(state, new)

    def _observe(self, state: AgentState, location: Optional[str] = None, *, places: bool = False) -> None:
        """更新人物对当前地点的信念；有变化时写一条 observation 记忆。

        places：顺带刷新「知道的地点」（换了地点，或刚做完动作，可能打开了新通路）。
        """
        from knowledge import current_location, describe_sighting, knowledge_mode, observe

        if knowledge_mode() == "omniscient":
            return
        name = state.agent.name
        try:
            loc = location or current_location(self.world, name)
            new, gone, kinds = observe(
                self.world, name, loc, t=self.sim_time, places=places or loc != state.seen_loc
            )
            state.seen_loc = loc
        except Exception as e:
            if self.verbose:
                print(f"[KNOW] [{name}] observe failed: {e}")
            return
        if (new or gone) and self.memory:
            self.memory.add(
                name,
                describe_sighting(loc, new, gone, kinds),
                type="observation",
                location=loc or "",
            )

    def _absorb_own_changes(self, state: AgentState, before: Optional[frozenset]) -> None:
        """自己动作造成的背包变化不算「收到」事件；别人给的仍会被下次扫描发现。"""
        if state.observation is None or before is None:
            return
        from dataclasses import replace

        from triggers import held_items

        after = held_items(self.world, state.agent.name)
        inv = (state.observation.inventory - (before - after)) | (after - before)
        state.observation = replace(state.observation, inventory=frozenset(inv))

    def _has_routine(self, state: AgentState) -> bool:
        from routine import load_person_routine

        return bool(load_person_routine(self.world, state.agent.name))

    def _routine_done_key(self, slot_key: str) -> str:
        from routine import day_index

        return f"{day_index(self.sim_time)}:{slot_key}"

    def _routine_slot_done(self, state: AgentState, slot) -> bool:
        return bool(slot.once) and self._routine_done_key(slot.key) in state.routine_done

    def _mark_routine_done(self, state: AgentState) -> None:
        goal = state.last_goal or {}
        key = goal.get("routine_key")
        if goal.get("source") != "routine" or not key:
            return
        done = self._routine_done_key(str(key))
        if done not in state.routine_done:
            state.routine_done = (state.routine_done + [done])[-32:]

    # ----- 持续状态（内容包 states/） -----

    def _current_slot_key(self, state: AgentState) -> Optional[str]:
        from routine import current_slot, load_person_routine

        slots = load_person_routine(self.world, state.agent.name)
        slot = current_slot(slots, self.sim_time) if slots else None
        return slot.key if slot else None

    def _held_state_def(self, state: AgentState):
        from content import get_content

        return get_content().state(state.held_state)

    def _state_gate(self, state: AgentState) -> bool:
        """角色处于持续状态时暂停决策；日程换段 / 足够紧急的触发器会结束状态。返回是否仍被挡住。"""
        from action import get_property
        from content import get_content

        name = get_property(self.world, state.agent.name, "Person", "state")
        sdef = get_content().state(name)
        if sdef is None:
            state.held_state = None
            return False
        if state.held_state != name:
            state.held_state = name
            state.state_slot = self._current_slot_key(state)
            state.state_since = self.sim_time
            state.pending_triggers = []
            if self.verbose:
                print(f"[STATE] [{state.agent.name}] enters '{name}'")
        reason = None
        if sdef.ends_on_routine_change and self._current_slot_key(state) != state.state_slot:
            reason = "the routine moved on"
        else:
            urgent = [t for t in state.pending_triggers if t.priority >= sdef.wake_priority]
            if urgent:
                reason = urgent[0].text
        if reason is None:
            return True
        self._exit_state(state, sdef, reason)
        return False

    def _exit_state(self, state: AgentState, sdef, reason: str) -> None:
        from graph_ops import apply_all
        from routine import format_clock
        from triggers import Trigger

        agent = state.agent
        ctx = {"actor": agent.name}
        apply_all(self.world, [{"op": "set_state", "entity": "@actor", "state": None}] + sdef.on_exit, ctx)
        state.held_state = None
        state.state_slot = None
        state.decision_cooldown_until = 0.0
        state.replan_backoff_until = 0.0
        self._log(state, "state", f"No longer {sdef.name}", reason=reason)
        if self.verbose:
            print(f"[STATE] [{agent.name}] leaves '{sdef.name}': {reason}")
        self._push_triggers(state, [Trigger(
            "routine", "state_end",
            f"{format_clock(self.sim_time)}: you are no longer {sdef.name} ({reason}).",
            0.6, self.sim_time,
        )])

    def _adopt_planned_goal(
        self,
        state: AgentState,
        assessed,
        *,
        reason: str,
    ) -> None:
        state.replan_reason = reason
        state.interrupt_count += 1
        state.is_interrupted = False
        state.current_plan = list(assessed.plan or [])
        state.plan_index = 0
        state.plan_engine = assessed.engine
        state.plan_error = None
        state.last_goal = dict(assessed.goal_data)
        state.last_decision_at = self.sim_time
        state.agent.set_current_goal(state.last_goal)
        state.status = "ready" if state.current_plan else "idle"
        state.decision_cooldown_until = 0.0
        self._log(
            state,
            "decision",
            _goal_text(state.last_goal),
            reason=reason,
            source=state.last_goal.get("source"),
            mode="interrupt",
            plan=self._remaining_plan(state),
            goal=dict(state.last_goal),
            outcome="chosen",
        )

    def _routine_candidates(self, state: AgentState) -> List[Dict[str, Any]]:
        from routine import (
            current_slot,
            load_person_routine,
            pick_social_target,
            routine_goal_candidate,
            social_interrupt_goal,
        )
        from world_systems import urgent_alert_goal

        out: List[Dict[str, Any]] = []
        fire_goal = urgent_alert_goal(self.world, state.agent.name, sim_time=self.sim_time)
        if fire_goal:
            out.append(fire_goal)
        # 商人的店缺货：去弄来（采集 / 制造 / 收购交给规划器）—— 这是他的活，日程里也算
        try:
            from economy import restock_candidates

            out.extend(restock_candidates(self.world, state.agent.name))
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"[WARN] restock {state.agent.name}: {e}")

        slots = load_person_routine(self.world, state.agent.name)
        if not slots:
            return out
        slot = current_slot(slots, self.sim_time)
        if slot is None:
            return out
        state.routine_slot_key = slot.key
        if self._routine_slot_done(state, slot):
            if state.agent.location != slot.location:
                out.append({
                    "goal": "Go to location",
                    "target_entity": slot.location,
                    "reason": f"Routine: stay at {slot.location} ({slot.activity})",
                    "priority": 0.72,
                    "confidence": 0.8,
                    "source": "routine",
                })
            return out
        cand = routine_goal_candidate(
            slot,
            character=state.agent.name,
            current_location=state.agent.location,
        )
        if cand.get("goal") == "Gain information" and not cand.get("target_entity"):
            target = pick_social_target(
                self.world,
                state.agent.name,
                state.agent.location,
                recent_talk_targets=state.recent_talk_targets,
            )
            if target:
                out.append(social_interrupt_goal(target, activity=slot.activity))
            else:
                out.append(
                    {
                        "goal": "Go to location",
                        "target_entity": slot.location,
                        "reason": f"Routine: go to {slot.location} to meet others",
                        "priority": 0.72,
                        "confidence": 0.8,
                        "source": "routine",
                    }
                )
            return out
        out.append(cand)
        if cand.get("goal") != "Go to location" and state.agent.location != slot.location:
            # 目标暂时做不成时，至少先去日程地点
            out.append({
                "goal": "Go to location",
                "target_entity": slot.location,
                "reason": f"Routine: head to {slot.location} ({slot.activity})",
                "priority": 0.7,
                "confidence": 0.8,
                "source": "routine",
            })
        return out

    def _ephemeral_action_candidates(self, state: AgentState) -> List[Dict[str, Any]]:
        """同地临时 Cypher action → 候选（带剪枝）。"""
        try:
            from item_interact import ephemeral_action_goal_candidates

            ctx = {
                "goal": (state.last_goal or {}).get("goal"),
                "current_goal": getattr(state.agent, "goal", None),
                "reason": (state.last_goal or {}).get("reason"),
                "routine_activity": getattr(state, "routine_slot_key", None),
            }
            return ephemeral_action_goal_candidates(
                self.world,
                state.agent.name,
                force_mock=bool(self.force_mock),
                memory=self.memory,
                same_location_only=True,
                prune=True,
                max_per_item=2,
                max_total=4,
                context=ctx,
            )
        except Exception as e:
            if self.verbose:
                print(f"[WARN] ephemeral candidates: {e}")
            return []

    # ----- 计划冲突 -----

    def _note_items_taken(self, actor: str, action_name: str, items: Any) -> None:
        """actor 刚成功动了这些物品；被拿走 / 用掉的，排队检查谁的计划因此落空。"""
        from conflicts import item_exists, item_holder

        for item in items:
            self._touched[item] = (actor, action_name, self.sim_time)
            holder = item_holder(self.world, item)
            if holder == actor or (holder is None and not item_exists(self.world, item)):
                with self._conflict_lock:
                    self._conflict_queue.append(
                        {"type": "taken", "culprit": actor, "item": item, "action": action_name,
                         "t": self.sim_time}
                    )

    def _queue_failure_conflict(self, state: AgentState, action_name: str, args: List[str], reason: str) -> None:
        """动作失败时：如果是因为别人拿走了涉及的物品，记成一次冲突。可在决策线程里调用。"""
        from conflicts import item_exists, item_holder, item_names

        me = state.agent.name
        for item in item_names(self.world, args):
            if f"{me} is not holding {item}" in (reason or ""):
                continue  # 计划以为自己拿着，其实从没拿到：是规划失误，不是被抢
            # 只算「我定下计划之后」才被别人拿走的：照着过时信息做的计划不怪别人
            who, _, at = self._touched.get(item, (None, None, -1e9))
            if not who or who == me or at < state.last_decision_at:
                continue
            if self.sim_time - at > CONFLICT_MEMORY:
                continue
            # 只有东西真被对方拿走 / 用掉才算；只是被碰过（砍过的树还在原地）不算
            holder = item_holder(self.world, item)
            taken = holder == who or (holder is None and not item_exists(self.world, item))
            culprit = who if taken else None
            if culprit:
                with self._conflict_lock:
                    self._conflict_queue.append({
                        "type": "failed", "loser": me, "culprit": culprit, "item": item,
                        "how": f"{action_name} failed: {reason}",
                    })

    def _flush_conflicts(self) -> None:
        """主线程：让排队的冲突生效（打断落空的计划、触发器、记矛盾）。"""
        with self._conflict_lock:
            queue, self._conflict_queue = self._conflict_queue, []
        for c in queue:
            if c["type"] == "taken":
                for other in self.agents:
                    if other.agent.name == c["culprit"] or not self._plan_mentions(other, c["item"]):
                        continue
                    if other.agent.name in self._inflight:
                        continue  # 正在重新决策；新计划的预检会发现东西没了
                    if other.last_decision_at > c["t"]:
                        continue  # 计划是在东西被拿走之后才定的
                    if other.pending_action is not None:
                        continue  # 进行中的动作完成时会失败，到时再记
                    other.status = "failed"
                    other.replan_reason = f"{c['culprit']} took {c['item']}"
                    other.plan_error = other.replan_reason
                    self._record_conflict(
                        other, c["culprit"], c["item"], f"{c['culprit']} took it before you got there"
                    )
            else:
                state = self.get_agent_state(c["loser"])
                if state is not None:
                    self._record_conflict(state, c["culprit"], c["item"], c["how"])

    def _plan_mentions(self, state: AgentState, item: str) -> bool:
        steps = (state.current_plan or [])[state.plan_index:]
        return any(item in (args or []) for _, args in steps)

    def _record_conflict(self, loser: AgentState, culprit: str, item: str, how: str) -> None:
        from conflicts import CONFLICT_PRIORITY, RIVAL_PRIORITY, add_tension
        from routine import format_clock
        from triggers import Trigger

        name = loser.agent.name
        recent = [
            c for c in self.conflicts[-12:]
            if (c["loser"], c["culprit"], c["item"]) == (name, culprit, item)
            and self.sim_time - c["t"] <= CONFLICT_MEMORY
        ]
        if recent:
            return
        entry = {
            "t": round(self.sim_time, 2),
            "clock": format_clock(self.sim_time),
            "loser": name,
            "culprit": culprit,
            "item": item,
            "how": how,
        }
        self.conflicts.append(entry)
        del self.conflicts[:-HISTORY_LIMIT]
        level = add_tension(self.world, name, culprit, reason=f"took {item}", at=self.sim_time)
        try:
            from relations import TAKEN, adjust

            adjust(self.world, name, culprit, TAKEN, reason=f"took {item} I needed", at=self.sim_time)
        except Exception:  # noqa: BLE001
            pass
        entry["tension"] = level
        self._push_triggers(loser, [Trigger(
            "event", "conflict",
            f"{culprit} took {item}, which you needed ({how}). Your grudge against {culprit} is now {level}.",
            CONFLICT_PRIORITY, self.sim_time, {"with": culprit, "item": item},
        )])
        self._log(loser, "conflict", f"Lost {item} to {culprit}", reason=how, tension=level)
        rival = self.get_agent_state(culprit)
        if rival is not None:
            self._push_triggers(rival, [Trigger(
                "event", "rival", f"{name} wanted {item} too and is upset with you.",
                RIVAL_PRIORITY, self.sim_time, {"with": name, "item": item},
            )])
            self._log(rival, "conflict", f"Took {item} that {name} wanted")
        if self.verbose:
            print(f"[CONFLICT] {culprit} took {item} from {name} (tension {level})")

    # ----- 剧情 -----

    def _economy_step(self) -> None:
        """每个游戏小时：价格随供需变、商店缺货记成需求、做好的东西上架、过期的委托撤下。"""
        from economy import ECONOMY_EVERY, economy_step
        from job_board import tidy

        due = getattr(self, "_next_economy_at", None)
        if due is not None and self.sim_time < due:
            return
        self._next_economy_at = self.sim_time + ECONOMY_EVERY
        try:
            tidy(self.world, now=self.sim_time)
            report = economy_step(self.world)
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"[WARN] economy: {e}")
            return
        for trader, items in (report.get("listed") or {}).items():
            state = self.get_agent_state(trader)
            if state is not None:
                self._log(state, "event", f"Put {', '.join(items)} up for sale")

    def _combat_step(self, state: AgentState) -> bool:
        """打、追、逃（见 combat.react）。接手了这个人这一拍就返回 True。

        正在做别的事：只有在动手之后又挨了打才打断；正在打 / 逃：让它做完这一下。
        """
        from combat import react

        name = state.agent.name
        checks = self.__dict__.setdefault("_combat_checks", {})
        until = self.__dict__.setdefault("_combat_until", {})
        pending = state.pending_action
        in_combat = (state.last_goal or {}).get("source") == "combat"
        if pending is not None:
            if in_combat:
                return False
            rows = self.world.run_query(
                "MATCH (p:Person {name: $n}) RETURN p.attacked_at AS t", n=name
            ) or []
            hit_at = rows[0].get("t") if rows else None
            if hit_at is None or float(hit_at) < pending.started_at:
                return False
        else:
            if in_combat and state.current_plan and state.plan_index < len(state.current_plan):
                return False  # 战斗计划还有下一步（先走过去再打）：照常推进
            if self.sim_time < checks.get(name, -1.0):
                return False
        checks[name] = self.sim_time + 1.0  # 环顾四周不用每拍都做
        try:
            choice = react(self.world, name, at=self.sim_time)
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"[WARN] combat react {name}: {e}")
            return False
        if not choice:
            return False
        if pending is not None:
            state.pending_action = None
            state.sim_busy_until = 0.0
        attack = choice["kind"] == "attack"
        target = choice.get("target") if attack else choice.get("to")
        state.current_plan, state.plan_index = list(choice["plan"]), 0
        state.last_goal = {
            "goal": "Defeat enemy" if attack else "Flee",
            "target_entity": target,
            "reason": choice.get("why") or "",
            "source": "combat",
        }
        until[name] = self.sim_time + 3.0
        text = (f"Fights {target} ({choice.get('why')})" if attack
                else f"Flees from {choice.get('from')} to {target}")
        self._log(state, "event", text, reason=choice.get("why") or "")
        self._start_next_step(state)
        return True

    def _relations_step(self) -> None:
        """每两个游戏小时：好感回落、矛盾消退，够亲近的人自然结盟。"""
        from relations import DECAY_EVERY, decay

        due = getattr(self, "_next_relations_at", None)
        if due is None:
            self._next_relations_at = self.sim_time + DECAY_EVERY
            return
        if self.sim_time < due:
            return
        self._next_relations_at = self.sim_time + DECAY_EVERY
        try:
            from combat import regen

            regen(self.world, at=self.sim_time)  # 没在打仗的人慢慢回血
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"[WARN] regen: {e}")
        try:
            decay(self.world, at=self.sim_time)
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"[WARN] relations decay: {e}")

    def _story_step(self) -> None:
        """到点就让 Storyteller 出一拍剧情；LLM 在线程池里写，下一拍生效，不卡模拟。"""
        st = self.storyteller
        if st is None:
            return
        fut = self._story_future
        if fut is not None:
            if not fut.done():
                return
            self._story_future = None
            try:
                plan = fut.result()
            except Exception as e:  # noqa: BLE001
                if self.verbose:
                    print(f"[WARN] storyteller: {e}")
                plan = None
            if plan:
                st.apply(self, plan)
            return
        if not st.due(self.sim_time):
            return
        st.mark_started(self.sim_time)
        digest = st.digest(self)
        if self.force_mock or not st.use_llm or not _llm_configured():
            st.apply(self, st.compose_template(digest))
            return
        self._story_future = self._pool().submit(st.compose_llm, digest)

    def record_conversation(self, a: str, b: str, location: str, lines: List[Any]) -> None:
        said = [
            {"speaker": str(ln.get("speaker") or ""), "text": str(ln.get("text") or "")}
            for ln in lines or []
            if isinstance(ln, dict) and ln.get("text")
        ]
        if not said:
            return
        self._conversation_seq = getattr(self, "_conversation_seq", 0) + 1
        self.conversations.append({
            "id": f"{self._conversation_seq}:{a}:{b}",
            "t": round(self.sim_time, 2),
            "a": a,
            "b": b,
            "location": location,
            "lines": said,
        })
        del self.conversations[:-CONVERSATION_LIMIT]

    def _feelings_after(self, agent: AgentProfile, action_name: str, args: List[str]) -> None:
        """送东西、做成买卖之类的事会改变别人对他的好感（见 relations.py）。"""
        try:
            from registry import get_registry
            from relations import after_action

            action = get_registry().get(action_name)
            if action is None:
                return
            params = {p.name: str(v) for p, v in zip(action.parameters, args)}
            after_action(self.world, agent.name, action_name, params, at=self.sim_time)
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"[WARN] feelings after {action_name}: {e}")

    def _list_new_goods(self, state: AgentState) -> None:
        """商人刚做好 / 收来一件他卖的东西：马上上架。"""
        try:
            from economy import list_goods

            listed = list_goods(self.world, state.agent.name)
        except Exception:  # noqa: BLE001
            return
        if listed:
            self._log(state, "event", f"Put {', '.join(listed)} up for sale")

    def _follow_step(self, state: AgentState, leader: str) -> None:
        """组队成员：队长在别的区域就赶过去；同一区域就走到他身边站着。"""
        from spatial import find_path, person_cell, walkable_cells, WALK_CELLS_PER_SEC, path_length

        if state.pending_action is not None:
            return
        name = state.agent.name
        rows = self.world.run_query(
            """
            MATCH (me:Person {name: $me})-[:LOCATED_AT]->(a:Location)
            MATCH (l:Person {name: $l})-[:LOCATED_AT]->(b:Location)
            RETURN a.name AS mine, b.name AS theirs
            """,
            me=name, l=leader,
        ) or []
        if not rows:
            return
        mine, theirs = rows[0]["mine"], rows[0]["theirs"]
        state.status = "following"
        if mine != theirs:
            # 另一个区域：规划一条去那里的路（已经在路上就接着走）
            goal = {"goal": "Go to location", "target_entity": theirs, "reason": f"keeping up with {leader}",
                    "source": "follow"}
            if not (state.current_plan and state.plan_index < len(state.current_plan)
                    and (state.last_goal or {}).get("target_entity") == theirs):
                from goal_feasibility import assess_goal

                assessed = assess_goal(self.world, name, goal, engines=self.plan_engines)
                if not (assessed.ok and assessed.plan):
                    return
                state.last_goal = dict(goal)
                state.current_plan, state.plan_index = list(assessed.plan), 0
            self._start_next_step(state)
            return
        state.current_plan, state.plan_index = None, 0
        here, there = person_cell(self.world, name), person_cell(self.world, leader)
        if here is None or there is None or max(abs(here[0] - there[0]), abs(here[1] - there[1])) <= 2:
            return
        walkable = walkable_cells(self.world) | {here}
        goals = {(there[0] + dx, there[1] + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy}
        path = find_path(walkable, here, goals)
        if not path or len(path) < 2:
            return
        secs = max(0.3, path_length(path) / WALK_CELLS_PER_SEC)
        state.pending_action = PendingAction(
            name=act("idle"), args=[name], started_at=self.sim_time, duration=secs,
            completes_at=self.sim_time + secs, path=list(path), hold=True, walk_seconds=secs,
        )
        state.sim_busy_until = state.pending_action.completes_at

    def heard(self, name: str, other: str, lines: List[Any]) -> bool:
        """name 刚和 other 说完话：推一个「要不要因此改主意」的触发器（带上说了什么）。"""
        from triggers import Trigger

        state = self.get_agent_state(name)
        if state is None or self._is_player(state) or self._routine_only(state):
            return False  # 玩家自己拿主意；背景 NPC 只照日程，聊什么都不改主意
        other_state = self.get_agent_state(other)
        from_player = other_state is not None and self._is_player(other_state)
        said = [
            f'{ln.get("speaker")}: "{ln.get("text")}"'
            for ln in (lines or [])[-6:]
            if isinstance(ln, dict) and ln.get("text")
        ]
        if said:
            text = f"You just talked with {other}: " + " / ".join(said)
        else:
            text = f"You just talked with {other}."
        if from_player:
            text += f" If {other} asked you for something, decide whether to do it."
        # 同一个人的新一轮对话替换旧的（别让聊天把触发器队列塞满）
        state.pending_triggers = [
            t for t in state.pending_triggers
            if not (t.kind == "conversation" and (t.data or {}).get("with") == other)
        ]
        prio = PLAYER_TALK_PRIORITY if from_player else TALK_PRIORITY
        self._push_triggers(state, [Trigger("event", "conversation", text[:600], prio, self.sim_time, {"with": other})])
        return True

    def recent_talk(self, name: str) -> str:
        """决策用：最近和谁说过什么（逐句原话）。"""
        out = []
        for c in reversed(self.conversations):
            if self.sim_time - float(c.get("t") or 0.0) > RECENT_TALK_WINDOW:
                break
            if name not in (c.get("a"), c.get("b")):
                continue
            other = c["b"] if c.get("a") == name else c.get("a")
            said = "\n".join(f'  {ln.get("speaker")}: "{ln.get("text")}"' for ln in c.get("lines") or [])
            out.append(f"With {other} at {c.get('location') or 'here'}:\n{said}")
            if len(out) >= 2:
                break
        return "\n".join(reversed(out))

    def engage(self, name: str, by: str, seconds: float) -> bool:
        """让 name 站着陪 by 说话 seconds 模拟秒（不开始新步骤 / 不做新决策）。"""
        state = self.get_agent_state(name)
        if state is None or self._is_player(state):
            return False
        state.engaged_by = by
        state.engaged_until = max(state.engaged_until, self.sim_time + max(0.0, seconds))
        return True

    def release(self, name: str, by: str) -> None:
        """by 不聊了：name 可以走了。"""
        state = self.get_agent_state(name)
        if state is not None and state.engaged_by == by:
            state.engaged_by, state.engaged_until = None, 0.0

    def _engaged(self, state: AgentState, *, besides: Optional[str] = None) -> bool:
        """有人叫住了 state（besides 本人叫的不算）且还没超时。"""
        if not state.engaged_by or state.engaged_by == besides:
            return False
        if self.sim_time >= state.engaged_until:
            state.engaged_by, state.engaged_until = None, 0.0
            return False
        return True

    def _talk_partner(self, state: AgentState, action_name: str, args: List[str]) -> Optional[AgentState]:
        """搭话对象（由本循环驱动的 NPC）；玩家 / 非对话动作返回 None。"""
        if action_name != act("converse") or len(args) < 2:
            return None
        other = args[1] if args[0] == state.agent.name else args[0]
        partner = self.get_agent_state(other) if other and other != state.agent.name else None
        if partner is None or self._is_player(partner):
            return None
        return partner

    def _wait_for_partner(self, state: AgentState, partner: AgentState) -> Dict[str, Any]:
        """对方手头有事：叫住他（这一步做完就别走），自己原地等到那时再出发。"""
        busy_until = partner.pending_action.completes_at if partner.pending_action else partner.engaged_until
        if not self._engaged(partner):
            partner.engaged_by = state.agent.name
            partner.engaged_until = busy_until + ENGAGE_WAIT
        wait = min(max(0.2, busy_until - self.sim_time + 0.05), ENGAGE_WAIT)
        state.pending_action = PendingAction(
            name=act("idle"), args=[state.agent.name], started_at=self.sim_time,
            duration=wait, completes_at=self.sim_time + wait, hold=True,
        )
        state.sim_busy_until = state.pending_action.completes_at
        state.status = "waiting"
        state.waiting_for = partner.agent.name
        return {"action": act("idle"), "args": [state.agent.name], "started": True, "waiting_for": partner.agent.name,
                "duration": wait, "completes_at": state.pending_action.completes_at, "step": state.plan_index + 1}

    def _hold_while_talking(self, names: List[str], lines: List[Any]) -> None:
        """两人站着把这段话说完（台词的阅读时间）再各干各的；手头有事的人不打断。"""
        wall = sum(reading_seconds(str(ln.get("text") or "")) for ln in lines if isinstance(ln, dict))
        hold = wall * max(0.0, self.sim_per_wall)
        if hold <= 0:
            return
        for name in names:
            state = self.get_agent_state(name)
            if state is None or state.pending_action is not None:
                continue
            state.pending_action = PendingAction(
                name=act("idle"), args=[name], started_at=self.sim_time,
                duration=hold, completes_at=self.sim_time + hold, hold=True,
            )
            state.sim_busy_until = state.pending_action.completes_at
            state.status = "talking"

    def _run_hooks(self) -> None:
        for hook in list(self.hooks):
            try:
                hook.on_tick(self)
            except Exception as e:  # noqa: BLE001
                if self.verbose:
                    print(f"[WARN] tick hook {type(hook).__name__}: {e}")

    def _pool(self) -> ThreadPoolExecutor:
        if self._plan_pool is None:
            self._plan_pool = ThreadPoolExecutor(
                max_workers=self.plan_workers, thread_name_prefix="npc-plan"
            )
        return self._plan_pool

    def _log(self, state: AgentState, kind: str, text: str, **detail: Any) -> None:
        from routine import format_clock

        entry: Dict[str, Any] = {
            "t": round(self.sim_time, 2),
            "clock": format_clock(self.sim_time),
            "kind": kind,
            "text": text,
        }
        # 每一步都记下是为了哪个目标、当时为什么这么决定（历史里每条都看得到理由）
        own_step = detail.pop("own_step", True)
        goal = state.last_goal or {}
        if kind in ("start", "done", "failed") and own_step and goal:
            entry["for_goal"] = {k: goal.get(k) for k in ("goal", "target_entity", "interact_action") if goal.get(k)}
            why = goal.get("reason") or goal.get("reasoning")
            if why:
                entry["why"] = str(why)
            if goal.get("source"):
                entry["goal_source"] = goal.get("source")
        entry.update({k: v for k, v in detail.items() if v not in (None, "", [], {})})
        state.history.append(entry)
        if len(state.history) > HISTORY_LIMIT:
            del state.history[:-HISTORY_LIMIT]

    def _remaining_plan(self, state: AgentState) -> List[str]:
        if not state.current_plan:
            return []
        return [_step_text(s) for s in state.current_plan[state.plan_index:]]

    def _decide_and_plan(
        self,
        state: AgentState,
        *,
        instant: bool = False,
    ) -> Dict[str, Any]:
        out = self._decide_and_plan_core(state, instant=instant)
        triggers = [t.get("text") for t in state.last_triggers if isinstance(t, dict) and t.get("text")]
        goal = state.last_goal or {}
        if out.get("kept"):
            text, reason, outcome = f"Keeps current plan: {_goal_text(goal)}", None, "kept"
        elif out.get("fallback") == "wait":
            text, reason, outcome = "Nothing actionable — waits", goal.get("reason"), "wait"
        elif out.get("goal"):
            g = out["goal"]
            goal = g
            text = _goal_text(g)
            outcome = "chosen"
            if not state.current_plan:
                text += " (already satisfied)"
                outcome = "satisfied"
            reason = g.get("reason") or g.get("reasoning")
        else:
            text, reason, outcome = "Could not decide", state.plan_error, "none"
        self._log(
            state,
            "decision",
            text,
            reason=reason,
            source=(out.get("goal") or {}).get("source"),
            mode=state.decision_mode,
            triggers=triggers,
            plan=self._remaining_plan(state),
            goal=dict(goal) if goal else None,
            outcome=outcome,
        )
        self._remember_doing(state, goal if outcome == "chosen" else None, reason)
        return out

    def _remember_doing(self, state: AgentState, goal: Optional[Dict[str, Any]], reason: Any) -> None:
        """把「正在做什么、为什么」写在人身上：聊天时他说得出自己在忙什么、缺什么。"""
        try:
            from narration import goal_phrase

            text = ""
            if goal:
                text = goal_phrase(goal, self.world)
                if reason:
                    text += f" (because {str(reason)[:160]})"
            self.world.run_query("MATCH (p:Person {name: $n}) SET p.doing = $d", n=state.agent.name, d=text)
        except Exception:  # noqa: BLE001
            pass

    def _decide_and_plan_core(
        self,
        state: AgentState,
        *,
        instant: bool = False,
    ) -> Dict[str, Any]:
        agent = state.agent
        decider = getattr(agent, "_decider", None)
        out: Dict[str, Any] = {"decided": False}

        state.status = "deciding"
        if self.verbose:
            print(f"[THINK] [{agent.name}] deciding | location: {agent.location} | HP: {agent.hp}")

        # 供 PromptBuilder 显示正确的一日时段
        agent._sim_time = self.sim_time  # noqa: SLF001

        # 消费触发器：告诉 LLM「为什么现在要做决定」
        from triggers import describe_triggers

        triggers = list(state.pending_triggers)
        state.pending_triggers = []
        state.last_triggers = [t.to_dict() for t in triggers]
        state.last_decision_at = self.sim_time
        agent._trigger_context = describe_triggers(triggers)  # noqa: SLF001
        agent._recent_talk = self.recent_talk(agent.name)  # noqa: SLF001
        in_plan = bool(state.current_plan and state.plan_index < len(state.current_plan))
        if self.verbose and triggers:
            print(f"   triggered by: {', '.join(t.kind for t in triggers)}")

        # 清空旧候选，重新生成
        agent.goal_manager.clear_all()
        routine_only = self._routine_only(state)
        routine_cands = self._routine_candidates(state)
        if routine_only:
            # 背景 NPC：只看日程（含日程里的「和周围的人聊聊」），不管物品动作、不随性格找事做
            routine_cands = [c for c in routine_cands if (c or {}).get("source") in ("routine", "social_interrupt", "shop")]
        ephemeral_cands = [] if routine_only else self._ephemeral_action_candidates(state)
        has_routine = self._has_routine(state)
        if routine_only:
            candidates = list(routine_cands)
            state.decision_mode = "routine"
            state.decision_process = {
                "planner": "routine",
                "note": "Background NPC: follows the daily routine only (no LLM decisions)",
                "routine": routine_cands[0] if routine_cands else None,
            }
        elif self.force_mock:
            from profile_planner import profile_aware_pick_goal

            # 有日程的人：日程满足后就待在原地，不随机找事做
            fallback = [] if has_routine else [profile_aware_pick_goal(self.world, agent)]
            candidates = list(routine_cands) + list(ephemeral_cands) + fallback
            state.decision_mode = "mock"
            state.decision_process = {
                "planner": "profile_aware",
                "settings": agent.get_settings_summary(),
                "note": "Rule-based decision: routine + ephemeral interact actions + personality",
                "routine": routine_cands[0] if routine_cands else None,
                "ephemeral_n": len(ephemeral_cands),
            }
        else:
            candidates = agent.generate_candidate_goals(
                decider=decider,
                parent_goal=None,
                current_goal=(
                    (state.last_goal.get("goal") if state.last_goal else None)
                    or agent.goal
                    or None
                ),
                last_action_result=state.last_action_result.message
                if state.last_action_result
                else None,
                last_action_success=state.last_action_result.success
                if state.last_action_result
                else True,
                num_candidates=1,
                verbose=self.verbose,
            )
            llm_cands = [dict(c or {}, source=(c or {}).get("source") or "llm") for c in candidates or []]
            candidates = (
                list(routine_cands)
                + list(ephemeral_cands)
                + llm_cands
            )
            state.decision_mode = "llm"
            state.decision_process = {
                "force_mock": False,
                "settings": agent.get_settings_summary(),
                "note": "Triggered LLM decision; routine / item signals are fallbacks",
                "triggers": state.last_triggers,
                "routine": routine_cands[0] if routine_cands else None,
                "ephemeral_n": len(ephemeral_cands),
            }
        state.candidate_goals = list(candidates)
        state.decision_steps = []

        exclude = list(state.tried_goal_keys)
        llm_goal = None
        if candidates:
            # 展示「非日程」的首个 LLM/规则候选，便于 UI 对比
            for c in candidates:
                if (c or {}).get("source") in (
                    "routine",
                    "social_interrupt",
                    "item_script",
                    "fire_emergency",
                    "ephemeral_action",
                ):
                    continue
                c0 = dict(c or {})
                if not c0.get("goal") and c0.get("final_goal"):
                    c0["goal"] = c0.get("final_goal")
                llm_goal = {
                    "goal": c0.get("goal"),
                    "target_entity": c0.get("target_entity"),
                    "interact_action": c0.get("interact_action"),
                }
                break

        picked = None
        if not self._rule_based(state):
            # LLM 自己的选择优先；与当前目标相同则继续原计划
            for c in candidates:
                if (c or {}).get("source") != "llm":
                    continue
                c0 = dict(c)
                if not c0.get("goal") and c0.get("final_goal"):
                    c0["goal"] = c0.get("final_goal")
                if in_plan and _goal_key(c0) == _goal_key(state.last_goal):
                    state.status = "ready"
                    out.update({"decided": True, "kept": True})
                    if self.verbose:
                        print(f"[GOAL] [{agent.name}] keeps current plan")
                    return out
                assessed = assess_goal(self.world, agent.name, c0, engines=self.plan_engines)
                if _goal_key(assessed.goal_data) in exclude:
                    continue
                if assessed.ok and not assessed.already_satisfied and assessed.plan:
                    picked = assessed
                    break
            if picked is None and in_plan:
                # 被打断但 LLM 没给出更好的可行目标：继续原计划
                state.status = "ready"
                out.update({"decided": True, "kept": True})
                return out

        # 物品脚本告警 / 日程优先（规则模式，或 LLM 目标不可行时的兜底）
        for rc in ([] if picked is not None else routine_cands):
            if rc.get("source") not in ("item_script", "fire_emergency"):
                continue
            assessed = assess_goal(
                self.world,
                agent.name,
                rc,
                engines=self.plan_engines,
            )
            key = _goal_key(assessed.goal_data)
            if key in exclude:
                continue
            if assessed.ok and not assessed.already_satisfied and assessed.plan:
                picked = assessed
                break

        if picked is None:
            for rc in routine_cands:
                need_travel = (
                    rc.get("goal") == "Go to location"
                    and rc.get("target_entity")
                    and rc.get("target_entity") != agent.location
                )
                if not need_travel and rc.get("source") == "routine":
                    continue
                if rc.get("source") in ("item_script", "fire_emergency"):
                    continue
                assessed = assess_goal(
                    self.world,
                    agent.name,
                    rc,
                    engines=self.plan_engines,
                )
                key = _goal_key(assessed.goal_data)
                if key in exclude:
                    continue
                if assessed.ok and not assessed.already_satisfied and assessed.plan:
                    picked = assessed
                    break

        if picked is None:
            picked = pick_best_feasible(
                self.world,
                agent.name,
                candidates,
                engines=self.plan_engines,
                exclude_goals=exclude,
            )

        if picked is None and not has_routine and not routine_only:
            # profile 兜底
            try:
                from profile_planner import profile_aware_pick_goal

                fallback = profile_aware_pick_goal(self.world, agent)
                state.decision_mode = "profile"
                state.decision_process = {
                    "planner": "profile_aware",
                    "settings": agent.get_settings_summary(),
                    "note": "Rule fallback after all candidates infeasible or already satisfied",
                    "llm_goal_overridden": llm_goal,
                    "fallback_goal": {
                        "goal": fallback.get("goal"),
                        "target_entity": fallback.get("target_entity"),
                    },
                }
                state.candidate_goals = [fallback]
                if self.verbose and llm_goal:
                    print(
                        f"[GOAL] [{agent.name}] LLM goal overridden: "
                        f"{llm_goal.get('goal')}->{llm_goal.get('target_entity')} "
                        f"=> {fallback.get('goal')}->{fallback.get('target_entity')}"
                    )
                picked = assess_goal(
                    self.world,
                    agent.name,
                    fallback,
                    engines=self.plan_engines,
                )
                if not picked.ok:
                    picked = None
            except Exception:
                picked = None

        # 想做的事做成了没有：卡住就挂委托 / 请大模型（只看最想做的那一件）
        wanted = None
        if routine_only:
            wanted = routine_cands[0] if routine_cands else None
        elif llm_goal and llm_goal.get("goal"):
            wanted = llm_goal
        try:
            self._note_progress(state, wanted, picked)
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"[WARN] progress {agent.name}: {e}")

        if picked is None:
            # wait + backoff
            wp = wait_plan(agent.name)
            state.last_goal = {
                "goal": "Go to location",
                "target_entity": agent.location,
                "reason": "No actionable goal; waiting",
                "priority": 0.1,
                "confidence": 0.1,
            }
            state.current_plan = list(wp.plan)
            state.plan_index = 0
            state.plan_engine = wp.engine
            state.plan_error = None
            state.is_interrupted = False
            state.status = "planning"
            state.replan_backoff_until = self.sim_time + self.replan_backoff
            state.decision_cooldown_until = self.sim_time + self.decision_cooldown
            out["decided"] = True
            out["fallback"] = "wait"
            return out

        goal_data = picked.goal_data
        target, _ = sanitize_goal_target(
            self.world,
            agent.name,
            goal_data.get("goal", ""),
            goal_data.get("target_entity"),
        )
        goal_data = dict(goal_data)
        goal_data["target_entity"] = target
        state.last_goal = goal_data
        agent.set_current_goal(goal_data)
        out["goal"] = goal_data
        out["decided"] = True

        if self.verbose:
            ia = goal_data.get("interact_action") or ""
            extra = f" [{ia}]" if ia else ""
            print(
                f"[GOAL] [{agent.name}] selected: {goal_data.get('goal')} -> "
                f"{goal_data.get('target_entity')}{extra}"
            )

        state.status = "planning"
        if picked.already_satisfied or not picked.plan:
            agent.complete_current_goal()
            state.current_plan = None
            state.plan_index = 0
            state.is_interrupted = False
            state.plan_engine = picked.engine or "satisfied"
            state.plan_error = None
            state.status = "idle"
            state.decision_cooldown_until = self.sim_time + self.decision_cooldown
            self._remember_action(
                agent,
                "noop",
                [],
                ActionResult(
                    success=True,
                    message=f"Goal already satisfied: {goal_data.get('goal')}",
                    agent_name=agent.name,
                    action_type="noop",
                ),
            )
            return out

        state.current_plan = list(picked.plan or [])
        state.plan_index = 0
        state.plan_engine = picked.engine
        state.plan_error = None
        state.is_interrupted = False
        state.status = "ready" if state.current_plan else "idle"
        # 有待执行计划时不进入决策冷却，否则同 tick/下一 tick 容易看起来像「只决策不动」
        if not state.current_plan:
            state.decision_cooldown_until = self.sim_time + self.decision_cooldown
        state.replan_reason = None
        if self.verbose:
            print(f"   plan {len(state.current_plan)} steps (engine={state.plan_engine})")
            for i, step in enumerate(state.current_plan, 1):
                print(f"     {i}. {step[0]}({', '.join(step[1])})")
        return out

    def _failure_replan(
        self,
        state: AgentState,
        *,
        reason: str,
        instant: bool = False,
    ) -> None:
        agent = state.agent
        self._log(
            state, "replan", f"Plan broke: {_goal_text(state.last_goal)}", reason=reason,
            goal=dict(state.last_goal) if state.last_goal else None,
        )
        state.replan_reason = reason
        state.interrupt_count += 1
        state.is_interrupted = True
        self._cancel_pending(state, apply=False)
        state.current_plan = None
        state.plan_index = 0
        state.plan_error = reason
        state.status = "failed"

        if self._is_dead(state):
            return

        key = _goal_key(state.last_goal)
        if self.verbose:
            print(f"[REPLAN] [{agent.name}] failure replan: {reason}")

        # 1) 同目标再规划一次
        if state.last_goal:
            retry = assess_goal(
                self.world,
                agent.name,
                state.last_goal,
                engines=self.plan_engines,
            )
            if retry.ok and not retry.already_satisfied and retry.plan:
                state.current_plan = list(retry.plan)
                state.plan_index = 0
                state.plan_engine = retry.engine
                state.plan_error = None
                state.is_interrupted = False
                state.status = "idle"
                state.last_goal = retry.goal_data
                if self.verbose:
                    print(f"   same-goal replan succeeded: {len(state.current_plan)} steps")
                return
            if key and key not in state.tried_goal_keys:
                state.tried_goal_keys.append(key)

        # 2) 换次优候选 / 重新决策
        state.decision_cooldown_until = 0.0  # 允许立即决策
        if self.trigger_driven:
            from triggers import Trigger

            goal = state.last_goal or {}
            self._push_triggers(state, [Trigger(
                "event", "plan_failed",
                f"Your plan ({goal.get('goal')} -> {goal.get('target_entity')}) failed: {reason}",
                0.8, self.sim_time,
            )])
        decided = self._decide_and_plan(state, instant=instant)
        if state.current_plan:
            state.is_interrupted = False
            if self.verbose:
                print(f"   alternate-goal replan succeeded (fallback={decided.get('fallback')})")
            return

        # 3) wait + backoff
        wp = wait_plan(agent.name)
        state.current_plan = list(wp.plan)
        state.plan_index = 0
        state.plan_engine = wp.engine
        state.is_interrupted = False
        state.status = "idle"
        state.replan_backoff_until = self.sim_time + self.replan_backoff
        if self.verbose:
            print(f"   replan failed; waiting backoff={self.replan_backoff}s")

    def _execute_plan_step(
        self,
        agent: AgentProfile,
        action_name: str,
        args: List[str],
    ) -> ActionResult:
        if action_name == "noop":
            return ActionResult(
                success=True,
                message="noop",
                agent_name=agent.name,
                action_type="noop",
            )
        from registry import get_registry

        registry = get_registry()
        action = registry.get(action_name)
        if action is None:
            return ActionResult(
                success=False,
                message=f"Unknown action: {action_name}",
                agent_name=agent.name,
                action_type=action_name,
            )
        kwargs = {p.name: v for p, v in zip(action.parameters, args)}
        if action_name == "interact":
            try:
                import item_interact as _ii

                _ii.FORCE_MOCK = self._rule_based_name(agent)
            except Exception:
                pass
        ok, reason = registry.execute(self.world, action_name, **kwargs)
        target = args[-1] if args else None
        metadata: Dict[str, Any] = {}
        if ok and action_name == act("converse") and len(args) >= 3:
            try:
                from dialogue_service import handle_talk_dialogue

                listener = self.get_agent_state(args[1])
                to_player = listener is not None and self._is_player(listener)
                dialogue = handle_talk_dialogue(
                    self.world,
                    args[0],
                    args[1],
                    args[2],
                    memory=self.memory,
                    # 背景 NPC 去找人说话（日程里的寒暄）不调用大模型：不写台词
                    force_mock=self._rule_based_name(agent),
                    # 跟玩家说话：只说自己这句，不替玩家回答
                    listener_speaks=not to_player,
                )
                metadata["dialogue"] = dialogue
                if to_player and dialogue.get("awaiting_reply"):
                    self.engage(args[0], args[1], PLAYER_REPLY_WAIT)
            except Exception:
                pass
        if ok and action_name == "interact":
            try:
                from item_interact import take_last_interact_result

                inter = take_last_interact_result()
                if inter:
                    metadata["interaction"] = inter
                    if inter.get("narrative"):
                        reason = inter["narrative"]
            except Exception:
                pass
        return ActionResult(
            success=ok,
            message=reason,
            agent_name=agent.name,
            action_type=action_name,
            target=target,
            metadata=metadata,
        )

    def _remember_action(
        self,
        agent: AgentProfile,
        action_name: str,
        args: List[str],
        result: ActionResult,
    ) -> None:
        if self.memory is None:
            return
        text = f"{action_name}({', '.join(args)}) -> {result.message}"
        self.memory.add(
            agent.name,
            text,
            type="action",
            location=agent.location or "",
            success=result.success,
            result=result.message,
        )
