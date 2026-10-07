"""领域模型层 —— 纯数据定义，不依赖任何基础设施。

这一层是整个系统的"词汇表"：所有模块之间传递的数据都长这样。
设计原则：
  1. 零外部依赖（只用标准库），可被任意层引用而不产生循环依赖
  2. 用 dataclass 而非 dict，保证类型安全和可读性
  3. 枚举集中定义，避免散落的魔法字符串
"""

from pipi.domain.emotion import EmotionState, Mood, EmotionTransition, MOOD_TRANSITIONS
from pipi.domain.memory import (
    DecayReport,
    MemoryItem,
    MemoryLayer,
    MemoryType,
    RetrievedMemory,
    RetrieveQuery,
)
from pipi.domain.persona import PersonaSpec, SpeechStyle, Traits
from pipi.domain.profile import UserProfile
from pipi.domain.dialogue import Bubble, DialogueTurn, Role, Session
from pipi.domain.vision import (
    BBox,
    ClothingObservation,
    ExpressionObservation,
    FaceObservation,
    VisionObservation,
)
from pipi.domain.audio import AudioEvent, AudioEventType, Transcript
from pipi.domain.events import (
    OwnerArrivedEvent,
    OwnerLeftEvent,
    ProactiveTriggerEvent,
    SystemEvent,
    UserUtteranceEvent,
)
from pipi.domain.motion import Axis, Gesture, MotionCommand

__all__ = [
    # emotion
    "EmotionState", "Mood", "EmotionTransition", "MOOD_TRANSITIONS",
    # memory
    "DecayReport", "MemoryItem", "MemoryLayer", "MemoryType",
    "RetrievedMemory", "RetrieveQuery",
    # persona
    "PersonaSpec", "SpeechStyle", "Traits",
    # profile
    "UserProfile",
    # dialogue
    "Bubble", "DialogueTurn", "Role", "Session",
    # vision
    "BBox", "ClothingObservation", "ExpressionObservation",
    "FaceObservation", "VisionObservation",
    # audio
    "AudioEvent", "AudioEventType", "Transcript",
    # events
    "OwnerArrivedEvent", "OwnerLeftEvent", "ProactiveTriggerEvent",
    "SystemEvent", "UserUtteranceEvent",
    # motion
    "Axis", "Gesture", "MotionCommand",
]
