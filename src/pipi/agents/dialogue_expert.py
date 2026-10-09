"""对话生成 Agent —— 组装 Prompt 并调用 LLM。

工具调用协议（单请求内嵌意图）：
  system prompt 要求 LLM 在需要外部信息时，第一行只输出标记后停止：
    [WEATHER:城市] / [SEARCH:关键词] / [VISION:观察要点]
  编排层捕获标记 → 执行工具 → 携带结果重新生成（第二轮禁再标记）。
  闲聊/情感/时间日期/对皮皮的请求 → 直接回答，不输出标记。
  这样意图识别内嵌在主请求里，none 类对话零额外请求。
"""

from __future__ import annotations

import logging
import random
import re
import time
from types import SimpleNamespace
from typing import Any

from pipi.agents.base import AgentContext, AgentResult, BaseAgent
from pipi.agents.scenarios import detect_scene, pick_quote, pick_reply
from pipi.providers.llm.base import LLMProvider
from pipi.providers.llm.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)

_TOOL_RE = re.compile(
    r"^\s*\[(WEATHER|SEARCH|VISION)\s*[:：]\s*([^\]]{0,80})\]")

TOOL_PROTOCOL = (
    "\n\n# 工具调用协议（必须遵守）\n"
    "若回答前需要外部实时信息，你的回复第一行只输出一个标记，然后立即停止"
    "（标记后面什么都不要写，系统会把资料给你，你再正常回答）：\n"
    "[WEATHER:城市]  —— 天气/气温/降雨类\n"
    "[SEARCH:关键词] —— 需要查外部资料（百科/人物/新闻/历史事件）\n"
    "[VISION:要点]   —— 需要看摄像头画面（用户问穿着/表情/你能否看见）\n"
    "闲聊、情感表达、时间日期、关于皮皮自己的状态与能力、对皮皮的请求，"
    "一律直接正常回答，绝不输出标记。\n"
)


class ToolCallSignal(Exception):
    """LLM 输出了工具标记：携带 (tool, query)，由编排层执行工具并重生成。"""

    def __init__(self, tool: str, query: str):
        self.tool = tool.lower()
        self.query = query.strip()
        super().__init__(f"{self.tool}: {self.query}")


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

    def _prepare(self, ctx: AgentContext,
                 tool_round: int = 0) -> tuple[list[dict[str, str]], float, int, bool]:
        """组装消息与生成参数。tool_round>0 表示工具结果已注入的第二轮。"""
        # 需要安抚时，在 system 后追加一条指令
        extra = ""
        if ctx.needs_comfort:
            extra = (
                "[重要] 他现在情绪不好。先接住情绪，不要给建议，"
                "不要说教，可以陪他一起丧，或者就安静陪着。"
            )

        # 工具查询结果（如实时天气）：LLM 把它当"皮皮刚查到的事实"来说
        if ctx.tool_brief:
            extra += (f"\n\n[工具结果] 这是你刚查到的实时信息，"
                      f"基于它回答，用你的口吻：{ctx.tool_brief}")

        if tool_round == 0:
            extra += TOOL_PROTOCOL
        else:
            # 第二轮（工具已执行）：严禁再输出标记，直接以皮皮口吻回答
            extra += ("\n\n[系统] 查询已完成：资料可用就用，查不到/超时"
                      "就直接以你的口吻正常回答。绝对不要再输出 [WEATHER]"
                      "/[SEARCH]/[VISION] 标记。")

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
            inner_state=ctx.inner_brief,
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

    async def run(self, ctx: AgentContext, tool_round: int = 0) -> AgentResult:
        start = time.time()

        # D1：场景话术短路（不调 LLM）
        scenario = self._scenario_reply(ctx)
        if scenario is not None:
            return AgentResult(
                ok=True,
                data=SimpleNamespace(text=scenario, model="scenario"),
                latency_ms=int((time.time() - start) * 1000),
            )

        messages, temperature, max_tokens, use_cheap = self._prepare(
            ctx, tool_round)

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

        text = (getattr(resp, "text", "") or "")
        m = _TOOL_RE.match(text.strip()) if tool_round == 0 else None
        if m:
            raise ToolCallSignal(m.group(1), m.group(2))

        return AgentResult(
            ok=True,
            data=resp,
            latency_ms=int((time.time() - start) * 1000),
        )

    async def stream(self, ctx: AgentContext, tool_round: int = 0):
        """流式生成回复（token 增量）。仅当 LLM 支持流式时可用。

        开头缓冲最多 14 字符做工具标记检测：命中 → 抛 ToolCallSignal
        （编排层执行工具后以 tool_round=1 重入）；判定非标记 → 冲刷缓冲
        正常透传。
        """
        # D1：场景话术短路（流式路径同样生效，整句作为单个增量）
        scenario = self._scenario_reply(ctx)
        if scenario is not None:
            yield scenario
            return
        messages, temperature, max_tokens, use_cheap = self._prepare(
            ctx, tool_round)
        buf = ""
        decided = tool_round > 0   # 第二轮协议已禁标记，无需检测
        async for delta in self.llm.stream(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            use_cheap=use_cheap,
        ):
            if decided:
                yield delta
                continue
            buf += delta
            stripped = buf.lstrip()
            m = _TOOL_RE.match(stripped)
            if m:
                raise ToolCallSignal(m.group(1), m.group(2).strip())
            if stripped.startswith("[") and "]" not in stripped and len(buf) < 96:
                continue   # 疑似标记未闭合 → 继续等（要点可能很长）
            decided = True
            if buf:
                yield buf
                continue
        if not decided and buf:
            yield buf   # 流结束时仍未判定（短回答），冲刷缓冲


def _fmt_time(ts: float) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ts))
