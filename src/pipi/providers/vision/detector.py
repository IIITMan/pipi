"""人脸检测与识别。

检测器优先级：
  1. OpenCV YuNet（最轻，ONNX，无需额外依赖，精度够用）
  2. MediaPipe
  3. OpenCV Haar 级联（最古老，兜底）

识别器：
  InsightFace（arcface 特征）—— 装不上就只做"有人/没人"判断。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipi.domain.vision import BBox

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------

class YuNetDetector:
    """OpenCV 内置的 YuNet 人脸检测器（轻量、快、准）。"""

    name = "yunet"

    def __init__(self, score_threshold: float = 0.6, model_path: str = ""):
        self.score_threshold = score_threshold
        self.model_path = model_path
        self._detector: Any = None

    def is_available(self) -> bool:
        try:
            import cv2
            return hasattr(cv2, "FaceDetectorYN")
        except ImportError:
            return False

    def _ensure(self, frame_shape: tuple[int, int]) -> None:
        if self._detector is not None:
            return
        import cv2
        h, w = frame_shape
        path = self.model_path
        if not path or not Path(path).exists():
            # OpenCV 自带模型路径（不同版本位置不同，尝试常见位置）
            candidates = [
                "/usr/share/opencv4/models/face_detection_yunet_2023mar.onnx",
                str(Path.home() / ".cache/opencv/face_detection_yunet_2023mar.onnx"),
            ]
            path = next((c for c in candidates if Path(c).exists()), "")
        if path and Path(path).exists():
            self._detector = cv2.FaceDetectorYN.create(path, "", (w, h),
                                                       self.score_threshold)
        else:
            # 没有 onnx 模型 → 退到 Haar
            logger.debug("YuNet 模型缺失，退到 Haar 级联")
            self._detector = _HaarFallback()

    def detect(self, frame) -> list[BBox]:
        try:
            import cv2
        except ImportError:
            return []
        h, w = frame.shape[:2]
        self._ensure((h, w))

        if isinstance(self._detector, _HaarFallback):
            return self._detector.detect(frame)

        try:
            self._detector.setInputSize((w, h))
            _, faces = self._detector.detect(frame)
            if faces is None:
                return []
            out = []
            for f in faces:
                x, y, fw, fh = f[:4]
                out.append(BBox(int(x), int(y), int(fw), int(fh)))
            return out
        except Exception as exc:
            logger.debug("YuNet 检测失败：%s", exc)
            return []


class HaarDetector:
    """OpenCV Haar 级联 —— 最老的方案，但永远可用。"""

    name = "opencv_haar"

    def __init__(self):
        self._cascade = None

    def is_available(self) -> bool:
        try:
            import cv2
            return hasattr(cv2, "CascadeClassifier")
        except ImportError:
            return False

    def detect(self, frame) -> list[BBox]:
        import cv2
        if self._cascade is None:
            path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self._cascade = cv2.CascadeClassifier(path)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._cascade.detectMultiScale(gray, 1.1, 5, minSize=(40, 40))
        return [BBox(int(x), int(y), int(w), int(h)) for x, y, w, h in faces]


class _HaarFallback:
    """YuNet 模型缺失时的内部兜底。"""

    def __init__(self):
        self._haar = HaarDetector()

    def detect(self, frame) -> list[BBox]:
        try:
            return self._haar.detect(frame)
        except Exception:
            return []


class MediaPipeDetector:
    """MediaPipe 人脸检测（如果装了）。"""

    name = "mediapipe"

    def __init__(self):
        self._mp = None

    def is_available(self) -> bool:
        try:
            import mediapipe  # noqa: F401
            return True
        except ImportError:
            return False

    def detect(self, frame) -> list[BBox]:
        import mediapipe as mp
        if self._mp is None:
            self._mp = mp.solutions.face_detection.FaceDetection(
                model_selection=0, min_detection_confidence=0.6
            )
        h, w = frame.shape[:2]
        rgb = frame[:, :, ::-1]
        res = self._mp.process(rgb)
        if not res.detections:
            return []
        out = []
        for d in res.detections:
            bb = d.location_data.relative_bounding_box
            out.append(BBox(
                int(bb.xmin * w), int(bb.ymin * h),
                int(bb.width * w), int(bb.height * h),
            ))
        return out


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------

class InsightFaceRecognizer:
    """InsightFace 人脸识别 —— 用于"认主"。"""

    name = "insightface"

    def __init__(self, model_name: str = "buffalo_sc", match_threshold: float = 0.45):
        self.model_name = model_name
        self.match_threshold = match_threshold
        self._app: Any = None
        self._enrolled: dict[str, list[float]] = {}

    def is_available(self) -> bool:
        try:
            import insightface  # noqa: F401
            return True
        except ImportError:
            logger.info("未安装 InsightFace，人脸识别降级为'有人/没人'判断")
            return False

    def _ensure(self) -> None:
        if self._app is not None:
            return
        from insightface.app import FaceAnalysis
        self._app = FaceAnalysis(name=self.model_name,
                                 providers=["CPUExecutionProvider"])
        self._app.prepare(ctx_id=-1, det_size=(640, 640))

    def enroll(self, person_id: str, frames: list, is_owner: bool = False) -> list[float]:
        """注册人脸，返回特征向量。"""
        self._ensure()
        import numpy as np
        embs = []
        for frame in frames:
            try:
                faces = self._app.get(frame)
                if faces:
                    embs.append(faces[0].normed_embedding)
            except Exception:
                continue
        if not embs:
            return []
        mean = np.mean(embs, axis=0)
        norm = np.linalg.norm(mean)
        emb = (mean / norm).tolist() if norm > 0 else mean.tolist()
        self._enrolled[person_id] = emb
        return emb

    def identify(self, frame, bbox: BBox) -> tuple[str | None, float]:
        """识别这是谁。返回 (person_id, 置信度)。"""
        if not self._enrolled:
            return None, 0.0
        self._ensure()
        try:
            import numpy as np
            faces = self._app.get(frame)
            if not faces:
                return None, 0.0
            # 取与 bbox 最近的人脸
            best_face, best_iou = None, 0.0
            for f in faces:
                fb = f.bbox
                cur = BBox(int(fb[0]), int(fb[1]), int(fb[2] - fb[0]), int(fb[3] - fb[1]))
                iou = cur.iou(bbox)
                if iou > best_iou:
                    best_iou, best_face = iou, f
            if best_face is None:
                return None, 0.0

            emb = best_face.normed_embedding
            best_id, best_sim = None, 0.0
            for pid, ref in self._enrolled.items():
                sim = float(np.dot(emb, np.array(ref)))
                if sim > best_sim:
                    best_sim, best_id = sim, pid
            if best_sim >= self.match_threshold:
                return best_id, best_sim
            return None, best_sim
        except Exception as exc:
            logger.debug("人脸识别失败：%s", exc)
            return None, 0.0

    def load_enrolled(self, faces: list[dict]) -> None:
        """从持久化数据恢复已注册人脸。"""
        for f in faces:
            if f.get("embedding"):
                self._enrolled[f["person_id"]] = f["embedding"]

    def detect_faces(self, frame) -> list[BBox]:
        """用 InsightFace 自带检测器出人脸框。

        YuNet 缺模型文件、Haar 在 opencv 5.0 下不可用 —— 识别器的
        检测是最可靠的兜底（process_frame 检出 0 脸时调用）。
        """
        try:
            self._ensure()
            out = []
            for f in self._app.get(frame):
                x1, y1, x2, y2 = [int(v) for v in f.bbox[:4]]
                out.append(BBox(x=x1, y=y1, w=max(0, x2 - x1), h=max(0, y2 - y1)))
            return out
        except Exception:
            return []
