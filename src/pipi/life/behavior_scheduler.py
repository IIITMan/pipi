"""生命模块 · 行为调度器（§4.3 / §8，2Hz 扫描念头队列）。"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable

from pipi.life.action_adapter import ActionAdapter, ActionIntent
from pipi.life.state_machine import AttentionMode, LifeState, Personality, Physiology
from pipi.life.thought_queue import Thought, ThoughtQueue

logger = logging.getLogger(__name__)

# 预定义动作（§7.2；待机微动作大幅精简：待机时的"活感"主要由
# Web/屏幕上的眼睛表情表达，物理动作只留极低频的眨眼示意）
MICRO_MOTIONS = ["blink"]
IDLE_MOTIONS = ["look_around"]
STARTLE_MOTIONS = ["freeze"]


class BehaviorScheduler:
    """扫描念头队列，选择执行行为（语音 / 动作 / 丢弃）。

    约束（§9）：
      - 发声念头全局冷却 18s，wandering 模式 30s
      - 念头 TTL 5s，过期即丢（ThoughtQueue 负责）
      - energy < 2.5：禁止发声念头（没力气说话）
      - 同一时间最多执行 1 个念头
    """

    def __init__(self, cfg: dict, action: ActionAdapter):
        self.action = action
        self.vocal_cooldown_s = cfg.get("vocal_cooldown_s", 18.0)
        self.wandering_cooldown_s = cfg.get("wandering_cooldown_s", 30.0)
        self.energy_min_vocal = cfg.get("energy_min_vocal", 2.5)
        self.idle_motion_interval_s = cfg.get("idle_motion_interval_s", 30.0)
        self._last_vocal = 0.0
        self._last_idle_motion = 0.0
        self._executing = False

    # ------------------------------------------------------------------

    def cooldown_for(self, attention: AttentionMode) -> float:
        if attention == AttentionMode.WANDERING:
            return self.wandering_cooldown_s
        return self.vocal_cooldown_s

    def scan(self, life_state: LifeState, attention: AttentionMode,
             physio: Physiology, personality: Personality,
             queue: ThoughtQueue, session_busy: bool,
             on_event: Callable[[str, str], None] | None = None) -> Thought | None:
        """2Hz 调度：返回应当执行的念头（调用方负责执行），无则 None。

        规则：
          - 会话忙（对话/机器人说话/用户说话中）→ 不执行念头（§4.3 不抢话）
          - 冷却期内 → 不执行发声念头
          - energy < 下限 → 发声念头降级为内心
        on_event(kind, text)：被丢弃/降级的念头回传给监控记录。
        """
        now = time.monotonic()
        if session_busy or self._executing or life_state != LifeState.IDLE:
            return None

        thought = queue.pop_best()
        if thought is None:
            return None

        # 冷却检查（仅发声念头）
        cd = self.cooldown_for(attention)
        if thought.vocal_enable and now - self._last_vocal < cd:
            if on_event:
                on_event("cooldown", thought.content)
            return None

        # 疲劳保护（§9.3）：没力气说话时降级为内心
        if physio.energy < self.energy_min_vocal and thought.vocal_enable:
            thought.vocal_enable = False
            thought.type = "motion_only"
            if on_event:
                on_event("fatigue_mute", thought.content)

        # 好奇/外向微调发声概率（低优先级念头有概率被丢弃）
        if thought.priority <= 2 and thought.vocal_enable:
            keep_p = 0.35 + personality.extraversion / 25.0
            if random.random() > keep_p:
                if on_event:
                    on_event("discard", thought.content)
                return None

        return thought

    def mark_vocal(self, now: float | None = None) -> None:
        self._last_vocal = now if now is not None else time.monotonic()

    def paired_motion(self, thought: Thought,
                      attention: AttentionMode) -> str | None:
        """念头配套动作（§4.1 motion_enable / §7.4 组合输出）。

        返回实际下发的动作类型（无动作返回 None），供调用方记录活动。
        """
        if not thought.motion_enable:
            return None
        action_type = thought.motion or random.choice(
            MICRO_MOTIONS if attention != AttentionMode.WANDERING else IDLE_MOTIONS)
        self.action.perform(ActionIntent(
            action_type=action_type,
            priority=2,
            duration=thought.motion_duration,
            source="thought",
        ))
        return action_type

    def idle_motion(self, physio: Physiology, attention: AttentionMode) -> str | None:
        """待机微动作：默认关闭物理动作（活感由眼睛表情表达）。

        仅在 wandering 发呆时，极低频地看一眼四周（最小间隔可配）。
        """
        if attention != AttentionMode.WANDERING:
            return None
        now = time.monotonic()
        if now - self._last_idle_motion < self.idle_motion_interval_s:
            return None
        if random.random() < 0.15:
            self._last_idle_motion = now
            self.action.perform(ActionIntent(
                action_type="look_around",
                priority=1,
                duration=1.5,
                source="instinct",
            ))
            return "look_around"
        return None

    def startle(self) -> None:
        """§3 distracted 受惊：冻结 + 高优先级应激动作。"""
        self.action.perform(ActionIntent(
            action_type="freeze", priority=5, duration=0.5, source="instinct"))
        self.action.perform(ActionIntent(
            action_type="alert_pose", priority=5, duration=2.0, source="instinct"))
