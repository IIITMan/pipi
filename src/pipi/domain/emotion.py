"""情感状态与状态机。

情绪不是"每次现算"的，而是一个**有记忆的状态**：
  - mood / intensity  当前主情绪及其强度
  - affection         好感度（0~1000，长期累积）
  - companion_need    陪伴需求（0~1，累积后会推动主动搭话）

强度会自然回落（每轮 ×0.85），低于阈值回归平静，
这模拟了"人生气一会儿就消气了"的自然过程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Mood(str, Enum):
    """皮皮可能处于的情绪状态。"""

    CALM = "平静"
    HAPPY = "开心"
    CONCERNED = "关心"
    JEALOUS = "吃醋"
    AFFECTED = "委屈"
    PROUD = "得意"
    SULKY = "傲娇"
    ANGRY = "生气"
    SLEEPY = "困倦"
    PLAYFUL = "调皮"


@dataclass
class EmotionTransition:
    """一次情绪转移的规则定义。

    Attributes:
        trigger: 触发源描述（用于日志与调试）
        target: 转移到的情绪
        intensity: 转移后的强度
        affection_delta: 好感度变化
        need_delta: 陪伴需求变化
    """

    trigger: str
    target: Mood
    intensity: float
    affection_delta: float = 0.0
    need_delta: float = 0.0


# ---------------------------------------------------------------------------
# 状态转移表：把"什么情况下皮皮会怎么想"集中定义，避免散落在业务代码里
# ---------------------------------------------------------------------------

MOOD_TRANSITIONS: dict[str, EmotionTransition] = {
    # 用户提到竞争对象（猫/狗/别的机器人/朋友）
    "mention_rival": EmotionTransition(
        trigger="用户提到吃醋对象",
        target=Mood.JEALOUS,
        intensity=0.7,
        affection_delta=-5.0,
        need_delta=0.10,
    ),
    # 用户吐露负面情绪
    "negative_emotion": EmotionTransition(
        trigger="用户表达负面情绪",
        target=Mood.CONCERNED,
        intensity=0.7,
        affection_delta=1.0,
        need_delta=0.20,
    ),
    # 用户夸奖 / 致谢
    "praise": EmotionTransition(
        trigger="用户夸奖或致谢",
        target=Mood.PROUD,
        intensity=0.8,
        affection_delta=5.0,
        need_delta=-0.10,
    ),
    # 长时间没有交互（每小时累积，由 decay pass 触发）
    # 产品约束（ENGINE_CONTRACTS §5.5）：沉默不推断委屈/冷落——
    # 只累积陪伴需求，情绪保持平静，主动降频由 proactive 引擎负责。
    "idle": EmotionTransition(
        trigger="长时间未交互（安静等待，不推断委屈）",
        target=Mood.CALM,
        intensity=0.3,
        affection_delta=0.0,
        need_delta=0.15,
    ),
    # 视觉：主人回家
    "owner_arrived": EmotionTransition(
        trigger="主人回家（视觉）",
        target=Mood.HAPPY,
        intensity=0.8,
        affection_delta=3.0,
        need_delta=-0.30,
    ),
    # 久别重逢：上次交互距今很久后再次说话 —— 好感度回来
    "warm_return": EmotionTransition(
        trigger="久别后重新联系",
        target=Mood.HAPPY,
        intensity=0.7,
        affection_delta=12.0,
        need_delta=-0.40,
    ),
    # 普通一轮对话的日常回暖
    "casual_warmth": EmotionTransition(
        trigger="日常闲聊的陪伴感",
        target=Mood.CALM,
        intensity=0.35,
        affection_delta=0.6,
        need_delta=-0.05,
    ),
    # 用户负面情绪 + 视觉表情难过 → 强化关心
    "negative_with_sad_face": EmotionTransition(
        trigger="用户负面情绪且表情难过",
        target=Mood.CONCERNED,
        intensity=0.9,
        affection_delta=2.0,
        need_delta=0.20,
    ),
    # 被用户打断
    "interrupted": EmotionTransition(
        trigger="回复被用户打断",
        target=Mood.SULKY,
        intensity=0.5,
        affection_delta=0.0,
        need_delta=0.05,
    ),
    # 用户忽视主动搭话（连续未回应）
    # 产品约束（ENGINE_CONTRACTS §5.5）：被忽视不累计委屈——情绪保持平静，
    # 降频（上限减半/归零）由 proactive 引擎的 ignored_streak 负责。
    "ignored": EmotionTransition(
        trigger="主动搭话被忽视（降频而非委屈）",
        target=Mood.CALM,
        intensity=0.3,
        affection_delta=0.0,
        need_delta=0.0,
    ),
}


@dataclass
class EmotionState:
    """皮皮当前的情感状态快照。

    这个对象每轮对话都会被更新，并注入到 LLM 的 system message 里，
    让模型"知道"自己现在是什么心情。
    """

    mood: Mood = Mood.CALM
    intensity: float = 0.3
    affection: float = 500.0
    companion_need: float = 0.0
    last_interaction: float = 0.0
    updated_at: float = 0.0
    # 是否已"认主"——决定好感度是否设底价（关系会不会归零）
    is_attachment: bool = True

    # ---- 常量 ----
    AFFECTION_MIN: float = field(default=0.0, repr=False)
    AFFECTION_MAX: float = field(default=1000.0, repr=False)
    INTENSITY_DECAY: float = field(default=0.85, repr=False)
    INTENSITY_FLOOR: float = field(default=0.3, repr=False)
    NEED_MIN: float = field(default=0.0, repr=False)
    NEED_MAX: float = field(default=1.0, repr=False)

    def apply(self, transition: EmotionTransition) -> "EmotionState":
        """应用一次情绪转移，返回自身（便于链式调用）。"""
        self.mood = transition.target
        self.intensity = max(0.0, min(1.0, transition.intensity))
        self.affection = max(
            self.AFFECTION_MIN, min(self.AFFECTION_MAX, self.affection + transition.affection_delta)
        )
        self.companion_need = max(
            self.NEED_MIN, min(self.NEED_MAX, self.companion_need + transition.need_delta)
        )
        return self

    def apply_by_key(self, key: str) -> "EmotionState":
        """按规则名应用转移；未知 key 不报错（便于扩展）。"""
        if t := MOOD_TRANSITIONS.get(key):
            return self.apply(t)
        return self

    def cool_down(self) -> "EmotionState":
        """情绪强度自然回落。每轮对话结束后调用。"""
        self.intensity *= self.INTENSITY_DECAY
        if self.intensity < self.INTENSITY_FLOOR:
            self.intensity = self.INTENSITY_FLOOR
            self.mood = Mood.CALM
        return self

    def tick_idle(self, hours: float) -> "EmotionState":
        """空闲时间流逝：好感度下降、陪伴需求上升。

        注意：
          - 好感度**不是无限掉**的。长期不联系会下降，但存在一个
            "关系底价" —— 认过主的人不该因为出差一个月就变成陌生人。
          - 下降也**不该随小时线性叠加**：用 sqrt 压缩，让第一天的
            "你怎么不理我"感最强，之后逐渐钝化（符合真实心理）。
        """
        if hours <= 0:
            return self

        decay = 3.0 * (hours ** 0.5)      # 平方根压缩：越久越钝
        floor = self.affection_floor()
        self.affection = max(floor, self.affection - decay)

        # 陪伴需求同样压缩增长，且 24h 内到顶（否则一天不见就崩了）
        self.companion_need = min(
            self.NEED_MAX, self.companion_need + 0.15 * min(hours, 24.0))

        if hours >= 2:
            self.mood = Mood.AFFECTED
            self.intensity = min(1.0, 0.4 + 0.1 * hours)
        return self

    def affection_floor(self) -> float:
        """关系底价 —— 不因长期不联系而掉到这以下。

        区分是否"认主"：认主的人有较高底价（关系不会归零），
        陌生人不设底价（可以彻底遗忘）。
        """
        return 300.0 if self.is_attachment else self.AFFECTION_MIN

    @property
    def affection_level(self) -> str:
        """好感度分档，用于描述亲密度。"""
        if self.affection >= 850:
            return "非常亲密"
        if self.affection >= 650:
            return "亲近"
        if self.affection >= 400:
            return "熟悉"
        if self.affection >= 200:
            return "有点熟"
        return "陌生"

    def to_prompt_line(self) -> str:
        """生成注入 LLM 的一句话状态描述。"""
        return (
            f"[当前状态] 情绪={self.mood.value}(强度{self.intensity:.1f}) "
            f"好感度={self.affection:.0f}({self.affection_level}) "
            f"想被陪着={self.companion_need:.1f}"
        )
