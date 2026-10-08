"""视觉 Provider 工厂。"""

from __future__ import annotations

import logging
from typing import Any

from pipi.providers.vision.base import VisionProvider
from pipi.providers.vision.clothing import ColorClothingAnalyzer
from pipi.providers.vision.detector import (
    HaarDetector,
    InsightFaceRecognizer,
    MediaPipeDetector,
    YuNetDetector,
)
from pipi.providers.vision.expression import (
    FERRecognizer,
    HsemotionRecognizer,
    NullExpressionRecognizer,
)
from pipi.providers.vision.mock import (
    MockVision,
    create_mock_vision,
    make_absence_observation,
    make_arrival_observation,
)
from pipi.providers.vision.pipeline import VisionPipeline

logger = logging.getLogger(__name__)


def _create_detector(name: str, model_path: str = "") -> Any:
    candidates = {
        "yunet": lambda: YuNetDetector(model_path=model_path),
        "mediapipe": MediaPipeDetector,
        "opencv_haar": HaarDetector,
    }
    if name in candidates:
        d = candidates[name]()
        if d.is_available():
            return d
    # 按优先级自动挑
    for factory in (lambda: YuNetDetector(model_path=model_path),
                    MediaPipeDetector, HaarDetector):
        try:
            d = factory()
            if d.is_available():
                logger.info("视觉检测器选用：%s", d.name)
                return d
        except Exception:
            continue
    return None


def _create_recognizer(name: str, threshold: float) -> Any:
    if name == "none":
        return None
    r = InsightFaceRecognizer(match_threshold=threshold)
    if r.is_available():
        return r
    return None


def _create_expression(name: str) -> Any:
    for factory in (HsemotionRecognizer, FERRecognizer):
        try:
            r = factory()
            if r.is_available():
                logger.info("表情识别器选用：%s", r.name)
                return r
        except Exception:
            continue
    logger.info("表情识别不可用，将由语音情感标签补位")
    return NullExpressionRecognizer()


def create_vision(cfg: Any) -> VisionProvider | None:
    """创建完整视觉 pipeline。"""
    if not cfg.get("vision.enabled", True):
        return None

    caps_cfg = cfg.get("vision.capabilities", {}) or {}

    # 摄像头驱动
    try:
        from pipi.hardware.sim import OpenCVCamera
        camera = OpenCVCamera(
            index=cfg.get("vision.camera_index", 0),
            width=640, height=480,
        )
        if not camera.is_available():
            logger.info("摄像头不可用（vision.enabled=true 但无设备），改用 MockVision")
            return MockVision()
    except Exception as exc:
        logger.info("摄像头初始化失败：%s，改用 MockVision", exc)
        return MockVision()

    detector = _create_detector(cfg.get("vision.detector", "yunet")) \
        if caps_cfg.get("detect", True) else None
    recognizer = _create_recognizer(cfg.get("vision.recognizer", "insightface"),
                                    cfg.get("vision.face_match_threshold", 0.45)) \
        if caps_cfg.get("recognize", True) else None
    expression = _create_expression(cfg.get("vision.expression_model", "hsemotion")) \
        if caps_cfg.get("expression", True) else None
    clothing = ColorClothingAnalyzer() if caps_cfg.get("clothing", True) else None

    return VisionPipeline(
        camera,
        detector=detector, recognizer=recognizer,
        expression=expression, clothing=clothing,
    )


__all__ = [
    "VisionProvider", "VisionPipeline", "MockVision", "create_mock_vision",
    "make_arrival_observation", "make_absence_observation",
    "YuNetDetector", "HaarDetector", "MediaPipeDetector",
    "InsightFaceRecognizer",
    "HsemotionRecognizer", "FERRecognizer", "NullExpressionRecognizer",
    "ColorClothingAnalyzer",
    "create_vision",
]
