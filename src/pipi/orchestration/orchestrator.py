"""会话编排器 —— 系统的主循环。

一次完整对话的数据流：

  用户输入
     │
     ├─[并行] EmotionAnalyst  情感分析
     ├─[并行] MemoryExpert    记忆检索 + 强化
     │
     ├─► DialogueExpert       组装 Prompt → LLM → 原始回复
     │
     ├─► ExpressionService    去 Markdown / 熔断 / 拆气泡
     │
     ├─► TTS（可选）          合成语音
     ├─► Motion（可选）       肢体动作
     │
     └─[异步] 记忆写入 + 持久化 + 情绪落库

设计要点：
  - 每轮重注入完整 System Prompt（防止人格漂移）
  - 情感与记忆并行（用 asyncio.gather）
  - 记忆写入异步化，不阻塞回复
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pipi.agents.base import AgentContext
from pipi.agents.dialogue_expert import DialogueExpert, ToolCallSignal
from pipi.agents.emotion_analyst import EmotionAnalyst
from pipi.agents.memory_expert import MemoryExpert
from pipi.capabilities.expression import PostProcessResult, postprocess, to_bubbles
from pipi.capabilities.memory_manager import MemoryManager
from pipi.domain.dialogue import Bubble, DialogueTurn, Role, Session
from pipi.domain.emotion import EmotionState
from pipi.domain.persona import PersonaSpec
from pipi.domain.profile import UserProfile
from pipi.domain.vision import VisionObservation
from pipi.infra.clock import Clock
from pipi.infra.db import (
    EmotionRepo,
    ProfileRepo,
    TurnRepo,
    UserRepo,
)
from pipi.providers.llm.base import LLMProvider

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """一轮对话的完整结果。"""

    bubbles: list[Bubble] = field(default_factory=list)
    raw_text: str = ""
    emotion_state: EmotionState | None = None
    memories_used: list[str] = field(default_factory=list)
    memories_written: list[str] = field(default_factory=list)
    latency_ms: int = 0
    degraded: bool = False
    error: str = ""

    @property
    def text(self) -> str:
        return " ".join(b.text for b in self.bubbles)


class ConversationOrchestrator:
    """对话主循环编排。"""

    def __init__(
        self,
        *,
        llm: LLMProvider,
        persona: PersonaSpec,
        memory: MemoryManager,
        emotion_analyst: EmotionAnalyst,
        memory_expert: MemoryExpert,
        dialogue_expert: DialogueExpert,
        clock: Clock,
        user_repo: UserRepo,
        profile_repo: ProfileRepo,
        turn_repo: TurnRepo,
        emotion_repo: EmotionRepo,
        tts: Any | None = None,
        motion: Any | None = None,
        owner_name: str = "主人",
        enable_proactive_emotion: bool = True,
        growth: Any | None = None,
        reminders: Any | None = None,
        inner_state_provider: Callable[[], str] | None = None,
        weather: Any | None = None,
        search: Any | None = None,
        env: Any | None = None,
        cfg: Any | None = None,
    ):
        self.llm = llm
        self.persona = persona
        self.memory = memory
        self.search = search
        self.env = env
        self.cfg_snapshot = (getattr(cfg, "data", None) or {}) if cfg is not None \
            else (cfg or {})
        self.emotion_analyst = emotion_analyst
        self.memory_expert = memory_expert
        self.dialogue_expert = dialogue_expert
        self.clock = clock
        self.user_repo = user_repo
        self.profile_repo = profile_repo
        self.turn_repo = turn_repo
        self.emotion_repo = emotion_repo
        self.tts = tts
        self.motion = motion
        self.owner_name = owner_name
        self.enable_proactive_emotion = enable_proactive_emotion
        self.growth = growth   # GrowthEngine（可成长性，可为 None）
        self.reminders = reminders   # ReminderService（提醒/闹铃/待办，可为 None）
        # PULSE 内心状态提供者（每轮拉取注入对话；legacy/无 PULSE 时为 None）
        self.inner_state_provider = inner_state_provider
        self.weather = weather   # 天气工具（可为 None，不启用）
        self.search = None       # 通用搜索工具（app 装配，可为 None）
        self.env = None          # 环境档案（贾维斯化③，app 装配，可为 None）

        # 会话缓存
        self._sessions: dict[str, Session] = {}
        self._states: dict[str, EmotionState] = {}
        self._profiles: dict[str, UserProfile] = {}

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def get_session(self, user_id: str) -> Session:
        if user_id not in self._sessions:
            self._sessions[user_id] = Session(user_id=user_id)
        return self._sessions[user_id]

    def get_state(self, user_id: str) -> EmotionState:
        if user_id not in self._states:
            self._states[user_id] = self.emotion_repo.load(user_id, self.clock.now())
        return self._states[user_id]

    def get_profile(self, user_id: str) -> UserProfile:
        if user_id not in self._profiles:
            self._profiles[user_id] = self.profile_repo.get(user_id)
        return self._profiles[user_id]

    def reload_profile(self, user_id: str) -> UserProfile:
        self._profiles[user_id] = self.profile_repo.get(user_id)
        return self._profiles[user_id]

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def handle_turn(
        self,
        user_id: str,
        user_text: str,
        *,
        transcript: Any | None = None,
        vision: VisionObservation | None = None,
        persist: bool = True,
        on_notice: Any = None,
    ) -> TurnResult:
        """处理一轮对话。

        on_notice：工具查询（天气/搜索/视觉）启动时的"先应一声"回调，
        接收一句提示文本（如"等等哈，我查查……"）。
        """
        # 提醒/待办入口：规则解析命中则直接返回（不消耗 LLM）
        if self.reminders is not None:
            try:
                handled, reply = self.reminders.try_parse(user_id, user_text)
                if handled:
                    start = time.time()
                    pp = postprocess(reply, self.persona.speech_style)
                    bubbles = to_bubbles(pp, self.persona.speech_style)
                    return TurnResult(
                        bubbles=bubbles, raw_text=reply,
                        latency_ms=int((time.time() - start) * 1000),
                        degraded=False,
                    )
            except Exception as exc:
                logger.warning("提醒意图解析失败（回落对话）：%s", exc)

        start, now, ctx, session, profile = self._prepare_context(
            user_id, user_text, transcript=transcript, vision=vision)

        # 环境档案（贾维斯化③）：顺带记下提到的地点/物品/家人（永不阻塞）
        if self.env is not None:
            try:
                self.env.observe_chat(user_text)
            except Exception as exc:
                logger.debug("环境档案记录失败（忽略）：%s", exc)

        # ---- 并行：情感分析 + 记忆检索 ----
        emo_task = asyncio.create_task(self.emotion_analyst.run(ctx))
        mem_task = asyncio.create_task(self.memory_expert.run(ctx))
        emo_res, mem_res = await asyncio.gather(emo_task, mem_task,
                                                return_exceptions=True)
        if isinstance(emo_res, Exception):
            logger.warning("情感分析 Agent 失败：%s", emo_res)
        if isinstance(mem_res, Exception):
            logger.warning("记忆检索 Agent 失败：%s", mem_res)

        # ---- 生成（工具标记 → 执行工具 → 携带结果重生成，最多 2 轮）----
        raw_text = ""
        degraded = False
        for attempt in range(2):
            try:
                gen_res = await self.dialogue_expert.run(ctx, tool_round=attempt)
                if not gen_res.ok or gen_res.data is None:
                    degraded = attempt > 0
                    raw_text = self._fallback_reply(gen_res.error)
                else:
                    raw_text = gen_res.data.text
                break
            except ToolCallSignal as sig:
                await self._run_tool(sig, ctx, vision, user_text, on_notice)
                continue
        return await self._finalize_turn(
            ctx, session, user_id, user_text, raw_text, degraded, start,
            persist=persist, now=now, profile=profile)

    async def handle_turn_stream(
        self,
        user_id: str,
        user_text: str,
        *,
        on_chunk: Callable[[str], Any] | None = None,
        transcript: Any | None = None,
        vision: VisionObservation | None = None,
        persist: bool = True,
        on_notice: Any = None,
    ) -> TurnResult:
        """流式处理一轮对话。

        LLM token 级增量输出，每个增量回调 on_chunk(delta)（可为 async），
        调用方可边收边喂给 TTS 分句合成，实现"边生成边播放"。
        LLM 不支持流式或流式失败时自动回退非流式。
        on_notice：工具查询启动时的"先应一声"回调（同 handle_turn）。
        """
        # 提醒/待办入口（与 handle_turn 一致：命中直接返回，不走 LLM）
        if self.reminders is not None:
            try:
                handled, reply = self.reminders.try_parse(user_id, user_text)
                if handled:
                    start = time.time()
                    pp = postprocess(reply, self.persona.speech_style)
                    bubbles = to_bubbles(pp, self.persona.speech_style)
                    # 语音模式：喂给 TTS 管线（否则只上屏不出声）
                    if on_chunk is not None:
                        for b in bubbles:
                            r = on_chunk(b.text)
                            if asyncio.iscoroutine(r):
                                await r
                    return TurnResult(
                        bubbles=bubbles, raw_text=reply,
                        latency_ms=int((time.time() - start) * 1000),
                        degraded=False,
                    )
            except Exception as exc:
                logger.warning("提醒意图解析失败（回落对话）：%s", exc)

        start, now, ctx, session, profile = self._prepare_context(
            user_id, user_text, transcript=transcript, vision=vision)

        # 环境档案（贾维斯化③）：顺带记下提到的地点/物品/家人（永不阻塞）
        if self.env is not None:
            try:
                self.env.observe_chat(user_text)
            except Exception as exc:
                logger.debug("环境档案记录失败（忽略）：%s", exc)

        # ---- 并行：情感分析 + 记忆检索 + 意图路由 ----
        emo_task = asyncio.create_task(self.emotion_analyst.run(ctx))
        mem_task = asyncio.create_task(self.memory_expert.run(ctx))
        emo_res, mem_res = await asyncio.gather(emo_task, mem_task,
                                                return_exceptions=True)
        if isinstance(emo_res, Exception):
            logger.warning("情感分析 Agent 失败：%s", emo_res)
        if isinstance(mem_res, Exception):
            logger.warning("记忆检索 Agent 失败：%s", mem_res)

        # ---- 流式生成（工具标记 → 执行工具 → 携带结果重生成，最多 2 轮）----
        raw_text = ""
        degraded = False
        streamed = False
        for attempt in range(2):
            streamed = False
            try:
                async for delta in self.dialogue_expert.stream(
                        ctx, tool_round=attempt):
                    raw_text += delta
                    streamed = True
                    if on_chunk is not None:
                        r = on_chunk(delta)
                        if asyncio.iscoroutine(r):
                            await r
                break   # 正常完成
            except ToolCallSignal as sig:
                if streamed:
                    # 异常前已有正文流出（理论上不该发生）——不重生成，直出
                    logger.warning("工具标记出现在正文后（忽略）：%s", sig)
                    break
                await self._run_tool(sig, ctx, vision, user_text, on_notice)
                continue
            except Exception as exc:
                logger.warning("流式生成中断：%s", exc)
        if not streamed or not raw_text.strip():
            # 回退非流式
            try:
                gen_res = await self.dialogue_expert.run(ctx, tool_round=1)
                degraded = not gen_res.ok or gen_res.data is None
                raw_text = gen_res.data.text if not degraded \
                    else self._fallback_reply(gen_res.error)
            except ToolCallSignal:
                degraded = True
                raw_text = self._fallback_reply("tool signal in fallback")

        return await self._finalize_turn(
            ctx, session, user_id, user_text, raw_text, degraded, start,
            persist=persist, now=now, profile=profile)

    async def _run_tool(self, sig: ToolCallSignal, ctx, vision_obs,
                        user_text: str, on_notice) -> None:
        """执行 LLM 请求的工具，结果注入 ctx.tool_brief / vision.raw。"""
        tool, query = sig.tool, sig.query
        intent_cn = {"weather": "想查天气", "search": "想查资料",
                     "vision": "想看看眼前"}[tool]
        try:
            print(f"  💭 意图：{intent_cn}"
                  f"（{query[:30]}）" if query else f"  💭 意图：{intent_cn}",
                  flush=True)
        except Exception:
            pass
        notice = {"weather": "等等哈，我看看天气……",
                  "search": "等等哈，我查查……",
                  "vision": "稍等，我看看……"}.get(tool)
        if notice and on_notice is not None:
            try:
                r = on_notice(notice)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass
        try:
            if tool == "weather" and self.weather is not None:
                brief = await asyncio.wait_for(self.weather.fetch(), timeout=6.0)
                if brief:
                    ctx.tool_brief = brief
                    logger.info("天气工具命中：%s", brief)
            elif tool == "search" and self.search is not None:
                brief = await asyncio.wait_for(
                    self.search.fetch(query or user_text), timeout=6.0)
                if brief:
                    ctx.tool_brief = "联网查到的资料（可用可不用）：" + brief
                    logger.info("搜索工具命中：%s", brief[:40])
            elif tool == "vision" and vision_obs is not None:
                # 单次 6s 预算：超时/失败直接降级本地视觉提示，不重试 ——
                # 重试会把首响拖到 12s+，得不偿失（2026-10 实测）
                desc = None
                try:
                    desc = await asyncio.wait_for(
                        self._vlm_describe(vision_obs, query or user_text),
                        timeout=6.0)
                except asyncio.TimeoutError:
                    print("  👁 （云端识别超时，先用本地视觉凑合）", flush=True)
                except Exception as exc:
                    print(f"  👁 （视觉识别异常：{exc}）", flush=True)
                if desc:
                    vision_obs.raw["_vlm"] = desc
                    print(f"  👁 看到的是：{desc}", flush=True)
        except asyncio.TimeoutError:
            logger.info("工具 %s 超时（6s）—— 第二轮将直接以已有知识回答", tool)
        except Exception as exc:
            logger.warning("工具 %s 执行失败：%s", tool, exc)

    # ------------------------------------------------------------------
    # 内部：回合准备 / 收尾
    # ------------------------------------------------------------------

    async def _vlm_describe(self, obs: VisionObservation,
                            question: str) -> str | None:
        """云端多模态识别：把预编码的当前画面发给视觉模型换回描述。

        失败原因打印到控制台（便于排查：画面缺失/接口错误/空响应）。
        """
        b64 = (obs.raw or {}).get("_jpeg")
        if not b64:
            print("  👁 （识别失败：没有可用画面）", flush=True)
            return None
        llm = self.llm
        vlm_model = (self.cfg_snapshot or {}).get("vision", {}).get("vlm_model", "")
        if vlm_model and vlm_model != getattr(llm, "model", ""):
            from pipi.providers.llm.openai_compat import OpenAICompatLLM
            # thinking disabled：混合推理模型（如 DeepSeek V4.1）不关思考会把
            # max_tokens 吃光导致空响应/截断，且白耗 1.5s+；关闭后实测 1.56s
            llm = OpenAICompatLLM(
                base_url=llm.base_url, api_key=llm.api_key,
                model=vlm_model, name="vlm",
                extra_body={"thinking": {"type": "disabled"}},
                timeout_s=getattr(llm, "timeout_s", 30))
        messages = [
            {"role": "system",
             "content": "你是机器人皮皮的眼睛。仔细观察画面，用中文描述："
                        "有什么人/物品、衣着颜色、动作、表情，以及这个人在"
                        "做什么、可能想干什么。100 字以内，不要寒暄。"},
            {"role": "user", "content": [
                {"type": "text",
                 "text": question or "描述你现在看到的画面"},
                {"type": "image_url", "image_url": {"url": b64}},
            ]},
        ]
        try:
            resp = await llm.chat(messages, temperature=0.3, max_tokens=512)
        except Exception as exc:
            print(f"  👁 （视觉识别请求失败：{exc}）", flush=True)
            return None
        text = (getattr(resp, "text", "") or "").strip()
        if not text:
            print("  👁 （视觉模型返回空）", flush=True)
            return None
        return text

    def _prepare_context(self, user_id: str, user_text: str, *,
                         transcript: Any | None = None,
                         vision: VisionObservation | None = None):
        """回合准备：用户/档案/状态/会话 + 冷启动时间流逝。"""
        start = time.time()
        now = self.clock.now()

        self.user_repo.ensure(user_id, now)
        profile = self.get_profile(user_id)
        state = self.get_state(user_id)
        session = self.get_session(user_id)

        # 冷启动：时间流逝处理（好感度衰减）
        if state.last_interaction and now > state.last_interaction:
            hours = (now - state.last_interaction) / 3600.0
            if hours >= 0.5:   # 半小时以上才算"分开过"
                self.emotion_analyst.engine.tick_idle(state, now)
            # 久别重逢（超过一天）—— 好感度回来，气也消了
            if hours >= 24:
                state.apply_by_key("warm_return")
            elif hours >= 0.5:
                state.apply_by_key("casual_warmth")

        # PULSE 此刻真实内心状态（无 provider 时为空，兼容 legacy）
        inner = ""
        if self.inner_state_provider is not None:
            try:
                inner = self.inner_state_provider() or ""
            except Exception as exc:
                logger.debug("内心状态获取失败：%s", exc)

        ctx = AgentContext(
            user_id=user_id,
            user_text=user_text,
            now=now,
            profile=profile,
            emotion_state=state,
            persona=self.persona,
            inner_brief=inner,
            transcript=transcript,
            vision=vision,
            history=session.context_messages(),
            turn_id=None,
            trace_id=session.trace_id,
        )
        return start, now, ctx, session, profile

    async def _finalize_turn(self, ctx: AgentContext, session: Session,
                             user_id: str, user_text: str, raw_text: str,
                             degraded: bool, start: float, *,
                             persist: bool, now: float,
                             profile: UserProfile) -> TurnResult:
        """回合收尾：后处理/气泡/动作/会话记录/异步持久化。"""
        raw_text = self._strip_reasoning(raw_text)
        pp: PostProcessResult = postprocess(raw_text, self.persona.speech_style)
        voice_emotion = "neutral"
        if self.enable_proactive_emotion and ctx.emotion_state:
            voice_emotion = self.emotion_analyst.engine.mood_to_voice_emotion(ctx.emotion_state)
        bubbles = to_bubbles(pp, self.persona.speech_style, emotion=voice_emotion)

        # ---- 从回复里提取肢体动作（部署到机器人时生效）----
        if self.motion and ctx.emotion_state:
            gesture = self.emotion_analyst.engine.mood_to_gesture(ctx.emotion_state)
            if gesture:
                await self._safe_motion(gesture)

        # ---- 记录会话 ----
        session.add(Role.USER, user_text)
        for b in bubbles:
            session.add(Role.ASSISTANT, b.text)

        result = TurnResult(
            bubbles=bubbles,
            raw_text=raw_text,
            emotion_state=ctx.emotion_state,
            memories_used=[m.item.content for m in ctx.memories],
            latency_ms=int((time.time() - start) * 1000),
            degraded=degraded,
        )

        # ---- 异步：记忆写入 + 持久化 ----
        if persist:
            task = asyncio.create_task(self._post_turn(
                user_id, user_text, pp.text, ctx, profile, now
            ))
            # 让调用方能拿到写入结果（脚本/测试里很需要；不阻塞主流程）
            task.add_done_callback(
                lambda t: result.memories_written.extend(
                    getattr(t, "_written", []) or [])
            )

        return result

    # ------------------------------------------------------------------
    # 回合后处理（异步）
    # ------------------------------------------------------------------

    async def _post_turn(
        self,
        user_id: str,
        user_text: str,
        reply_text: str,
        ctx: AgentContext,
        profile: UserProfile,
        now: float,
    ) -> None:
        """记忆抽取、档案更新、状态落库。"""
        written: list[str] = []
        try:
            # 1. 记忆抽取
            intensity = ctx.emotion_state.intensity if ctx.emotion_state else 0.0
            extracted = await self.memory.extract(
                user_text, reply_text, profile, emotion_intensity=intensity
            )
            if extracted.candidates:
                ids = await self.memory.commit(user_id, extracted.candidates, now)
                written = list(ids or [])
                # 2. 档案更新
                changed = self.memory.apply_profile_updates(profile, extracted.profile_updates)
                if changed:
                    self.profile_repo.save(profile, now)
                    if not profile.is_owner and profile.nickname:
                        profile.is_owner = True
                        self.user_repo.update(user_id, is_owner=1)
                        # 成长系统 M1：认主（A4）
                        if self.growth is not None:
                            try:
                                await self.growth.record_milestone(user_id, "M1", now)
                            except Exception as exc:
                                logger.warning("M1 记录失败：%s", exc)
        except Exception as exc:
            logger.error("记忆写入失败：%s", exc)

        try:
            # 3. 情绪状态落库
            if ctx.emotion_state:
                self.emotion_repo.save(user_id, ctx.emotion_state, now,
                                       trigger="turn")
            # 4. 对话落库
            session = self.get_session(user_id)
            for turn in session.turns[-3:]:
                if turn.created_at and not getattr(turn, "_persisted", False):
                    self.turn_repo.save(turn)
                    turn._persisted = True  # type: ignore[attr-defined]
            # 5. 用户统计
            self.user_repo.update(
                user_id,
                last_seen=now,
                total_turns=(self.user_repo.get(user_id) or {}).get("total_turns", 0) + 1,
            )
        except Exception as exc:
            logger.error("状态落库失败：%s", exc)

        # 把写入结果挂在当前 task 上，供 add_done_callback 读取
        try:
            asyncio.current_task()._written = written  # type: ignore[attr-defined]
        except Exception:
            pass

        # 6. 成长引擎（单向写入：异常已在其内部吞掉）
        if self.growth is not None:
            await self.growth.on_turn(
                user_id, user_text, ctx.emotion_state, now=now)

    # ------------------------------------------------------------------
    # 主动搭话
    # ------------------------------------------------------------------

    async def handle_proactive(
        self,
        user_id: str,
        event: str,
        *,
        vision: VisionObservation | None = None,
    ) -> TurnResult:
        """生成主动搭话。"""
        start = time.time()
        now = self.clock.now()
        self.user_repo.ensure(user_id, now)

        profile = self.get_profile(user_id)
        state = self.get_state(user_id)
        session = self.get_session(user_id)

        # 主动搭话也会更新情绪（如回家 → 开心）
        ctx = AgentContext(
            user_id=user_id, user_text="", now=now,
            profile=profile, emotion_state=state, persona=self.persona,
            vision=vision, trace_id=session.trace_id,
        )

        # 主动搭话也读此刻内心（统一说话：语气与内心一致）
        inner = ""
        if self.inner_state_provider is not None:
            try:
                inner = self.inner_state_provider() or ""
            except Exception:
                inner = ""
        messages = self.dialogue_expert.pb.build_proactive(
            event=event,
            current_time=time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
            profile=profile,
            memories=[],
            emotion_state=state,
            vision=vision,
            has_vision=vision is not None,
            inner_state=inner,
        )

        try:
            resp = await self.llm.chat(
                messages,
                temperature=self.persona.temperature,
                max_tokens=int(self.persona.max_tokens * 0.6),
                use_cheap=True,
            )
            raw = resp.text
        except Exception as exc:
            logger.error("主动搭话生成失败：%s", exc)
            raw = self._fallback_proactive(event)

        pp = postprocess(raw, self.persona.speech_style)
        bubbles = to_bubbles(pp, self.persona.speech_style)

        session.add(Role.SYSTEM_EVENT, f"[主动] {event}")
        for b in bubbles:
            session.add(Role.ASSISTANT, b.text)

        self.emotion_repo.save(user_id, state, now, trigger=f"proactive:{event}")

        return TurnResult(
            bubbles=bubbles, raw_text=raw, emotion_state=state,
            latency_ms=int((time.time() - start) * 1000),
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    _REASONING_MARKERS = ("首先", "其次", "比如", "按照", "性格来回应",
                          "我应该", "要符合", "既", "分析", "思路")

    def _strip_reasoning(self, text: str) -> str:
        """思考腔泄漏防护：模型偶发把解题过程当答案输出（尤其 lite 档）。

        特征：多段落 + 推理特征词。命中时取最后一个自然段——
        那通常才是真正的台词。
        """
        if "\n" not in text:
            return text
        hits = sum(1 for k in self._REASONING_MARKERS if k in text)
        if hits >= 2 or len(text) > 400:
            paras = [p.strip() for p in text.split("\n") if p.strip()]
            if len(paras) > 1:
                logger.warning("检测到思考腔泄漏（%d 特征），截取末段", hits)
                return paras[-1]
        return text

    def _fallback_reply(self, error: str) -> str:
        """LLM 失败时的兜底话术 —— 保证皮皮不沉默。"""
        logger.warning("走兜底回复（原因：%s）", error)
        return "……我刚走神了。你再说一遍？"

    def _fallback_proactive(self, event: str) -> str:
        return "喂……你在吗"

    async def _safe_motion(self, gesture: str) -> None:
        try:
            await self.motion.execute_gesture(gesture)
        except Exception as exc:
            logger.debug("动作执行失败：%s", exc)

    # ------------------------------------------------------------------
    # 语音输出
    # ------------------------------------------------------------------

    async def speak(self, bubbles: list[Bubble]) -> None:
        """把气泡转成语音播放（有 TTS 时）。"""
        if not self.tts:
            return
        for b in bubbles:
            try:
                await self.tts.say(b.text, emotion=b.emotion)
            except Exception as exc:
                logger.warning("TTS 失败：%s", exc)

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    def stats(self, user_id: str) -> dict[str, Any]:
        state = self.get_state(user_id)
        return {
            "mood": state.mood.value,
            "intensity": round(state.intensity, 2),
            "affection": round(state.affection, 1),
            "affection_level": state.affection_level,
            "companion_need": round(state.companion_need, 2),
            "session_turns": len(self.get_session(user_id).turns),
        }
