"""对话生成 Agent —— 组装 Prompt 并调用 LLM。"""

from __future__ import annotations

import logging
import random
import time
from types import SimpleNamespace
from typing import Any

from pipi.agents.base import AgentContext, AgentResult, BaseAgent
from pipi.agents.scenarios import detect_scene, pick_quote, pick_reply
from pipi.providers.llm.base import LLMProvider
from pipi.providers.llm.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)


class DialogueExpert(BaseAgent):
    """生成皮皮的回复。"""

    name = "dialogue_expert"

    def __init__(self, llm: LLMProvider, prompt_builder: PromptBuilder,
                 use_cheap_routing: bool = True, simple_max_chars: int = 8,
                 growth: Any | None = None):
        self.llm = llm
        self.pb = prompt_builder
        self.use_cheap_routing = use_cheap_routing
        self.simple_max_chars = simple_max_chars
        self.growth = growth              # 场景路由需要阶段/学习表达
        self._quote_count: dict[str, int] = {}   # D4：每会话引用上限

    def _stage_of(self, ctx: AgentContext) -> int:
        """当前用户的成长阶段（取不到按 S1，走最保守话术）。"""
        try:
            gp = self.growth.load(ctx.user_id)
            return gp.stage
        except Exception:
            return 1

    def _scenario_reply(self, ctx: AgentContext) -> str | None:
        """D1：场景识别命中且阶段解锁 → 直接给话术（跳过 LLM）。"""
        if self.growth is None:
            return None
        scene = detect_scene(ctx.user_text)
        if scene is None:
            return None
        stage = self._stage_of(ctx)
        reply = pick_reply(scene, stage)
        if reply is None:
            return None
        # D4：模仿引用——高频口头禅按概率挂尾（每会话 ≤2 次）
        try:
            gp = self.growth.load(ctx.user_id)
            quote = pick_quote(gp.learned_expressions)
            key = ctx.trace_id or "default"
            used = self._quote_count.get(key, 0)
            if quote and used < 2 and random.random() < 0.5 \
                    and quote not in ctx.user_text:
                reply = f"{reply}（{quote}……哎呀，跟你学的）"
                self._quote_count[key] = used + 1
        except Exception:
            pass
        logger.info("场景路由命中：%s（stage=%s）", scene, stage)
        return reply

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

        # D1：场景话术短路（不调 LLM）
        scenario = self._scenario_reply(ctx)
        if scenario is not None:
            return AgentResult(
                ok=True,
                data=SimpleNamespace(text=scenario, model="scenario"),
                latency_ms=int((time.time() - start) * 1000),
            )

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
        # D1：场景话术短路（流式路径同样生效，整句作为单个增量）
        scenario = self._scenario_reply(ctx)
        if scenario is not None:
            yield scenario
            return
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
