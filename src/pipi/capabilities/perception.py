"""感知融合 —— 把音视频感知结果翻译成给 LLM 的自然语言。

关键设计：**不把原始 JSON 塞给模型**。
模型看到 {"clothing_color":"灰色"} 会用"字段感"的语气回复；
看到 "[你看到的] 他穿着灰色的衣服" 才会自然地说"你穿灰色好看"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pipi.domain.audio import Transcript
from pipi.domain.profile import UserProfile
from pipi.domain.vision import VisionObservation

logger = logging.getLogger(__name__)


@dataclass
class PerceptionResult:
    """融合后的感知结果。"""

    vision_hint: str = ""
    audio_hint: str = ""
    owner_present: bool = False
    owner_newly_arrived: bool = False
    owner_just_left: bool = False
    extra_person_count: int = 0
    clothing_color: str = ""
    expression: str = ""

    @property
    def has_any(self) -> bool:
        return bool(self.vision_hint or self.audio_hint)


class PerceptionService:
    """感知融合服务。"""

    def __init__(self, owner_name: str = "主人"):
        self.owner_name = owner_name

    def fuse(
        self,
        vision: VisionObservation | None = None,
        transcript: Transcript | None = None,
        profile: UserProfile | None = None,
    ) -> PerceptionResult:
        result = PerceptionResult()

        # ---- 视觉 ----
        if vision is not None:
            result.owner_present = vision.owner_present
            result.owner_newly_arrived = vision.owner_newly_arrived
            result.owner_just_left = vision.owner_just_left
            result.extra_person_count = vision.extra_person_count

            if cl := vision.clothing.get("owner"):
                result.clothing_color = cl.primary_color() or ""
            if ex := vision.expressions.get("owner"):
                result.expression = ex.label

            result.vision_hint = vision.to_hint(
                profile.display_name if profile and not profile.is_new() else self.owner_name
            ) or ""

        # ---- 音频事件（非语音的声音）----
        if transcript is not None:
            hints = transcript.event_hints()
            if hints:
                result.audio_hint = "[你听到的] " + "，".join(hints)

        return result

    @staticmethod
    def should_greet_on_arrival(vision: VisionObservation | None) -> bool:
        """是否应该因为"人回来了"而主动打招呼。"""
        return bool(vision and vision.owner_newly_arrived and vision.owner_present)

    @staticmethod
    def expression_to_comfort(expression: str) -> bool:
        """表情是否提示需要关心。"""
        return expression in ("sad", "fear", "angry")
