"""视觉 Provider 接口与子能力定义。

四个子能力各自独立、可分别降级：
  detect      人脸检测      ← 基础，几乎总能开
  recognize   人脸识别      ← 认主，需要 InsightFace
  expression  表情识别      ← 需要 FER/hsemotion
  clothing    衣着颜色      ← 纯 OpenCV，最稳

设计意图：InsightFace 装不上不影响衣着识别，
表情模型缺失时用 ASR 的情感标签替代 —— 体验不塌。
"""

from __future__ import annotations

from typing import Protocol

from pipi.domain.vision import (
    BBox,
    ClothingObservation,
    ExpressionObservation,
    VisionObservation,
)


class FaceDetector(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def detect(self, frame) -> list[BBox]: ...


class FaceRecognizer(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def enroll(self, person_id: str, frames: list, is_owner: bool = False) -> list[float]: ...

    def identify(self, frame, bbox: BBox) -> tuple[str | None, float]: ...


class ExpressionRecognizer(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def analyze(self, frame, bbox: BBox) -> ExpressionObservation: ...


class ClothingAnalyzer(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def analyze(self, frame, person_bbox: BBox) -> ClothingObservation: ...


class VisionProvider(Protocol):
    """完整视觉能力。"""

    name: str

    def is_available(self) -> bool: ...

    def capabilities(self) -> set[str]: ...

    async def start(self) -> None: ...

    async def capture(self) -> VisionObservation | None: ...

    async def aclose(self) -> None: ...
