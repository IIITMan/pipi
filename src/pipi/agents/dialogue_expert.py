"""对话生成 Agent —— 组装 Prompt 并调用 LLM。"""

from __future__ import annotations

import logging
import time

from pipi.agents.base import AgentContext, AgentResult, BaseAgent
from pipi.providers.llm.base import LLMProvider
from pipi.providers.llm.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)


class DialogueExpert(BaseAgent):
    """生成皮皮的回复。"""

    name = "dialogue_expert"

    def __init__(self, llm: LLMProvider, prompt_builder: PromptBuilder,
                 use_cheap_routing: bool = True, simple_max_chars: int = 8):
        self.llm = llm
        self.pb = prompt_builder
        self.use_cheap_routing = use_cheap_routing
        self.simple_max_chars = simple_max_chars

    def _prepare(self, ctx: AgentContext) -> tuple[list[dict[str, str]], float, int, bool]:
        """组装消息与生成参数。"""
        # 需要安抚时，在 system 后追加一条指令
        extra = ""
        if ctx.needs_comfort:
            extra = (
                "[重要] 他现在情绪不好。先接住情绪，不要给建议，"
                "不要说教，可以陪他一起丧，或者就安静陪着。"
            )

        # 视觉提示
        messages = self.pb.build_messages(
            user_input=ctx.user_text,
            history=ctx.history,
            current_time=_fmt_time(ctx.now),
            profile=ctx.profile,
            memories=ctx.memories,
            emotion_state=ctx.emotion_state,
            vision=ctx.vision,
            has_vision=ctx.vision is not None,
            extra=extra,
        )

        # 成本控制：极短输入走便宜模型
        use_cheap = (
            self.use_cheap_routing
            and len(ctx.user_text) <= self.simple_max_chars
            and ctx.persona is not None
            and ctx.persona.traits.verbosity < 0.7
        )
        temperature = ctx.persona.temperature if ctx.persona else 0.9
        max_tokens = ctx.persona.max_tokens if ctx.persona else 150
        return messages, temperature, max_tokens, use_cheap

    async def run(self, ctx: AgentContext) -> AgentResult:
        start = time.time()
        messages, temperature, max_tokens, use_cheap = self._prepare(ctx)

        try:
            resp = await self.llm.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                use_cheap=use_cheap,
            )
        except Exception as exc:
            logger.error("LLM 生成失败：%s", exc)
            return AgentResult(ok=False, error=str(exc), data=None)

        return AgentResult(
            ok=True,
            data=resp,
            latency_ms=int((time.time() - start) * 1000),
        )

    async def stream(self, ctx: AgentContext):
        """流式生成回复（token 增量）。仅当 LLM 支持流式时可用。"""
        messages, temperature, max_tokens, use_cheap = self._prepare(ctx)
        async for delta in self.llm.stream(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            use_cheap=use_cheap,
        ):
            yield delta


def _fmt_time(ts: float) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ts))
