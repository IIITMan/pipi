"""情感引擎 —— 把感知信号转成情绪状态转移。

输入可以是：
  - 文本（关键词 + 可选 LLM 分析）
  - ASR 情感标签（SenseVoice 原生输出）
  - 视觉表情（人脸表情识别）
  - 系统事件（用户回家、长时间未交互）

输出：更新后的 EmotionState

融合策略：多个来源取"最强烈"的那个，避免来回横跳。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from pipi.domain.emotion import MOOD_TRANSITIONS, EmotionState, Mood
from pipi.domain.persona import PersonaSpec

logger = logging.getLogger(__name__)


# 关键词词典（无 LLM 时的兜底判断）
NEGATIVE_WORDS = [
    "累", "烦", "难过", "难受", "压力", "崩溃", "哭", "焦虑", "emo",
    "不开心", "郁闷", "委屈", "疼", "生病", "加班", "熬夜", "吵架",
    "分手", "失业", "失败", "紧张", "害怕", "孤单", "一个人",
]
POSITIVE_WORDS = [
    "开心", "高兴", "太棒", "升职", "赢了", "成功", "通过", "舒服",
    "顺利", "喜欢", "太好了", "哈哈哈", "笑死",
]
PRAISE_WORDS = ["喜欢你", "爱你", "想你", "好棒", "厉害", "谢谢", "好可爱", "真聪明"]
RIVAL_WORDS = ["猫", "狗", "别的机器人", "小爱", "天猫精灵", "chatgpt",
               "小度", "小艺", "siri", "别的助手"]


@dataclass
class EmotionSignal:
    """一个情感信号（来自某个感知源）。"""

    valence: float = 0.0        # −1..1
    arousal: float = 0.0        # 0..1
    is_negative: bool = False
    is_positive: bool = False
    needs_comfort: bool = False
    has_rival: bool = False
    is_praise: bool = False
    source: str = "text"
    keywords: list[str] = None

    def __post_init__(self):
        if self.keywords is None:
            self.keywords = []


class EmotionEngine:
    """情绪状态机。"""

    def __init__(self, persona: PersonaSpec, llm: Any | None = None):
        self.persona = persona
        self.llm = llm

    # ------------------------------------------------------------------
    # 信号提取
    # ------------------------------------------------------------------

    def analyze_text(self, text: str) -> EmotionSignal:
        """纯规则的情感分析（快、无需模型）。"""
        if not text:
            return EmotionSignal()
        low = text.lower()
        sig = EmotionSignal(source="text")

        neg_hits = [w for w in NEGATIVE_WORDS if w in text]
        pos_hits = [w for w in POSITIVE_WORDS if w in text]
        sig.keywords = neg_hits + pos_hits

        if neg_hits:
            sig.is_negative = True
            sig.valence = -min(0.9, 0.35 + 0.15 * len(neg_hits))
            sig.needs_comfort = len(neg_hits) >= 1
        if pos_hits:
            sig.is_positive = True
            sig.valence = max(sig.valence, min(0.9, 0.35 + 0.15 * len(pos_hits)))

        sig.has_rival = any(w in low for w in RIVAL_WORDS)
        sig.is_praise = any(w in text for w in PRAISE_WORDS)

        # 感叹号/问号密度 → 唤醒度
        exclaim = text.count("！") + text.count("!")
        question = text.count("？") + text.count("?")
        sig.arousal = min(1.0, 0.2 + 0.2 * exclaim + 0.1 * question)
        if neg_hits:
            sig.arousal = min(1.0, sig.arousal + 0.2)

        return sig

    def signal_from_asr(self, emotion: str, confidence: float) -> EmotionSignal:
        """从 ASR（SenseVoice）的情感标签构造信号。"""
        mapping = {
            "HAPPY": (0.7, 0.6, False, True),
            "SAD": (-0.7, 0.4, True, False),
            "ANGRY": (-0.6, 0.8, True, False),
            "FEARFUL": (-0.5, 0.7, True, False),
            "DISGUSTED": (-0.5, 0.5, True, False),
            "SURPRISED": (0.2, 0.8, False, False),
            "NEUTRAL": (0.0, 0.2, False, False),
        }
        val, aro, neg, pos = mapping.get(emotion.upper(), (0.0, 0.2, False, False))
        if confidence < 0.3:
            val, aro = val * 0.4, aro * 0.5
        return EmotionSignal(
            valence=val, arousal=aro, is_negative=neg,
            is_positive=pos, needs_comfort=neg,
            source="asr",
        )

    def signal_from_vision(self, expression_label: str, confidence: float) -> EmotionSignal:
        """从人脸表情构造信号。"""
        mapping = {
            "happy": (0.7, 0.5, False, True),
            "sad": (-0.7, 0.3, True, False),
            "angry": (-0.6, 0.8, True, False),
            "surprised": (0.2, 0.8, False, False),
            "fear": (-0.5, 0.7, True, False),
            "disgust": (-0.5, 0.5, True, False),
            "neutral": (0.0, 0.2, False, False),
        }
        val, aro, neg, pos = mapping.get(expression_label, (0.0, 0.2, False, False))
        if confidence < 0.4:
            val, aro = val * 0.5, aro * 0.5
        return EmotionSignal(
            valence=val, arousal=aro, is_negative=neg,
            is_positive=pos, needs_comfort=neg,
            source="vision",
        )

    # ------------------------------------------------------------------
    # 状态转移
    # ------------------------------------------------------------------

    def update(self, state: EmotionState, signals: list[EmotionSignal],
               now: float) -> tuple[EmotionState, str]:
        """综合多个信号，更新情绪状态。返回 (新状态, 触发原因)。"""
        state.last_interaction = now
        state.updated_at = now

        valid = [s for s in signals if s is not None]
        if not valid:
            state.cool_down()
            return state, "cool_down"

        # 取最强烈的信号（按 |valence| + arousal 排序）
        strongest = max(valid, key=lambda s: abs(s.valence) + s.arousal)

        trigger = "neutral"

        # 优先级：吃醋 > 被夸 > 负面 > 正面
        rival_sig = next((s for s in valid if s.has_rival), None)
        praise_sig = next((s for s in valid if s.is_praise), None)
        neg_sig = next((s for s in valid if s.is_negative), None)

        if rival_sig:
            state.apply_by_key("mention_rival")
            # 用 persona 的吃醋幅度覆盖默认值
            state.affection += (self.persona.jealousy_affection_delta + 5.0)
            trigger = "mention_rival"
        elif praise_sig:
            state.apply_by_key("praise")
            state.affection += (self.persona.praise_affection_delta - 5.0)
            trigger = "praise"
        elif neg_sig:
            # 文本负面 + 视觉也负面 → 强化关心
            vision_neg = any(s.is_negative and s.source == "vision" for s in valid)
            key = "negative_with_sad_face" if vision_neg else "negative_emotion"
            state.apply_by_key(key)
            # 强度随信号强度调整
            state.intensity = min(1.0, state.intensity + abs(neg_sig.valence) * 0.3)
            trigger = key
        elif strongest.is_positive:
            state.mood = Mood.HAPPY
            state.intensity = min(1.0, 0.4 + strongest.arousal * 0.4)
            state.affection = min(1000.0, state.affection + 1.0)
            trigger = "positive"
        else:
            state.cool_down()
            trigger = "cool_down"

        state.affection = max(0.0, min(1000.0, state.affection))
        state.companion_need = max(0.0, min(1.0, state.companion_need))
        return state, trigger

    def on_event(self, state: EmotionState, event_key: str, now: float) -> tuple[EmotionState, str]:
        """处理系统事件（回家/被忽视/被打断）。"""
        state.updated_at = now
        if event_key == "owner_arrived":
            state.apply_by_key("owner_arrived")
        elif event_key == "idle":
            state.apply_by_key("idle")
        elif event_key == "ignored":
            state.apply_by_key("ignored")
        elif event_key == "interrupted":
            state.apply_by_key("interrupted")
        else:
            return state, "unknown"
        state.last_interaction = now
        return state, event_key

    def tick_idle(self, state: EmotionState, now: float) -> EmotionState:
        """时间流逝处理 —— 好感度衰减、陪伴需求累积。

        两个重要约束（否则"出差一个月回来变陌生人"）：
          1. 衰减用 sqrt 压缩，不是线性叠加 —— 第一天掉得最狠，之后钝化
          2. 好感度有"关系底价"，认主的人不会归零
        """
        if not state.last_interaction:
            state.last_interaction = now
            return state
        hours = max(0.0, (now - state.last_interaction) / 3600.0)
        if hours <= 0:
            return state

        rate = self.persona.affection_decay_per_hour
        floor = state.affection_floor()
        state.affection = max(floor, state.affection - rate * (hours ** 0.5))
        state.companion_need = min(
            1.0, state.companion_need + 0.15 * min(hours, 24.0))

        if hours >= 2:
            state.mood = Mood.AFFECTED
            state.intensity = min(1.0, 0.4 + 0.1 * hours)
        state.updated_at = now
        return state

    # ------------------------------------------------------------------
    # 表达映射
    # ------------------------------------------------------------------

    def mood_to_voice_emotion(self, state: EmotionState) -> str:
        """情绪 → TTS 情感标签（豆包 2.0 语音指令用）。"""
        mapping = {
            Mood.CALM: "neutral",
            Mood.HAPPY: "cheerful",
            Mood.CONCERNED: "gentle",
            Mood.JEALOUS: "sulky",
            Mood.AFFECTED: "sad",
            Mood.PROUD: "smug",
            Mood.SULKY: "sulky",
            Mood.ANGRY: "firm",
            Mood.SLEEPY: "gentle",
            Mood.PLAYFUL: "cheerful",
        }
        return mapping.get(state.mood, "neutral")

    def mood_to_gesture(self, state: EmotionState) -> str | None:
        """情绪 → 配套的肢体动作（部署到机器人时生效）。"""
        from pipi.domain.motion import Gesture
        mapping = {
            Mood.HAPPY: Gesture.HAPPY_WIGGLE,
            Mood.PROUD: Gesture.NOD,
            Mood.JEALOUS: Gesture.TURN_AWAY,
            Mood.AFFECTED: Gesture.TURN_AWAY,
            Mood.SULKY: Gesture.SHAKE_HEAD,
            Mood.CONCERNED: Gesture.LEAN_IN,
            Mood.SLEEPY: Gesture.SLEEP,
        }
        g = mapping.get(state.mood)
        return g.value if g else None

    def needs_comfort(self, signals: list[EmotionSignal]) -> bool:
        """是否需要先安抚情绪（编排层据此调整回复策略）。"""
        return any(s.needs_comfort for s in signals if s)
