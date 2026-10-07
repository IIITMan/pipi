"""音频领域模型 —— ASR 的统一输出格式。

关键设计：SenseVoice 单次推理**同时**输出文本、情感、音频事件，
所以 Transcript 一次携带三者，避免重复调用模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AudioEventType(str, Enum):
    """音频事件类型（SenseVoice 原生支持）。"""

    SPEECH = "speech"
    LAUGHTER = "laughter"      # 笑声 → 皮皮可以回应"你笑啥"
    CRY = "cry"                # 哭声 → 高优先级关心
    SIGH = "sigh"              # 叹气 → 关心
    COUGH = "cough"
    SNEEZE = "sneeze"
    APPLAUSE = "applause"
    BGM = "bgm"
    NOISE = "noise"

    CN: dict[str, str] = None  # type: ignore[assignment]


# 事件 → 中文描述（注入 LLM 用）
EVENT_CN: dict[AudioEventType, str] = {
    AudioEventType.LAUGHTER: "他在笑",
    AudioEventType.CRY: "他好像在哭",
    AudioEventType.SIGH: "他叹了口气",
    AudioEventType.COUGH: "他咳嗽了",
    AudioEventType.SNEEZE: "他打了个喷嚏",
    AudioEventType.APPLAUSE: "有掌声",
    AudioEventType.BGM: "有背景音乐",
}


@dataclass
class AudioEvent:
    """一个音频事件。"""

    type: AudioEventType
    confidence: float = 1.0
    start_ms: int = 0
    end_ms: int = 0

    def describe_cn(self) -> str:
        return EVENT_CN.get(self.type, self.type.value)


@dataclass
class Transcript:
    """ASR 统一输出：文本 + 情感 + 音频事件 + 副语言特征。"""

    text: str
    is_final: bool = True

    # 情感（SenseVoice 原生：HAPPY/SAD/ANGRY/NEUTRAL/FEARFUL/DISGUSTED/SURPRISED）
    emotion: str = "NEUTRAL"
    emotion_confidence: float = 0.0

    # 音频事件
    events: list[AudioEvent] = field(default_factory=list)

    # 副语言特征（用于判断情绪强度）
    speech_rate: float | None = None    # 字/秒
    energy: float | None = None         # 归一化音量 0..1
    language: str | None = None         # zh/en/ja/ko/yue
    duration_ms: int = 0

    raw: dict[str, Any] = field(default_factory=dict)

    # ASR 情感 → 内部效价映射
    EMOTION_VALENCE: dict[str, float] = field(default_factory=lambda: {
        "HAPPY": 0.8,
        "SURPRISED": 0.3,
        "NEUTRAL": 0.0,
        "FEARFUL": -0.5,
        "SAD": -0.7,
        "ANGRY": -0.6,
        "DISGUSTED": -0.6,
    }, repr=False)

    @property
    def is_negative(self) -> bool:
        return self.valence <= -0.3

    @property
    def valence(self) -> float:
        """情绪效价 −1..1，负值代表负面情绪。"""
        base = self.EMOTION_VALENCE.get(self.emotion.upper(), 0.0)
        return base * max(0.5, self.emotion_confidence)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    def event_hints(self) -> list[str]:
        """返回非语音事件的描述列表。"""
        return [e.describe_cn() for e in self.events
                if e.type not in (AudioEventType.SPEECH, AudioEventType.NOISE)]
