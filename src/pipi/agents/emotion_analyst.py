"""情感分析 Agent —— 融合文本、语音、表情三路信号。"""

from __future__ import annotations

import logging
import time

from pipi.agents.base import AgentContext, AgentResult, BaseAgent
from pipi.capabilities.emotion_engine import EmotionEngine, EmotionSignal

logger = logging.getLogger(__name__)


class EmotionAnalyst(BaseAgent):
    """分析用户当前的情绪状态。"""

    name = "emotion_analyst"

    def __init__(self, engine: EmotionEngine):
        self.engine = engine

    async def run(self, ctx: AgentContext) -> AgentResult:
        start = time.time()
        signals: list[EmotionSignal] = []

        # 1. 文本信号（总是可用）
        if ctx.user_text:
            signals.append(self.engine.analyze_text(ctx.user_text))

        # 2. 语音情感（SenseVoice 原生输出）
        if ctx.transcript and ctx.transcript.emotion:
            if ctx.transcript.emotion.upper() != "NEUTRAL":
                signals.append(self.engine.signal_from_asr(
                    ctx.transcript.emotion, ctx.transcript.emotion_confidence
                ))

        # 3. 视觉表情
        if ctx.vision and ctx.vision.owner_present:
            if expr := ctx.vision.expressions.get("owner"):
                signals.append(self.engine.signal_from_vision(expr.label, expr.confidence))

        # 融合更新情绪状态
        if ctx.emotion_state is not None:
            updated, trigger = self.engine.update(ctx.emotion_state, signals, ctx.now)
            ctx.emotion_state = updated
        else:
            trigger = "no_state"

        ctx.emotion_signals = signals
        ctx.needs_comfort = self.engine.needs_comfort(signals)

        return AgentResult(
            ok=True,
            data={
                "signals": signals,
                "trigger": trigger,
                "needs_comfort": ctx.needs_comfort,
            },
            latency_ms=int((time.time() - start) * 1000),
        )
