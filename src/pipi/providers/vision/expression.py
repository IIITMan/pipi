"""表情识别。

优先级：
  1. hsemotion（较准，需 onnxruntime）
  2. FER（经典，依赖 tensorflow）
  3. 无 → 返回 neutral，由 ASR 的情感标签补位

设计取舍：表情识别是最容易装不上的能力，
所以**必须能优雅降级**，不能拖垮整个视觉链路。
"""

from __future__ import annotations

import logging
from typing import Any

from pipi.domain.vision import BBox, ExpressionObservation

logger = logging.getLogger(__name__)

# 各模型的标签 → 统一标签
_LABEL_ALIASES = {
    "happiness": "happy", "happy": "happy", "joy": "happy",
    "sadness": "sad", "sad": "sad",
    "anger": "angry", "angry": "angry",
    "fear": "fear", "fearful": "fear", "afraid": "fear",
    "surprise": "surprised", "surprised": "surprised",
    "disgust": "disgust", "disgusted": "disgust",
    "neutral": "neutral", "contempt": "neutral",
}

# 效价/唤醒度映射
_VA_MAP = {
    "happy": (0.8, 0.5), "sad": (-0.7, 0.3), "angry": (-0.6, 0.8),
    "fear": (-0.5, 0.7), "surprised": (0.2, 0.8),
    "disgust": (-0.5, 0.5), "neutral": (0.0, 0.2),
}


class HsemotionRecognizer:
    """hsemotion 表情识别。"""

    name = "hsemotion"

    def __init__(self):
        self._model: Any = None

    def is_available(self) -> bool:
        try:
            from hsemotion_onnx.facial_emotions import HSEmotionRecognizer  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure(self) -> None:
        if self._model is not None:
            return
        from hsemotion_onnx.facial_emotions import HSEmotionRecognizer
        self._model = HSEmotionRecognizer(model_name="enet_b0_8_best_afew")

    def analyze(self, frame, bbox: BBox) -> ExpressionObservation:
        try:
            self._ensure()
            crop = _crop_face(frame, bbox)
            if crop is None:
                return ExpressionObservation(label="neutral", confidence=0.0)
            label, scores = self._model.predict_emotions(crop, logits=False)
            norm = _LABEL_ALIASES.get(str(label).lower(), "neutral")
            conf = float(max(scores)) if hasattr(scores, "__len__") else 0.5
            v, a = _VA_MAP.get(norm, (0.0, 0.2))
            return ExpressionObservation(
                label=norm, confidence=conf, valence=v, arousal=a
            )
        except Exception as exc:
            logger.debug("表情识别失败：%s", exc)
            return ExpressionObservation(label="neutral", confidence=0.0)


class FERRecognizer:
    """FER 库表情识别（经典方案）。"""

    name = "fer"

    def __init__(self):
        self._model: Any = None

    def is_available(self) -> bool:
        try:
            from fer import FER  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure(self) -> None:
        if self._model is not None:
            return
        from fer import FER
        self._model = FER(mtcnn=False)

    def analyze(self, frame, bbox: BBox) -> ExpressionObservation:
        try:
            self._ensure()
            crop = _crop_face(frame, bbox)
            if crop is None:
                return ExpressionObservation(label="neutral", confidence=0.0)
            rgb = crop[:, :, ::-1]
            result = self._model.top_emotion(rgb)
            if not result or not result[0]:
                return ExpressionObservation(label="neutral", confidence=0.0)
            label, score = result
            norm = _LABEL_ALIASES.get(str(label).lower(), "neutral")
            v, a = _VA_MAP.get(norm, (0.0, 0.2))
            return ExpressionObservation(
                label=norm, confidence=float(score or 0.5),
                valence=v, arousal=a,
            )
        except Exception as exc:
            logger.debug("FER 识别失败：%s", exc)
            return ExpressionObservation(label="neutral", confidence=0.0)


class NullExpressionRecognizer:
    """空实现 —— 表情信息缺失时由 ASR 情感标签补位。"""

    name = "none"

    def is_available(self) -> bool:
        return True

    def analyze(self, frame, bbox: BBox) -> ExpressionObservation:
        return ExpressionObservation(label="neutral", confidence=0.0)


def _crop_face(frame, bbox: BBox):
    """裁剪人脸区域，带边界保护。"""
    try:
        h, w = frame.shape[:2]
        x1 = max(0, bbox.x)
        y1 = max(0, bbox.y)
        x2 = min(w, bbox.x2)
        y2 = min(h, bbox.y2)
        if x2 - x1 < 10 or y2 - y1 < 10:
            return None
        return frame[y1:y2, x1:x2]
    except Exception:
        return None
