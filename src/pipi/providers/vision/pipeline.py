"""视觉 Pipeline —— 把四个子能力串起来，并做帧间状态对比。

核心职责：
  1. 按可用能力组合 pipeline（缺哪个跳过哪个）
  2. 对比上一帧，算出 owner_newly_arrived / owner_just_left
     —— 这是"主动打招呼"的触发源
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pipi.domain.vision import (
    ClothingObservation,
    ExpressionObservation,
    FaceObservation,
    VisionObservation,
)
from pipi.providers.vision.clothing import ColorClothingAnalyzer

logger = logging.getLogger(__name__)


class VisionPipeline:
    """组合式视觉 pipeline。"""

    def __init__(
        self,
        camera: Any,
        *,
        detector: Any | None = None,
        recognizer: Any | None = None,
        expression: Any | None = None,
        clothing: Any | None = None,
        owner_person_id: str = "owner",
    ):
        self.camera = camera
        self.detector = detector
        self.recognizer = recognizer
        self.expression = expression
        self.clothing = clothing or ColorClothingAnalyzer()
        self.owner_person_id = owner_person_id

        self._frame_id = 0
        self._prev_owner_present = False
        self._last_owner_seen_at = 0.0

    # ------------------------------------------------------------------

    def capabilities(self) -> set[str]:
        caps = set()
        if self.detector and self.detector.is_available():
            caps.add("detect")
        if self.recognizer and self.recognizer.is_available():
            caps.add("recognize")
        if self.expression and self.expression.is_available():
            caps.add("expression")
        if self.clothing and self.clothing.is_available():
            caps.add("clothing")
        return caps

    async def start(self) -> None:
        if hasattr(self.camera, "open"):
            await _maybe_await(self.camera.open())
        caps = self.capabilities()
        logger.info("视觉 pipeline 启动，可用能力：%s", ", ".join(sorted(caps)) or "无")
        if self.recognizer and hasattr(self.recognizer, "load_enrolled"):
            pass  # 由外部注入已注册人脸

    async def aclose(self) -> None:
        if hasattr(self.camera, "close"):
            await _maybe_await(self.camera.close())

    # ------------------------------------------------------------------

    async def capture(self) -> VisionObservation | None:
        frame = await _maybe_await(self.camera.read())
        if frame is None:
            return None
        return self.process_frame(frame)

    def process_frame(self, frame) -> VisionObservation:
        """处理单帧。可被 mock/离线场景直接调用。"""
        self._frame_id += 1
        now = time.time()
        caps = self.capabilities()

        obs = VisionObservation(timestamp=now, frame_id=self._frame_id)

        # ---- 1. 人脸检测 ----
        boxes = []
        if "detect" in caps:
            try:
                boxes = self.detector.detect(frame)
            except Exception as exc:
                logger.debug("检测失败：%s", exc)
        obs.person_count = len(boxes)

        # ---- 2. 逐脸识别 ----
        owner_present = False
        for box in boxes:
            face = FaceObservation(bbox=box)

            if "recognize" in caps:
                try:
                    pid, conf = self.recognizer.identify(frame, box)
                    face.person_id = pid
                    face.identity_confidence = conf
                    face.is_owner = (pid == self.owner_person_id)
                except Exception as exc:
                    logger.debug("识别失败：%s", exc)

            if face.is_owner:
                owner_present = True

            obs.faces.append(face)

            # 关键 person_id（主人优先，否则第一张脸）
            key = "owner" if face.is_owner else (face.person_id or f"unknown_{len(obs.faces)}")

            # ---- 3. 表情 ----
            if "expression" in caps:
                try:
                    obs.expressions[key] = self.expression.analyze(frame, box)
                except Exception as exc:
                    logger.debug("表情失败：%s", exc)

            # ---- 4. 衣着 ----
            if "clothing" in caps:
                try:
                    obs.clothing[key] = self.clothing.analyze(frame, box)
                except Exception as exc:
                    logger.debug("衣着失败：%s", exc)

        # ---- 5. 帧间对比：算出"刚回来" ----
        obs.owner_present = owner_present
        if owner_present and not self._prev_owner_present:
            obs.owner_newly_arrived = True
            logger.info("检测到主人出现")
        elif not owner_present and self._prev_owner_present:
            obs.owner_just_left = True
            logger.info("检测到主人离开")

        if owner_present:
            self._last_owner_seen_at = now
        self._prev_owner_present = owner_present

        return obs


async def _maybe_await(value: Any) -> Any:
    """兼容同步/异步的 camera 接口。"""
    if hasattr(value, "__await__"):
        return await value
    return value
