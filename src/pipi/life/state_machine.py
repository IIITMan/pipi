"""生命模块 · 生理状态机与全局状态。

依据《生命模块 v2.1 设计文档》实现，与现有架构冲突处以现有为准：
  - 文档要求"后台多线程"，现有工程为 asyncio 单事件循环 → 用 asyncio 任务实现
    （职责/周期与文档 §8 一致，线程换成协程任务）；
  - 会话状态机的所有权保留在 InteractionSession（现有方案），
    生命模块只做生理/注意力/念头，通过事件注入与空闲询问接入。

全局状态（§3）：sleep / idle / chatting / muttering / distracted。
注意（与现有映射，详见 docs/LIFE_MODULE_NOTES.md）：
  现有 session.IDLE（KWS+VAD 监听）↔ life.idle（不单独设 sleep 态）；
  现有 session.LISTEN / RESPOND ↔ life.chatting / muttering（机器人说话）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class LifeState(str, Enum):
    SLEEP = "sleep"
    IDLE = "idle"
    CHATTING = "chatting"
    MUTTERING = "muttering"
    DISTRACTED = "distracted"


class AttentionMode(str, Enum):
    FOCUS_ON_OWNER = "focus_on_owner"
    WANDERING = "wandering"
    REST = "rest"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class Physiology:
    """内在状态：只保留 3 个可解释的轴，全部由真实事件驱动。

    mood   -5~+5  心情：被打打断变差、聊得开心变好，随时间回归平静
    energy  0~10  精力：说话消耗、安静恢复（低精力会犯困、不爱说话）
    social  0~10  想陪：安静越久越想找主人，聊天即满足
    （旧模型 8 个独立数值互不耦合、速率拍脑袋，观感像假仪表盘，已废弃）
    """

    mood: float = 0.5
    energy: float = 8.0
    social: float = 1.0
    mood_value: str = "平静"       # 最近一轮对话的原始情绪（开心/委屈/生气…）
    _mood_value_t: float = 0.0     # 情绪值设置时刻（超时回落平静）

    # 内部
    _last_user_voice: float = field(default_factory=time.monotonic)
    _quiet_since: float = field(default_factory=time.monotonic)

    # ------------------------------------------------------------------

    def tick(self, dt: float, life_state: LifeState,
             attention: AttentionMode) -> None:
        """1Hz 更新：变化全部来自「正在发生什么」，不凭空涨落。"""
        chatting = life_state in (LifeState.CHATTING, LifeState.MUTTERING)
        since_voice = time.monotonic() - self._last_user_voice

        # 精力：说话消耗，安静恢复（rest 恢复更快）
        rate = -0.012 if chatting else (0.008 if attention == AttentionMode.REST
                                        else 0.004)
        self.energy = _clamp(self.energy + rate * dt, 0.0, 10.0)

        # 想陪：安静超过 40s 才开始积累，聊天中迅速满足
        if chatting or since_voice < 40:
            self._quiet_since = time.monotonic()
            self.social = _clamp(self.social - 0.15 * dt, 0.0, 10.0)
        else:
            self.social = _clamp(self.social + 0.008 * dt, 0.0, 10.0)

        # 心情：缓慢回归平静；聊天中微微向好
        drift = 0.006 if chatting else -0.0
        self.mood = _clamp(self.mood * (1 - min(dt * 0.008, 0.5)) + drift * dt,
                           -5.0, 5.0)
        # 强表情（哭等）随时间平复：60s 无新情绪 → 回落平静
        if self.mood_value != "平静" and \
                time.monotonic() - self._mood_value_t > 60.0:
            self.mood_value = "平静"

    # 事件注入 ---------------------------------------------------------

    def on_user_voice(self) -> None:
        self._last_user_voice = time.monotonic()
        self.social = _clamp(self.social - 3.0, 0.0, 10.0)
        self.mood = _clamp(self.mood + 0.3, -5.0, 5.0)
        self.energy = _clamp(self.energy - 0.2, 0.0, 10.0)

    def seconds_since_user_voice(self) -> float:
        """距上次听到主人说话的秒数（公开方法，供引擎查询）。"""
        return time.monotonic() - self._last_user_voice

    def on_barge_in(self, sensitivity: float = 5.0) -> None:
        self.mood = _clamp(self.mood - 0.1 * (sensitivity / 5.0), -5.0, 5.0)
        self.energy = _clamp(self.energy - 0.5, 0.0, 10.0)

    def on_response_done(self) -> None:
        self.mood = _clamp(self.mood + 0.15, -5.0, 5.0)
        self.energy = _clamp(self.energy - 0.3, 0.0, 10.0)
        self.social = _clamp(self.social - 1.0, 0.0, 10.0)

    # 对话情绪 → 内在心情（编排器每轮回灌，眼睛表情随之变化）
    _MOOD_SCORE = {                       # Mood 枚举 → 内在心情分
        "开心": 2.5, "得意": 2.0, "调皮": 1.8, "关心": 1.0, "平静": 0.2,
        "傲娇": -0.3, "困倦": -0.5, "吃醋": -1.5, "委屈": -2.2, "生气": -3.0,
    }

    def observe_dialogue_mood(self, mood_value: str) -> None:
        """每轮对话后，把对话情绪引擎的结果融入内在心情（平滑过渡）。"""
        self.mood_value = mood_value      # 保留原始值（哭/大笑等强表情用）
        self._mood_value_t = time.monotonic()
        score = self._MOOD_SCORE.get(mood_value, 0.0)
        self.mood = _clamp(self.mood * 0.5 + score * 0.5, -5.0, 5.0)

    def on_noise(self) -> None:
        pass   # 噪声对内在状态无实质影响（旧模型虚涨 alert，已删）

    def describe(self) -> str:
        """给 LLM 念头 Prompt 的状态摘要（口语化，不报数字）。"""
        parts = []
        if self.mood > 1.0:
            parts.append("心情不错")
        elif self.mood < -1.0:
            parts.append("有点委屈")
        if self.energy < 3.0:
            parts.append("很困")
        elif self.energy < 5.5:
            parts.append("有点累")
        if self.social > 6.0:
            parts.append("特别想找人说话")
        elif self.social > 3.5:
            parts.append("有点想主人了")
        return "，".join(parts) if parts else "状态平静"


@dataclass
class Personality:
    """§2.4 人格固定参数（配置加载）。"""
    extraversion: float = 6.0
    curiosity_weight: float = 6.0
    sensitivity: float = 5.0
    like: list[str] = field(default_factory=lambda: ["晚霞", "小故事", "聊天气"])
    dislike: list[str] = field(default_factory=lambda: ["巨大噪音", "连续频繁打断"])


class AttentionSystem:
    """§2.2 注意力系统（focus_on_owner / wandering / rest）。"""

    def __init__(self, sleep_energy: float = 2.5,
                 wandering_after_s: float = 45.0):
        self.mode = AttentionMode.FOCUS_ON_OWNER
        self._wandering_after_s = wandering_after_s
        self._sleep_energy = sleep_energy
        self._quiet_since = time.monotonic()

    def update(self, life_state: LifeState, energy: float,
               user_voice: bool) -> AttentionMode:
        now = time.monotonic()
        if life_state in (LifeState.CHATTING, LifeState.MUTTERING):
            self.mode = AttentionMode.FOCUS_ON_OWNER
            self._quiet_since = now
            return self.mode
        if user_voice or energy > 6.0:
            self.mode = AttentionMode.FOCUS_ON_OWNER
            self._quiet_since = now
            return self.mode
        if energy < self._sleep_energy:
            self.mode = AttentionMode.REST
            return self.mode
        if now - self._quiet_since > self._wandering_after_s:
            self.mode = AttentionMode.WANDERING
        return self.mode
