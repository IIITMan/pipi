"""生命模块 · 引擎（后台任务编排 + 念头生成 + 对外接入）。

与现有会话的接入方式（以现有为准，详见 docs/LIFE_MODULE_NOTES.md）：
  - 生理/注意力/念头/调度以 asyncio 协程任务运行（§8 的"线程"职责等价）；
  - 只在会话空闲（IDLE 且无收集段）时执行发声念头，经
    session.try_speak_spontaneous() 走现有 TTS + Barge-in 通道；
  - 用户人声/打断等事件由会话反向注入（observe_user_voice / on_barge_in）。

后台任务（§8）：
  生理更新 1Hz · 念头生成 3Hz · 行为调度 2Hz（记忆摘要复用现有
  memory_manager 异步抽取，不再单独建线程）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pipi.life.behavior_scheduler import BehaviorScheduler
from pipi.life.state_machine import (
    AttentionMode,
    AttentionSystem,
    LifeState,
    Personality,
    Physiology,
)
from pipi.life.thought_queue import Thought, ThoughtQueue

logger = logging.getLogger(__name__)


@dataclass
class InnerEvent:
    """内心时间线事件（C1）：思想流的一格，带因果来源。"""

    t: float
    kind: str                  # thought/vocal/inner/discard/chain/action
    text: str
    cause: str = ""            # 触发来源：social/patrol/memory/chain/...


class LifeEngine:
    """生命感内核：生理状态、注意力、念头生成与行为调度。"""

    def __init__(
        self,
        cfg: dict,
        llm: Any,
        persona_name: str,
        persona_hint: str,
        memory_getter: Callable[[], str | None],
        session: Any,
    ):
        life_cfg = cfg.get("life", {}) or {}
        self.cfg = life_cfg
        self.llm = llm
        self.name = persona_name
        self.persona_hint = persona_hint
        self.memory_getter = memory_getter
        self.story_getter: Callable[[], str | None] | None = None  # C4：story 记忆优先
        self.session = session

        pers_cfg = life_cfg.get("personality", {}) or {}
        self.personality = Personality(
            extraversion=float(pers_cfg.get("extraversion", 6.0)),
            curiosity_weight=float(pers_cfg.get("curiosity_weight", 6.0)),
            sensitivity=float(pers_cfg.get("sensitivity", 5.0)),
            like=list(pers_cfg.get("like", [])),
            dislike=list(pers_cfg.get("dislike", [])),
        )
        self.physio = Physiology()
        self.attention = AttentionSystem(
            sleep_energy=life_cfg.get("sleep_energy", 2.5),
            wandering_after_s=life_cfg.get("wandering_enter_s", 45.0),
        )
        self.queue = ThoughtQueue(ttl_s=life_cfg.get("thought_ttl_s", 5.0))

        from pipi.life.action_adapter import create_action_adapter
        self.action = create_action_adapter(life_cfg.get("action_backend", "mock"))
        self.scheduler = BehaviorScheduler(
            {k: life_cfg.get(k) for k in
             ("vocal_cooldown_s", "wandering_cooldown_s", "energy_min_vocal",
              "idle_motion_interval_s")},
            self.action,
        )

        self.physio_hz = float(life_cfg.get("physio_hz", 1.0))
        self.thought_hz = float(life_cfg.get("thought_hz", 3.0))
        self.scheduler_hz = float(life_cfg.get("scheduler_hz", 2.0))
        # 想找人说话的触发阈值（越高越稀有；巡逻/探索等本体行为不受此限）
        self.social_vocal_threshold = float(
            life_cfg.get("social_vocal_threshold", 7.5))

        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._last_thought_desc = ""
        # 当前活动（监控页「正在做什么」）：动作描述 + 结束时刻
        self._activity_desc: str | None = None
        self._activity_until: float = 0.0
        # 最近念头（心里想着什么，准备干嘛）
        self._last_intent: str = ""
        # 联想链（机制 1）：内心时间线 + 触发冷却
        self.inner_stream: deque[InnerEvent] = deque(maxlen=12)
        self._chain_last = 0.0
        self._chain_task: asyncio.Task | None = None
        chain_cfg = life_cfg.get("chain", {}) or {}
        self._chain_enabled = bool(chain_cfg.get("enabled", True))
        self._chain_prob = float(chain_cfg.get("chain_prob", 0.01))
        self._chain_cooldown = float(chain_cfg.get("chain_cooldown_s", 90.0))
        self._chain_min_energy = float(chain_cfg.get("min_energy", 3.0))
        self._chain_vocal_prob = float(chain_cfg.get("vocal_prob", 0.3))
        self._chain_timeout = float(chain_cfg.get("timeout_s", 12.0))
        # 监控事件日志（心理记录）：kind ∈ thought/vocal/discard/cooldown/
        # fatigue_mute/action/user_voice/barge_in/response_done/farewell/whimper
        self.log: deque = deque(maxlen=300)
        self._last_whimper = 0.0
        # 监控上报：事件/快照/聊天经 sink 推给监控端（窗口端注入 WS 推送）
        self._sink: Callable[[dict], None] | None = None
        self._outbox: deque = deque(maxlen=100)
        self._chat_outbox: deque = deque(maxlen=60)

    def set_sink(self, fn: Callable[[dict], None]) -> None:
        """注入监控数据出口（窗口端注入 WS 广播函数）。"""
        self._sink = fn

    def _think_event(self, kind: str, text: str, cause: str = "") -> None:
        """C1：内心时间线留痕（思想流，供联想链承接上文）。"""
        self.inner_stream.append(
            InnerEvent(t=time.time(), kind=kind, text=text, cause=cause))

    def _log(self, kind: str, text: str) -> None:
        entry = {"t": round(time.time(), 3), "kind": kind, "text": text}
        self.log.append(entry)
        if self._sink is not None:
            self._outbox.append(entry)

    def log_chat(self, role: str, text: str) -> None:
        """会话聊天记录上报（语音会话的对话内容）。"""
        entry = {"t": round(time.time(), 3), "role": role, "text": text}
        if self._sink is not None:
            self._chat_outbox.append(entry)

    # 动作 → 监控页可读描述
    ACTIVITY_DESC = {
        "patrol_route": "正在巡逻（沿房间边缘巡视一圈）",
        "look_around": "正在四处张望",
        "wander_turn": "正在转身探索",
        "slow_forward": "正在小步走动",
        "go_home": "正在返回待命点",
    }

    def _start_activity(self, action_type: str, duration: float) -> None:
        """动作开始 → 记录「正在做什么」（到期自动清空）。"""
        desc = self.ACTIVITY_DESC.get(action_type)
        if desc:
            self._activity_desc = desc
            self._activity_until = time.monotonic() + duration

    def snapshot(self) -> dict:
        """给 Web 监控页的状态快照。"""
        p = self.physio
        now = time.monotonic()
        attention = self.attention.mode
        if self._activity_desc and now < self._activity_until:
            act_now = (f"{self._activity_desc}"
                       f"（还剩 {self._activity_until - now:.0f}s）")
        elif attention == AttentionMode.REST:
            act_now = "打盹中"
        elif attention == AttentionMode.WANDERING:
            act_now = "发呆走神中"
        else:
            act_now = "安静待机，竖着耳朵听动静"
        return {
            "state": self._life_state().value,
            "attention": attention.value,
            "physio": {
                "mood": round(p.mood, 2),
                "energy": round(p.energy, 2),
                "social": round(p.social, 2),
                "mood_value": p.mood_value,
                "summary": p.describe(),
            },
            "activity": {"now": act_now, "intent": self._last_intent},
            "queue": self.queue.size(),
            "last_thought": self._last_thought_desc,
            "action_backend": self.action.name,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._physio_loop(), name="life-physio"),
            asyncio.create_task(self._thought_loop(), name="life-thought"),
            asyncio.create_task(self._schedule_loop(), name="life-scheduler"),
            asyncio.create_task(self._monitor_flush_loop(), name="life-monitor"),
        ]
        logger.info("生命模块启动（action=%s）", self.action.name)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------
    # 会话事件注入（由 InteractionSession 调用）
    # ------------------------------------------------------------------

    def observe_user_voice(self) -> None:
        """会话逐帧调用，内部 1s 节流防 alert 爆表。"""
        now = time.monotonic()
        if now - getattr(self, "_last_voice_event", 0.0) < 1.0:
            return
        self._last_voice_event = now
        self.physio.on_user_voice()
        self._log("user_voice", "检测到主人说话")

    def on_barge_in(self) -> None:
        self.physio.on_barge_in(self.personality.sensitivity)
        self.scheduler.action.cancel_all()
        self._log("barge_in", "被主人打断")

    def on_response_done(self) -> None:
        self.physio.on_response_done()
        self._log("response_done", "一轮应答完成")

    def on_farewell(self) -> None:
        """退出告别播完：计入念叨冷却并清空未出口的念头。"""
        self.scheduler.mark_vocal()
        self.queue.clear()
        self._log("farewell", "对话结束，回待机")

    # ------------------------------------------------------------------
    # 后台任务
    # ------------------------------------------------------------------

    async def _physio_loop(self) -> None:
        """1Hz 生理更新。任何单次异常都不允许终止循环（生命不能静默停止）。"""
        dt = 1.0 / max(self.physio_hz, 0.1)
        while self._running:
            try:
                self.physio.tick(dt, self._life_state(), self.attention.mode)
                self.attention.update(self._life_state(), self.physio.energy,
                                      self._recent_user_voice())
            except Exception as exc:
                logger.warning("生理更新异常（循环继续）：%s", exc)
            await asyncio.sleep(dt)

    async def _thought_loop(self) -> None:
        """3Hz 念头生成：评估需求 → 生成念头入队（大量仅内心/过期丢弃）。"""
        dt = 1.0 / max(self.thought_hz, 0.1)
        while self._running:
            await asyncio.sleep(dt)
            if self._life_state() != LifeState.IDLE:
                continue
            try:
                self._maybe_think()
            except Exception as exc:
                logger.debug("念头生成异常：%s", exc)

    async def _schedule_loop(self) -> None:
        """2Hz 行为调度：扫描念头队列，选择执行/丢弃。"""
        dt = 1.0 / max(self.scheduler_hz, 0.1)
        while self._running:
            await asyncio.sleep(dt)
            try:
                await self._schedule_once()
            except Exception as exc:
                logger.warning("行为调度异常：%s", exc)

    async def _monitor_flush_loop(self) -> None:
        """每秒心跳式把（快照+增量事件）交给 sink；无 sink 时空转。

        无论有无新事件都推快照——保证监控页数据始终新鲜。
        """
        while self._running:
            await asyncio.sleep(1.0)
            if self._sink is None:
                continue
            payload = {
                "events": list(self._outbox),
                "chat": list(self._chat_outbox),
                "snapshot": self.snapshot(),
            }
            self._outbox.clear()
            self._chat_outbox.clear()
            try:
                self._sink(payload)
            except Exception as exc:
                logger.debug("监控上报失败：%s", exc)

    # ------------------------------------------------------------------
    # 念头生成（规则驱动；LLM 只在发声念头执行时渲染文本）
    # ------------------------------------------------------------------

    def _maybe_think(self) -> None:
        """念头生成：机器人本体行为为主（巡逻/探索/休息），社交发声为辅。

        自发念头的大头是「像机器人一样有事做」——巡视、到处看看、
        找个地方歇着；「想找人聊天」是稀有事件（阈值高、概率低、冷却长）。
        """
        p = self.physio
        wandering = self.attention.mode == AttentionMode.WANDERING

        # ---- 机器人行为念头（不发声，只动身体）----

        # 巡逻：精力充沛且发呆了一阵 → 出去转一圈看家
        if p.energy > 5.5 and wandering and random.random() < 0.035:
            self._push("巡视领地时间到……", "patrol", "patrol",
                       priority=2, vocal=False, motion="patrol_route",
                       motion_duration=8.0)
        # 到处看看：被什么东西吸引了
        elif wandering and random.random() < 0.03:
            self._push("那边好像有什么，去看看……", "explore", "explore",
                       priority=1, vocal=False, motion="look_around",
                       motion_duration=2.5)
        # 困了：找个角落歇着（动作上即原地静止，眼睛闭上）
        if p.energy < 3.0 and random.random() < 0.02:
            self._push("好困……眯一会儿。", "rest", "rest",
                       priority=1, vocal=False)

        # ---- 社交念头（稀有事件）----

        # 想陪：安静很久了才想找主人（阈值可配，再叠加发声冷却）
        if p.social > self.social_vocal_threshold and random.random() < 0.02:
            self._push("想找他说说话……", "ask_owner", "social",
                       priority=3, vocal=True)

        # 记忆闪回（idle + wandering，§5）
        if wandering and random.random() < 0.02:
            memory = self.memory_getter()
            if memory:
                self._push(f"想起：{memory}", "memory_flashback", "memory",
                           priority=3, vocal=random.random() < 0.5)

        # 联想链（机制 1）：承接上文续想
        self._maybe_chain()

    # ------------------------------------------------------------------
    # C3：联想链 —— 承接上文 + 一条记忆的续想（§触发决策流）
    # ------------------------------------------------------------------

    def _maybe_chain(self) -> None:
        """同步触发决策：门控全过才概率触发；命中后异步渲染（不阻塞）。"""
        if not self._chain_enabled:
            return
        if self._life_state() != LifeState.IDLE:
            return
        if self.physio.energy < self._chain_min_energy:      # 犯困不触发
            return
        if self.queue.size() > 0:                            # 队列非空不插队
            return
        if self._chain_task is not None and not self._chain_task.done():
            return                                           # 上一条联想还没完成
        now = time.monotonic()
        if now - self._chain_last < self._chain_cooldown:
            return
        if random.random() >= self._chain_prob:
            return
        self._chain_last = now
        self._chain_task = asyncio.create_task(self._run_chain())

    async def _run_chain(self) -> None:
        """异步联想：组装上下文 → LLM 续想 → 校验 → 入队（失败静默）。"""
        try:
            from pipi.prompts import render

            # 上文：最近 2 条内心事件；记忆：优先 story（C4），回落普通记忆
            recent = [e.text for e in list(self.inner_stream)[-2:]]
            memory = None
            if self.story_getter is not None:
                try:
                    memory = self.story_getter()
                except Exception:
                    memory = None
            if not memory:
                memory = self.memory_getter()
            if not recent or not memory:
                return

            state_desc = self.physio.describe()
            raw = await asyncio.wait_for(
                self.llm.complete(
                    render(
                        "chain_thought.jinja",
                        name=self.name,
                        persona_hint=self.persona_hint,
                        state_desc=state_desc,
                        recent=recent,
                        memory=memory,
                    ),
                    use_cheap=True, max_tokens=60, temperature=1.0,
                ),
                timeout=self._chain_timeout,
            )
            text = (raw or "").strip().strip('"「」')
            if not text or len(text) > 24:
                return
            # 防重复：与最近思想流前缀重叠 → 丢弃（避免原地打转）
            for e in self.inner_stream:
                if e.text[:8] and (e.text[:8] in text or text[:8] in e.text):
                    return
            vocal = random.random() < self._chain_vocal_prob
            self._push(text, "chain", "chain", priority=3, vocal=vocal)
            self._think_event("chain", text, cause="chain")
        except asyncio.TimeoutError:
            logger.debug("联想链渲染超时（静默丢弃）")
        except Exception as exc:
            logger.debug("联想链失败（静默）：%s", exc)

    def _push(self, content: str, ttype: str, source: str, *,
              priority: int = 2, vocal: bool = False,
              motion: str | None = None,
              motion_duration: float = 1.5) -> None:
        motion_enable = motion is not None or random.random() < 0.3
        self._last_intent = content
        self.queue.push(Thought(
            content=content, priority=priority, type=ttype, source=source,
            vocal_enable=vocal, motion_enable=motion_enable, motion=motion,
            motion_duration=motion_duration,
        ))
        logger.debug("念头入队：%s (vocal=%s)", content, vocal)

    # ------------------------------------------------------------------
    # 行为执行
    # ------------------------------------------------------------------

    async def _schedule_once(self) -> None:
        self.queue.purge()
        life_state = self._life_state()
        busy = getattr(self.session, "is_busy", False)

        def _on_event(kind: str, text: str) -> None:
            self._log(kind, text)
            self._think_event(kind, text, cause="gate")

        thought = self.scheduler.scan(
            life_state, self.attention.mode, self.physio,
            self.personality, self.queue, busy, on_event=_on_event,
        )

        # 无念头时的待机微动作（呼吸灯/摆头/摇尾巴……"小狗感"来源）
        if thought is None and not busy:
            act = self.scheduler.idle_motion(self.physio, self.attention.mode)
            if act:
                self._log("action", f"待机微动作：{act}")
                self._start_activity(act, 1.5)
            self._maybe_whimper()
            return
        if thought is None:
            return

        self._log("thought", thought.content)
        self._think_event("thought", thought.content, cause=thought.source)
        act_type = self.scheduler.paired_motion(thought, self.attention.mode)
        if act_type:
            self._start_activity(act_type, thought.motion_duration)

        if not thought.vocal_enable:
            logger.debug("念头仅内心/动作，不发声：%s", thought.content)
            self._log("inner", thought.content)
            return

        await self._speak_thought(thought)

    async def _speak_thought(self, thought: Thought) -> None:
        """渲染念头文本并经会话空闲通道发声（可被 Barge-in 打断）。"""
        text = await self._render_thought(thought)
        if not text:
            return
        # 念头同样会被念出来：过滤舞台指示（（歪头）之类）
        from pipi.capabilities.expression import strip_stage_directions

        text = strip_stage_directions(text)
        if not text:
            return
        self.scheduler.mark_vocal()
        ok = self.session.try_speak_spontaneous(text)
        self._log("vocal" if ok else "muted", text)
        self._think_event("vocal" if ok else "muted", text,
                          cause=thought.source)
        if not ok:
            logger.debug("会话忙，念头转内心：%s", text)
        self._last_thought_desc = text

    def _maybe_whimper(self) -> None:
        """特别想陪时哼唧一声（冷却 180s，不占发声大冷却，不配动作）。"""
        now = time.monotonic()
        if now - self._last_whimper < 180.0:
            return
        p = self.physio
        if p.social > 8.5 and random.random() < 0.08:
            text = "呜……好想有人陪我玩……"
        else:
            return
        self._last_whimper = now
        ok = self.session.try_speak_spontaneous(text)
        self._log("vocal" if ok else "muted", f"[哼唧] {text}")

    async def _render_thought(self, thought: Thought) -> str:
        """LLM 渲染念头为自然口语；失败回退模板句。"""
        try:
            from pipi.prompts import render

            recent = [e["text"] for e in self.log
                      if e["kind"] == "vocal" and not e["text"].startswith("[哼唧]")][-3:]
            raw = await asyncio.wait_for(
                self.llm.complete(
                    render(
                        "thought.jinja",
                        name=self.name,
                        persona_hint=self.persona_hint,
                        state_desc=self.physio.describe(),
                        memory_flashback=(thought.content
                                          if thought.type == "memory_flashback"
                                          else ""),
                        need_hint=self._need_hint(thought.source),
                        recent=recent,
                    ),
                    use_cheap=True, max_tokens=80, temperature=1.0,
                ),
                timeout=12.0,
            )
            text = (raw or "").strip().strip('"「」')
            # 防重复：与最近念头过于相似则丢弃（转内心）
            if text and any(text[:12] in r or r[:12] in text for r in recent):
                logger.debug("念头与最近重复，转内心：%s", text)
                return ""
            if text and len(text) <= 60:
                return text
        except Exception as exc:
            logger.debug("念头 LLM 渲染失败，用模板：%s", exc)

        # 模板兜底
        if thought.type == "memory_flashback":
            return f"突然想起来……{thought.content[3:]}，嘿嘿。"
        if thought.source == "social":
            return random.choice(["你还在忙吗？", "有点无聊了……", "喂，在吗？"])
        return random.choice(["嗯……", "（小声哼歌）", "今天会发生什么呢……"])

    def _need_hint(self, source: str) -> str:
        return {
            "social": "想主人了，想找人说话",
            "memory": "一段旧回忆",
        }.get(source, "说不上来的冲动")

    # ------------------------------------------------------------------

    def _life_state(self) -> LifeState:
        """现有会话状态 → 生命模块状态（映射见模块 docstring）。"""
        s = getattr(self.session, "state", None)
        if s is None:
            return LifeState.IDLE
        from pipi.interfaces.session import InteractionSession

        if s == InteractionSession.RESPOND:
            return LifeState.MUTTERING
        if s == InteractionSession.LISTEN:
            return LifeState.CHATTING
        return LifeState.IDLE

    def _recent_user_voice(self) -> bool:
        """最近 5s 内是否听到主人说话（防御式读取，异常按「没听到」处理）。"""
        try:
            return self.physio.seconds_since_user_voice() < 5.0
        except AttributeError:
            return False
