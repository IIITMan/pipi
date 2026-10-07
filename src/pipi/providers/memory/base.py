"""记忆检索接口定义。"""

from __future__ import annotations

from typing import Protocol

from pipi.domain.memory import (
    DecayReport,
    MemoryItem,
    RetrievedMemory,
    RetrieveQuery,
)


class MemoryStore(Protocol):
    """记忆存储接口。

    实现者需保证：
      - write 后立即可被 retrieve 检索到
      - decay_pass 是幂等的（同一 now 重复调用结果一致）
      - pinned 记忆永不衰减、永不被淘汰
    """

    async def write(self, item: MemoryItem) -> str: ...

    async def write_batch(self, items: list[MemoryItem]) -> list[str]: ...

    async def retrieve(self, q: RetrieveQuery) -> list[RetrievedMemory]: ...

    async def reinforce(self, ids: list[str], now: float) -> None: ...

    async def decay_pass(self, user_id: str, now: float) -> DecayReport: ...

    async def forget(self, ids: list[str]) -> None: ...

    async def export(self, user_id: str) -> dict: ...

    async def count(self, user_id: str) -> int: ...
