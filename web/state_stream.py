"""
Play-state 推送（Server-Sent Events）。

模拟跑完一拍、或任何改世界的请求之后标记 dirty；有订阅者时只算一次 play state，
广播给所有客户端。每个订阅者只保留最新一份（客户端慢就丢旧的，不堆积）。
"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import Executor
from typing import Any, AsyncIterator, Callable, Dict, Optional, Set

KEEPALIVE_SECONDS = 15.0


class StateHub:
    def __init__(
        self,
        build: Callable[[], Dict[str, Any]],
        executor: Executor,
        *,
        min_interval: float = 0.1,
    ) -> None:
        self._build = build
        self._executor = executor
        self._min_interval = min_interval
        self._subscribers: Set[asyncio.Queue] = set()
        self._dirty: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._latest: Optional[str] = None
        self._version = 0
        self._task: Optional[asyncio.Task] = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._loop = asyncio.get_running_loop()
            self._dirty = asyncio.Event()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def mark_dirty(self) -> None:
        """可从事件循环或其他线程调用。"""
        loop, dirty = self._loop, self._dirty
        if loop is None or dirty is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            dirty.set()
        else:
            loop.call_soon_threadsafe(dirty.set)

    async def _run(self) -> None:
        assert self._dirty is not None
        loop = asyncio.get_running_loop()
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            if not self._subscribers:
                # 没人看就不算；下一个订阅者来时会重新标记 dirty
                self._latest = None
                continue
            try:
                payload = await loop.run_in_executor(self._executor, self._build)
            except Exception as e:  # noqa: BLE001
                payload = {"found": False, "message": f"state build failed: {e}"}
            self._version += 1
            payload["stream"] = {"version": self._version, "server_time": time.time()}
            data = json.dumps(payload, ensure_ascii=False, default=str)
            self._latest = data
            for queue in list(self._subscribers):
                _put_latest(queue, data)
            # 合并密集的更新（例如模拟间隔很短时）
            await asyncio.sleep(self._min_interval)

    async def events(self) -> AsyncIterator[str]:
        """SSE 帧序列：event: state + keepalive 注释。"""
        self.start()  # 第一个订阅者到来时才启动后台任务
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        if self._latest is not None:
            _put_latest(queue, self._latest)
        else:
            self.mark_dirty()
        try:
            yield ": connected\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: state\ndata: {data}\n\n"
        finally:
            self._subscribers.discard(queue)


def _put_latest(queue: asyncio.Queue, data: str) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(data)
