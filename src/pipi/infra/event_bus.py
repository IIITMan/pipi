"""事件总线 —— asyncio 实现的发布/订阅，用于模块间解耦。

设计取舍：
  - 用 asyncio.Queue 而非 Redis/Kafka：单进程足够，避免运维负担
  - 每个订阅者一个独立队列：慢订阅者不阻塞快订阅者
  - 队列满时丢弃最旧事件（感知流允许丢帧，不能反压）
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, TypeVar

from pipi.domain.events import SystemEvent

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=SystemEvent)
Handler = Callable[[Any], Awaitable[None]]


class Subscription:
    """一个订阅句柄，可用于取消。"""

    def __init__(self, bus: "EventBus", event_type: type, queue: asyncio.Queue):
        self._bus = bus
        self.event_type = event_type
        self.queue = queue
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._unsubscribe(self)

    async def get(self, timeout: float | None = None) -> Any | None:
        try:
            if timeout is None:
                return await self.queue.get()
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed:
            raise StopAsyncIteration
        item = await self.queue.get()
        return item


class EventBus:
    """异步事件总线。

    用法：
        bus = EventBus()
        sub = bus.subscribe(OwnerArrivedEvent)
        bus.publish(OwnerArrivedEvent(...))
        async for evt in sub:
            ...
    """

    def __init__(self, queue_size: int = 64, drop_oldest: bool = True):
        self._subs: dict[type, list[Subscription]] = defaultdict(list)
        self._queue_size = queue_size
        self._drop_oldest = drop_oldest
        self._published = 0
        self._dropped = 0

    # ---- 订阅 ----

    def subscribe(self, event_type: type[E], queue_size: int | None = None) -> Subscription:
        q: asyncio.Queue = asyncio.Queue(maxsize=queue_size or self._queue_size)
        sub = Subscription(self, event_type, q)
        self._subs[event_type].append(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        lst = self._subs.get(sub.event_type)
        if lst and sub in lst:
            lst.remove(sub)

    # ---- 发布 ----

    def publish(self, event: Any) -> int:
        """发布事件。精确匹配类型，也支持基类订阅（isinstance）。"""
        self._published += 1
        delivered = 0
        for etype, subs in self._subs.items():
            if not isinstance(event, etype):
                continue
            for sub in subs:
                if sub._closed:
                    continue
                try:
                    sub.queue.put_nowait(event)
                    delivered += 1
                except asyncio.QueueFull:
                    if self._drop_oldest:
                        try:
                            sub.queue.get_nowait()
                            sub.queue.put_nowait(event)
                            self._dropped += 1
                            delivered += 1
                        except Exception:
                            pass
                    else:
                        self._dropped += 1
                        logger.warning("事件队列满，丢弃 %s", type(event).__name__)
        return delivered

    # ---- 统计 ----

    @property
    def stats(self) -> dict[str, int]:
        return {
            "published": self._published,
            "dropped": self._dropped,
            "subscribers": sum(len(v) for v in self._subs.values()),
        }

    def close(self) -> None:
        for subs in self._subs.values():
            for sub in subs:
                sub.close()
        self._subs.clear()
