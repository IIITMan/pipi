"""Anticipation 预感 —— 预测误差驱动惊讶/好奇/安心（PULSE_DESIGN §4.2）。

预测表按键维护 expected/confidence；surprise = |obs−expected|/scale × (1−conf)，
clamp 0..1。学习：滑动平均更新 expected，conf 随样本升到上限。
P0 用预置作息键；运行时（P2）由印记阶段写入新键。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipi.pulse.config import ANTICIPATION


@dataclass
class Surprise:
    level: float
    kind: str            # calm | curious | startle
    key: str = ""


class Anticipation:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or ANTICIPATION
        self.cfg = cfg
        # key -> {"expected": float, "confidence": float, "n": int, "scale": float}
        self.predictions: dict[str, dict] = {}
        for key, spec in cfg["keys"].items():
            self.predictions[key] = {
                "expected": spec["expected"],
                "confidence": cfg["conf_init"],
                "n": 1,
                "scale": spec["scale"],
            }

    # ------------------------------------------------------------------

    def update(self, key: str, observed: float) -> Surprise:
        pred = self.predictions.get(key)
        if pred is None:
            return Surprise(level=0.0, kind="calm", key=key)   # 未知键不惊讶
        exp = pred["expected"]
        scale = max(pred["scale"], 1e-6)
        rel = abs(observed - exp) / scale
        surprise = min(1.0, rel * (1.0 - pred["confidence"]))

        if surprise > self.cfg["startle_threshold"]:
            kind = "startle"
        elif surprise > self.cfg["curious_threshold"]:
            kind = "curious"
        else:
            kind = "calm"
        return Surprise(level=round(surprise, 3), kind=kind, key=key)

    def learn(self, key: str, observed: float) -> None:
        """印记：滑动平均更新期望，confidence 随样本上升。"""
        pred = self.predictions.setdefault(key, {
            "expected": observed,
            "confidence": self.cfg["conf_init"],
            "n": 0,
            "scale": 1.0,
        })
        pred["n"] += 1
        n = pred["n"]
        pred["expected"] += (observed - pred["expected"]) / n
        pred["confidence"] = min(self.cfg["max_confidence"],
                                 pred["confidence"] + self.cfg["conf_step"])

    def expected(self, key: str) -> float | None:
        """当前对某键的期望值（P2：提前/延迟判定的依据）。"""
        pred = self.predictions.get(key)
        return pred["expected"] if pred else None
