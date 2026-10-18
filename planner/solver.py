"""
PDDL 求解器封装

主路径：unified-planning（已证实在本环境可用）
备路径：内置 BFS 寻路（仅 move-only domain 时启用，避免外部依赖断网时整体瘫痪）

solve_pddl() 返回结构化结果：
    {
        "success": bool,
        "engine":  str,                # 实际使用的引擎名
        "plan":    List[Tuple[str, List[str]]],  # [(action_name, [arg1, arg2, ...]), ...]
        "raw":     str,                # 引擎的原始 plan 文本（调试用）
        "error":   Optional[str],
    }
"""

from __future__ import annotations

import os
import copy
import hashlib
import re
import tempfile
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any, Dict, List, Optional, Tuple


PlanStep = Tuple[str, List[str]]

# Fast Downward / UP 偶发挂起时，超时后改试下一引擎，并保证能写出 inspect 日志
_UP_TIMEOUT_SEC = float(os.environ.get("AITOWN3_UP_TIMEOUT", "20"))
_ENHSP_PARAMS = os.environ.get("AITOWN3_ENHSP_PARAMS", "-h hadd -s WAStar")

# 默认先 BFS：Fast Downward 子进程结束会 sys.exit，debugpy 跟进子进程时会停在
# up_fast_downward/.../driver/main.py；BFS 可覆盖纯寻路且不触发 FD。
_DEFAULT_ENGINES = ["bfs", "unified_planning"]


# 多个 NPC 并行求解时共用 os.environ：第一个进入的线程去掉变量，最后一个离开的线程还原
_env_lock = threading.Lock()
_env_users = 0
_env_saved: Dict[str, Optional[str]] = {}


def _scrub_debugpy_for_planner_child() -> None:
    """临时去掉会让 debugpy 附着到 Fast Downward 子进程的环境变量（引用计数，线程安全）。"""
    global _env_users, _env_saved
    keys = (
        "PYDEVD_USE_CYTHON",
        "PYDEVD_USE_FRAME_EVAL",
        "PYDEVD_LOAD_VALUES_ASYNC",
        "DEBUGPY_LOG_DIR",
        "DEBUGPY_ADAPTER_ENDPOINTS",
        "PYDEVD_DISABLE_FILE_VALIDATION",
    )
    with _env_lock:
        _env_users += 1
        if _env_users > 1:
            return
        _env_saved = {k: os.environ.pop(k, None) for k in keys}
        # 告知部分 pydevd 版本不要追子进程
        os.environ["PYDEVD_DISABLE_FILE_VALIDATION"] = "1"


def _restore_env() -> None:
    global _env_users
    with _env_lock:
        _env_users -= 1
        if _env_users > 0:
            return
        for k, v in _env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# 部分 PDDL 引擎（如 Fast Downward）会把对象名 lowercase 化。
# 为了让计划能正确写回 Neo4j（CamelCase），solver 层需要把返回的小写参数
# 还原成原始 PDDL 中出现过的写法。
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def _build_case_map(problem_str: str) -> Dict[str, str]:
    """从 problem PDDL 字符串里收集所有标识符，建立 lower -> original 的映射。"""
    case_map: Dict[str, str] = {}
    for token in _TOKEN_RE.findall(problem_str):
        case_map.setdefault(token.lower(), token)
    return case_map


def _restore_case(name: str, case_map: Dict[str, str]) -> str:
    return case_map.get(name.lower(), name)


def _format_plan_raw(plan: List[PlanStep]) -> str:
    if not plan:
        return "(empty plan)"
    return "\n".join(
        f"{i}. {action}({', '.join(args)})" for i, (action, args) in enumerate(plan, 1)
    )


def _record_solve_trace(
    *,
    domain_str: str,
    problem_str: str,
    result: Dict[str, Any],
    elapsed_ms: float,
    attempt: int,
) -> None:
    """写入 /inspect 的 PDDL 求解记录（失败静默）。"""
    try:
        import sys
        from pathlib import Path

        agent_path = str(Path(__file__).resolve().parent.parent / "agent")
        if agent_path not in sys.path:
            sys.path.insert(0, agent_path)
        from trace_util import record_trace

        ok = bool(result.get("success"))
        plan = list(result.get("plan") or [])
        err = result.get("error") or ""
        engine = result.get("engine") or ""
        record_trace(
            kind="pddl_plan",
            prompt=(
                f"engine_attempt={attempt} engine={engine}\n"
                f"--- domain.pddl ---\n{domain_str[:6000]}\n"
                f"--- problem.pddl ---\n{problem_str[:8000]}"
            ),
            raw=_format_plan_raw(plan) if ok else (err or str(result.get("raw") or "规划失败")),
            ok=ok,
            error="" if ok else (err or "规划失败"),
            meta={
                "provider": "pddl",
                "engine": engine,
                "attempt": attempt,
                "phase": "solve",
                "plan_len": len(plan),
            },
            parsed={
                "plan": [{"action": a, "args": args} for a, args in plan],
                "engine": engine,
                "error": err,
                "raw": result.get("raw"),
            },
            elapsed_ms=elapsed_ms,
        )
    except Exception:
        pass


# 同一份 domain+problem 的求解结果（规划器是确定性的）。一次决策里可行性检查常把
# 同一个问题解好几遍，每遍都要起一个 ENHSP/JVM 子进程（1–3 秒）
_UP_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_UP_CACHE_MAX = 256
_up_cache_lock = threading.Lock()


def _solve_with_unified_planning_timeout(
    domain_str: str,
    problem_str: str,
    *,
    timeout_sec: float = _UP_TIMEOUT_SEC,
) -> Dict[str, Any]:
    key = hashlib.sha1(f"{domain_str}\0{problem_str}".encode("utf-8")).hexdigest()
    with _up_cache_lock:
        hit = _UP_CACHE.get(key)
        if hit is not None:
            _UP_CACHE.move_to_end(key)
            return copy.deepcopy(hit)
    result = _solve_with_unified_planning_timeout_uncached(domain_str, problem_str, timeout_sec=timeout_sec)
    if "超时" not in str(result.get("error") or ""):
        with _up_cache_lock:
            _UP_CACHE[key] = copy.deepcopy(result)
            while len(_UP_CACHE) > _UP_CACHE_MAX:
                _UP_CACHE.popitem(last=False)
    return result


def _solve_with_unified_planning_timeout_uncached(
    domain_str: str,
    problem_str: str,
    *,
    timeout_sec: float = _UP_TIMEOUT_SEC,
) -> Dict[str, Any]:
    """UP/Fast Downward 可能挂起；超时后返回失败以便切换 BFS。"""
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(_solve_with_unified_planning, domain_str, problem_str, timeout_sec)
    try:
        # 引擎自己也按 timeout 结束子进程；这里多给一点余量
        return fut.result(timeout=timeout_sec + 5)
    except FuturesTimeout:
        return {
            "success": False,
            "engine": "unified_planning",
            "plan": [],
            "raw": "",
            "error": f"unified_planning 超时（>{timeout_sec:.0f}s）",
        }
    finally:
        pool.shutdown(wait=False)


# ---------- 公共入口 ----------

def solve_pddl(
    domain_str: str,
    problem_str: str,
    engines: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    依次尝试若干引擎求解，第一个成功的返回。
    engines 默认 ["unified_planning", "bfs"]。
    每次引擎尝试都会写入 pddl_plan 日志（即使失败/超时）。
    """
    if engines is None:
        engines = list(_DEFAULT_ENGINES)

    last_error: Optional[str] = None
    last_engine = engines[-1] if engines else ""
    for attempt, engine in enumerate(engines, start=1):
        t0 = time.perf_counter()
        if engine == "unified_planning":
            result = _solve_with_unified_planning_timeout(domain_str, problem_str)
        elif engine == "bfs":
            result = _solve_with_bfs(domain_str, problem_str)
        else:
            result = {
                "success": False,
                "error": f"未知 engine: {engine}",
                "engine": engine,
                "plan": [],
                "raw": "",
            }

        # 仅记录 UP 尝试（贵/易挂）；BFS 由上层 plan_for_goal 记一条，避免双倍刷屏
        if engine == "unified_planning":
            _record_solve_trace(
                domain_str=domain_str,
                problem_str=problem_str,
                result=result,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
                attempt=attempt,
            )

        if result["success"]:
            return result
        last_error = result.get("error")
        last_engine = result.get("engine") or engine

    return {
        "success": False,
        "engine": last_engine,
        "plan": [],
        "raw": "",
        "error": last_error or "所有引擎都失败",
    }


# ---------- unified-planning ----------

def _solve_with_unified_planning(
    domain_str: str, problem_str: str, timeout: Optional[float] = None
) -> Dict[str, Any]:
    try:
        from unified_planning.environment import Environment
        from unified_planning.io import PDDLReader
    except ImportError as e:
        return {"success": False, "engine": "unified_planning", "plan": [],
                "raw": "", "error": f"unified-planning 未安装: {e}"}

    domain_path: Optional[str] = None
    problem_path: Optional[str] = None
    _scrub_debugpy_for_planner_child()
    try:
        # PDDLReader.parse_problem 需要文件路径，这里写到临时文件
        domain_fd = tempfile.NamedTemporaryFile(mode="w", suffix=".pddl",
                                                delete=False, encoding="utf-8")
        domain_fd.write(domain_str)
        domain_fd.close()
        domain_path = domain_fd.name

        problem_fd = tempfile.NamedTemporaryFile(mode="w", suffix=".pddl",
                                                 delete=False, encoding="utf-8")
        problem_fd.write(problem_str)
        problem_fd.close()
        problem_path = problem_fd.name

        # 每次求解用独立 Environment：UP 全局环境的表达式管理器不是线程安全的
        env = Environment()
        env.credits_stream = None
        reader = PDDLReader(environment=env)
        problem = reader.parse_problem(domain_path, problem_path)

        # 让 unified-planning 自己挑可用引擎；找不到就抛 UPNoRequestedEngineAvailableException
        if ":numeric-fluents" in domain_str:
            # ENHSP 默认 hadd+gbfs 计划绕路很多；WAStar 基本同速但计划短一半
            planner_ctx = env.factory.OneshotPlanner(name="enhsp", params={"params": _ENHSP_PARAMS})
        else:
            planner_ctx = env.factory.OneshotPlanner(problem_kind=problem.kind)
        with planner_ctx as planner:
            result = planner.solve(problem, timeout=timeout)
            engine_used = getattr(planner, "name", "auto")

        if result.plan is None:
            if "TIMEOUT" in str(result.status):
                return {"success": False, "engine": f"up:{engine_used}", "plan": [],
                        "raw": str(result), "error": f"unified_planning 超时（>{timeout:.0f}s）"}
            return {"success": False, "engine": f"up:{engine_used}", "plan": [],
                    "raw": str(result), "error": "未找到可行计划"}

        case_map = _build_case_map(problem_str)
        steps: List[PlanStep] = []
        for action_inst in result.plan.actions:
            name = action_inst.action.name
            args = [_restore_case(str(p), case_map) for p in action_inst.actual_parameters]
            steps.append((name, args))

        return {
            "success": True,
            "engine": f"up:{engine_used}",
            "plan": steps,
            "raw": str(result.plan),
            "error": None,
        }
    except SystemExit as e:
        # Fast Downward driver 以 sys.exit(code) 结束；若被误导入到同进程则在此吞掉
        code = e.code
        return {
            "success": False,
            "engine": "unified_planning",
            "plan": [],
            "raw": "",
            "error": f"规划器 SystemExit({code!r})（常见于 Fast Downward；已改回退）",
        }
    except Exception as e:
        return {"success": False, "engine": "unified_planning", "plan": [],
                "raw": "", "error": f"求解异常: {e}"}
    finally:
        _restore_env()
        for p in (domain_path, problem_path):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ---------- 内置 BFS（兜底） ----------

_AT_RE = re.compile(r"\(\s*at\s+(\S+?)\s+(\S+?)\s*\)")
_ADJ_RE = re.compile(r"\(\s*adjacent\s+(\S+?)\s+(\S+?)\s*\)")
_GOAL_AT_RE = re.compile(r":goal\s+\(\s*at\s+(\S+?)\s+(\S+?)\s*\)\s*\)")


def _solve_with_bfs(domain_str: str, problem_str: str) -> Dict[str, Any]:
    """
    简化的兜底求解器：只处理 move-only 的纯寻路问题。
    解析 problem 中的 (at ?p ?l)、(adjacent ?l1 ?l2)、(:goal (at ?p ?l))，
    在邻接图上做 BFS，输出 move 序列。
    """
    if "move" not in domain_str:
        return {"success": False, "engine": "bfs", "plan": [], "raw": "",
                "error": "BFS 兜底仅支持 move-only domain"}

    # 当前位置
    at_facts = _AT_RE.findall(problem_str)
    if not at_facts:
        return {"success": False, "engine": "bfs", "plan": [], "raw": "",
                "error": "init 中找不到 (at ...) 事实"}

    # 邻接表
    adjacency: Dict[str, List[str]] = {}
    for a, b in _ADJ_RE.findall(problem_str):
        adjacency.setdefault(a, []).append(b)

    goal_match = _GOAL_AT_RE.search(problem_str)
    if not goal_match:
        return {"success": False, "engine": "bfs", "plan": [], "raw": "",
                "error": "BFS 兜底仅支持 (:goal (at ?p ?l)) 形式"}
    goal_person, goal_loc = goal_match.group(1), goal_match.group(2)

    start_loc: Optional[str] = None
    for p, l in at_facts:
        if p == goal_person:
            start_loc = l
            break
    if start_loc is None:
        return {"success": False, "engine": "bfs", "plan": [], "raw": "",
                "error": f"init 中没有 {goal_person} 的位置"}

    if start_loc == goal_loc:
        return {"success": True, "engine": "bfs", "plan": [], "raw": "(empty)",
                "error": None}

    queue: deque = deque([(start_loc, [start_loc])])
    visited = {start_loc}
    while queue:
        cur, path = queue.popleft()
        for nxt in adjacency.get(cur, []):
            if nxt in visited:
                continue
            new_path = path + [nxt]
            if nxt == goal_loc:
                steps: List[PlanStep] = []
                for i in range(len(new_path) - 1):
                    from registry import act

                    steps.append((act("travel"), [goal_person, new_path[i], new_path[i + 1]]))
                return {"success": True, "engine": "bfs", "plan": steps,
                        "raw": " -> ".join(new_path), "error": None}
            visited.add(nxt)
            queue.append((nxt, new_path))

    return {"success": False, "engine": "bfs", "plan": [], "raw": "",
            "error": f"BFS 无法从 {start_loc} 到 {goal_loc}"}


# ---------- 计划执行 ----------

def execute_plan(world, plan: List[PlanStep], registry=None) -> Tuple[bool, List[str]]:
    """
    用 ActionRegistry 把计划逐步落到 Neo4j。
    返回 (是否全部成功, 每步状态文本列表)。
    """
    if registry is None:
        from registry import get_registry
        registry = get_registry()

    logs: List[str] = []
    all_ok = True

    for step_idx, (action_name, args) in enumerate(plan, start=1):
        action = registry.get(action_name)
        if action is None:
            logs.append(f"[{step_idx}] 未知动作 {action_name}")
            all_ok = False
            continue

        # 把 PDDL 计划里的位置参数 (a, b, c, ...) 按动作 schema 映射到 kwargs
        kwargs = {}
        for param, value in zip(action.parameters, args):
            kwargs[param.name] = value

        ok, reason = registry.execute(world, action_name, **kwargs)
        flag = "OK  " if ok else "FAIL"
        logs.append(f"[{step_idx}] [{flag}] {action_name}({', '.join(args)}) -> {reason}")
        if not ok:
            all_ok = False
            break

    return all_ok, logs
