"""记忆检索 Agent —— 找出相关记忆并强化它们。"""

from __future__ import annotations

import logging
import time

from pipi.agents.base import AgentContext, AgentResult, BaseAgent
from pipi.capabilities.memory_manager import MemoryManager

logger = logging.getLogger(__name__)


class MemoryExpert(BaseAgent):
    """检索与当前话题相关的长期记忆。"""

    name = "memory_expert"

    def __init__(self, memory: MemoryManager, top_k: int = 5):
        self.memory = memory
        self.top_k = top_k

    async def run(self, ctx: AgentContext) -> AgentResult:
        start = time.time()
        # 用用户输入 + 最近一轮作为检索 query（提升召回质量）
        query = ctx.user_text or ""
        if ctx.history:
            last = ctx.history[-1].get("content", "")
            if last and last not in query:
                query = f"{query} {last}"[:200]

        try:
            memories = await self.memory.recall(
                ctx.user_id, query, ctx.now,
                top_k=self.top_k, reinforce=True, turn_id=ctx.turn_id,
            )
        except Exception as exc:
            logger.warning("记忆检索失败：%s", exc)
            memories = []

        ctx.memories = memories

        return AgentResult(
            ok=True,
            data=memories,
            latency_ms=int((time.time() - start) * 1000),
        )
