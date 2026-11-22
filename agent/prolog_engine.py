"""
Prolog 引擎（SWI-Prolog，经 pyswip）——只负责「装规则、装事实、证明目标」，不认识任何玩法。

SWI-Prolog 的引擎不能被多个 Python 线程同时用（游戏里规划是并行的），所以全部查询交给
一个专门的工作线程排队执行。每次 solve() 的「换规则文件（如有变化）→ 换事实 → 证明」是一个整体，
中间不会被别的查询插进来。

    from prolog_engine import solve, fact
    rows = solve(files, [fact("g_node", "Person", "Ash")], "findall(P, g_node('Person', P), L)")

规则文件由调用方给出（见 rulebook.rule_files）；facts 由 load_facts/1 装入（rules_core.pl）。
没装 SWI-Prolog 时抛 PrologUnavailable（安装方法见 README）。
"""

from __future__ import annotations

import atexit
import queue
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence


class PrologUnavailable(RuntimeError):
    pass


def atom(value: Any) -> str:
    """Python 值 → Prolog 字面量：数字、布尔（true/false）、列表原样，其余一律当带引号的原子。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"({value!r})" if value < 0 else repr(value)  # 括起来：'k'-(-9) 而不是 'k'--9
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(atom(v) for v in value) + "]"
    text = "" if value is None else str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def fact(name: str, *args: Any) -> str:
    return f"{name}({', '.join(atom(a) for a in args)})"


class _Engine:
    def __init__(self) -> None:
        self._jobs: "queue.Queue[tuple]" = queue.Queue()
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, name="prolog", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            raise PrologUnavailable(str(self._error)) from self._error
        atexit.register(self.shutdown)  # 比 pyswip 自己的退出钩子晚注册，所以先执行

    def shutdown(self) -> None:
        if self._thread.is_alive():
            reply: "queue.Queue[tuple]" = queue.Queue(maxsize=1)
            self._jobs.put(((), [], None, reply))
            reply.get(timeout=5)

    def _run(self) -> None:
        try:
            from pyswip import Prolog

            prolog = Prolog()
        except BaseException as e:  # noqa: BLE001  pyswip 找不到 libswipl 时可能抛各种异常
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        loaded: Sequence[str] = ()
        while True:
            files, facts, goal, reply = self._jobs.get()
            if goal is None:  # 退出：在本线程里收尾（否则退出时 SWI 会抱怨停不掉线程）
                from pyswip.core import PL_cleanup, PL_thread_self, _lib

                if PL_thread_self() == 1:  # 是本线程初始化的 Prolog：整个关掉
                    PL_cleanup(0)
                else:  # pyswip 先在别的线程导入过：只释放本线程的引擎，剩下的交给 pyswip
                    _lib.PL_thread_destroy_engine()
                reply.put((True, []))
                return
            try:
                if tuple(files) != tuple(loaded):
                    for path in loaded:
                        list(prolog.query(f"unload_file({atom(path)})"))
                    loaded = ()
                    for path in files:
                        prolog.consult(path)
                    loaded = tuple(files)
                list(prolog.query(f"load_facts([{', '.join(facts)}])"))
                reply.put((True, list(prolog.query(goal))))
            except BaseException as e:  # noqa: BLE001
                reply.put((False, e))

    def solve(self, files: Sequence[str], facts: Iterable[str], goal: str) -> List[Dict[str, Any]]:
        reply: "queue.Queue[tuple]" = queue.Queue(maxsize=1)
        self._jobs.put((tuple(files), list(facts), goal, reply))
        ok, value = reply.get()
        if not ok:
            raise value
        return value


_engine: Optional[_Engine] = None
_failure: Optional[PrologUnavailable] = None
_lock = threading.Lock()


def engine() -> _Engine:
    global _engine, _failure
    with _lock:
        if _failure is not None:
            raise _failure
        if _engine is None:
            try:
                _engine = _Engine()
            except PrologUnavailable as e:
                _failure = e
                raise
        return _engine


def solve(files: Sequence[str], facts: Iterable[str], goal: str) -> List[Dict[str, Any]]:
    """用这组规则文件（有变化才重新加载）和这次的 facts 证明 goal；返回所有解（变量名 → 值）。"""
    return engine().solve(files, facts, goal)
