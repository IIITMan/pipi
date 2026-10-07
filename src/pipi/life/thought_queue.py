"""生命模块 · 念头结构、队列与过期清理（§4）。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Thought:
    """§4.1 念头结构。"""

    content: str                       # 念头摘要文本
    priority: int = 2                  # 1~5
    type: str = "self_talk"            # self_talk/ask_owner/memory_flashback/patrol/explore/rest
    source: str = "social"             # social/memory/patrol/explore/rest
    vocal_enable: bool = False         # false=仅内心，不生成语音
    motion_enable: bool = False        # 是否配套机器人动作
    motion: str | None = None          # 配套动作标识（patrol_route/look_around 等）
    motion_duration: float = 1.5       # 动作预估时长（巡逻类更长）
    expire_time: float = field(default_factory=lambda: time.monotonic() + 5.0)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def expired(self) -> bool:
        return time.monotonic() > self.expire_time


class ThoughtQueue:
    """念头队列：入队、过期清理（§9.2 存活 5s）、按优先级取最优。"""

    def __init__(self, ttl_s: float = 5.0):
        self.ttl_s = ttl_s
        self._items: list[Thought] = []

    def push(self, thought: Thought) -> None:
        thought.expire_time = time.monotonic() + self.ttl_s
        self._items.append(thought)

    def purge(self) -> int:
        """过期清理，返回丢弃数量（模拟「想到又忘记」）。"""
        before = len(self._items)
        self._items = [t for t in self._items if not t.expired]
        return before - len(self._items)

    def size(self) -> int:
        self.purge()
        return len(self._items)

    def pop_best(self) -> Thought | None:
        """取当前未过期、优先级最高的念头。"""
        self.purge()
        if not self._items:
            return None
        self._items.sort(key=lambda t: t.priority, reverse=True)
        return self._items.pop(0)

    def clear(self) -> None:
        self._items.clear()
