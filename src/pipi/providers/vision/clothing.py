"""衣着颜色识别 —— 纯 OpenCV + 色彩聚类，无需额外模型。

这是最稳的一个子能力：只要有人脸框就能估出躯干区域，
再对那个区域做颜色统计，取主色并映射到中文色名。
"""

from __future__ import annotations

import logging
from collections import Counter

from pipi.domain.vision import BBox, ClothingObservation

logger = logging.getLogger(__name__)

# RGB 参考色 → 中文色名（用于把颜色说人话）
_REFERENCE_COLORS: list[tuple[str, tuple[int, int, int]]] = [
    ("黑", (20, 20, 20)),
    ("白", (240, 240, 240)),
    ("灰", (128, 128, 128)),
    ("深灰", (80, 80, 80)),
    ("浅灰", (190, 190, 190)),
    ("红", (200, 40, 40)),
    ("酒红", (120, 30, 45)),
    ("粉", (230, 160, 180)),
    ("橙", (230, 130, 50)),
    ("黄", (230, 210, 60)),
    ("绿", (60, 160, 70)),
    ("墨绿", (30, 80, 50)),
    ("蓝", (60, 100, 200)),
    ("深蓝", (30, 45, 100)),
    ("天蓝", (120, 180, 230)),
    ("紫", (140, 80, 180)),
    ("棕", (120, 80, 50)),
    ("卡其", (190, 175, 140)),
    ("米色", (230, 220, 195)),
]


def _nearest_color_name(rgb: tuple[int, int, int]) -> str:
    """找最接近的中文色名。"""
    best, best_d = "灰", float("inf")
    r, g, b = rgb
    for name, (rr, gg, bb) in _REFERENCE_COLORS:
        d = (r - rr) ** 2 + (g - gg) ** 2 + (b - bb) ** 2
        if d < best_d:
            best_d, best = d, name
    return best


class ColorClothingAnalyzer:
    """基于颜色统计的衣着分析。"""

    name = "color_kmeans"

    def __init__(self, top_k: int = 3, min_ratio: float = 0.12):
        self.top_k = top_k
        self.min_ratio = min_ratio

    def is_available(self) -> bool:
        try:
            import numpy  # noqa: F401
            return True
        except ImportError:
            return False

    def analyze(self, frame, person_bbox: BBox) -> ClothingObservation:
        """分析躯干区域的主色。"""
        try:
            import numpy as np
        except ImportError:
            return ClothingObservation()

        try:
            h, w = frame.shape[:2]
            # 人脸框 → 躯干 ROI
            face_box = person_bbox
            roi = face_box.torso_roi(h, w)
            if roi.w <= 2 or roi.h <= 2:
                return ClothingObservation()

            crop = frame[roi.y:roi.y2, roi.x:roi.x2]
            if crop.size == 0:
                return ClothingObservation()

            # 转 RGB
            if crop.shape[-1] == 3:
                # OpenCV 是 BGR
                rgb = crop[:, :, ::-1].reshape(-1, 3)
            else:
                return ClothingObservation()

            # 过滤掉极暗/极亮（头发、背景过曝）以提升衣物识别
            lum = rgb.mean(axis=1)
            mask = (lum > 25) & (lum < 245)
            if mask.sum() < 50:
                mask = np.ones(len(rgb), dtype=bool)
            pixels = rgb[mask]

            # 量化到 32 级色阶，统计众数（比 KMeans 快且够用）
            quant = (pixels // 32).astype(np.int32)
            keys = quant[:, 0] * 64 + quant[:, 1] * 8 + quant[:, 2]
            counts = Counter(keys.tolist())
            total = sum(counts.values()) or 1

            colors: list[tuple[int, int, int]] = []
            names: list[str] = []
            ratios: list[float] = []

            for key, cnt in counts.most_common(self.top_k):
                ratio = cnt / total
                if ratio < self.min_ratio and colors:
                    break
                # 还原颜色（取量化中心）
                k = key
                qb, qg, qr = k % 8, (k // 8) % 8, k // 64
                rgb_tuple = (qr * 32 + 16, qg * 32 + 16, qb * 32 + 16)
                colors.append(rgb_tuple)
                names.append(_nearest_color_name(rgb_tuple))
                ratios.append(round(ratio, 3))

            return ClothingObservation(
                dominant_colors=colors, color_names=names,
                color_ratios=ratios, region="torso",
            )
        except Exception as exc:
            logger.debug("衣着分析失败：%s", exc)
            return ClothingObservation()
