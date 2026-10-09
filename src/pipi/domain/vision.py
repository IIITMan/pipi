"""视觉领域模型。

关键设计：视觉结果**不直接给 LLM 原始 JSON**，
而是由 perception 层翻译成一句自然语言（见 to_hint），
否则模型会用"字段感"的语气回复，AI 味立刻暴露。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BBox:
    """人脸/人体检测框（像素坐标）。"""

    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return self.w * self.h

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def iou(self, other: "BBox") -> float:
        ix1, iy1 = max(self.x, other.x), max(self.y, other.y)
        ix2, iy2 = min(self.x2, other.x2), min(self.y2, other.y2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    def torso_roi(self, frame_h: int, frame_w: int) -> "BBox":
        """从人脸框估算躯干区域 —— 用于衣着颜色分析。

        经验比例：躯干在脸部下方，宽度约为脸宽的 2.5 倍。
        """
        tw = int(self.w * 2.5)
        th = int(self.h * 3.0)
        tx = self.x + self.w // 2 - tw // 2
        ty = self.y2 + int(self.h * 0.3)
        tx = max(0, min(tx, frame_w - 1))
        ty = max(0, min(ty, frame_h - 1))
        tw = min(tw, frame_w - tx)
        th = min(th, frame_h - ty)
        return BBox(tx, ty, tw, th)


@dataclass
class FaceObservation:
    """一张人脸的观测结果。"""

    bbox: BBox
    person_id: str | None = None        # 识别出的身份，None = 陌生人
    identity_confidence: float = 0.0
    is_owner: bool = False
    landmarks: list[tuple[int, int]] | None = None


@dataclass
class ExpressionObservation:
    """表情识别结果。"""

    label: str                          # happy/sad/angry/neutral/surprised/fear/disgust
    confidence: float
    valence: float = 0.0                # 情绪效价 −1..1（负=不快）
    arousal: float = 0.0                # 唤醒度 0..1（高=激动）

    # 表情 → 中文描述（注入 LLM 用）
    LABEL_CN: dict[str, str] = field(default_factory=lambda: {
        "happy": "看起来挺高兴",
        "sad": "看起来不太开心",
        "angry": "好像有点生气",
        "surprised": "看着有点惊讶",
        "fear": "好像有点紧张",
        "disgust": "有点嫌弃的样子",
        "neutral": "看着挺平静",
    }, repr=False)

    def describe_cn(self) -> str:
        return self.LABEL_CN.get(self.label, f"表情是{self.label}")


@dataclass
class ClothingObservation:
    """衣着颜色观测结果。"""

    dominant_colors: list[tuple[int, int, int]] = field(default_factory=list)  # RGB top-k
    color_names: list[str] = field(default_factory=list)                        # 中文色名
    color_ratios: list[float] = field(default_factory=list)
    region: str = "torso"

    def primary_color(self) -> str | None:
        return self.color_names[0] if self.color_names else None


@dataclass
class VisionObservation:
    """一帧画面的完整感知结果。"""

    timestamp: float = 0.0
    frame_id: int = 0
    faces: list[FaceObservation] = field(default_factory=list)
    expressions: dict[str, ExpressionObservation] = field(default_factory=dict)
    clothing: dict[str, ClothingObservation] = field(default_factory=dict)
    person_count: int = 0
    owner_present: bool = False
    owner_newly_arrived: bool = False   # 由 pipeline 对比上一帧算出
    owner_just_left: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_stranger(self) -> bool:
        return any(f.person_id is None for f in self.faces)

    @property
    def extra_person_count(self) -> int:
        return max(0, self.person_count - (1 if self.owner_present else 0))

    def to_hint(self, owner_name: str = "主人") -> str | None:
        """翻译成一句自然语言，注入 LLM 的 system message。

        这是"看到画面"能力的落地方式 —— 输出形如：
          [你看到的] 主人刚回来了，他现在穿着灰色的衣服，看起来挺高兴
        """
        parts: list[str] = []

        # 云端多模态识别结果优先（本地只做检测/跟随，复杂理解上云）
        vlm = (self.raw or {}).get("_vlm")
        if vlm:
            return "[你看到的] " + str(vlm)

        if self.owner_newly_arrived:
            parts.append(f"{owner_name}刚回来了")
        elif self.owner_present:
            parts.append(f"{owner_name}在屋里")

        if self.owner_present:
            owner_key = "owner"
            if cloth := self.clothing.get(owner_key):
                if color := cloth.primary_color():
                    parts.append(f"他现在穿着{color}的衣服")
            if expr := self.expressions.get(owner_key):
                if expr.label != "neutral":
                    parts.append(expr.describe_cn())

        if self.extra_person_count > 0:
            parts.append(f"屋里还有 {self.extra_person_count} 个别人")

        if not parts:
            return None
        return "[你看到的] " + "，".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "person_count": self.person_count,
            "owner_present": self.owner_present,
            "owner_newly_arrived": self.owner_newly_arrived,
            "owner_just_left": self.owner_just_left,
            "faces": [
                {
                    "person_id": f.person_id,
                    "is_owner": f.is_owner,
                    "confidence": f.identity_confidence,
                    "bbox": [f.bbox.x, f.bbox.y, f.bbox.w, f.bbox.h],
                }
                for f in self.faces
            ],
            "expressions": {k: v.label for k, v in self.expressions.items()},
            "clothing": {
                k: {"colors": v.color_names, "ratios": v.color_ratios}
                for k, v in self.clothing.items()
            },
        }
