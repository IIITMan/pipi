"""Affect 天气 —— 连续的身体天气，双时间尺度（PULSE_DESIGN §4.4）。

base 底色：慢漂移（回归平静 τ=15min）；pulse 波澜：事件冲击、指数衰减（τ=60s）。
天气标签由 (base, pulse) 映射：温暖/沉重/紧绷/松弛。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pipi.pulse.config import AFFECT


@dataclass
class Weather:
    base: float
    pulse: float
    label: str


class Affect:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or AFFECT
        self.cfg = cfg
        self.baseline = float(cfg.get("baseline", 0.0))
        self.base = self.baseline
        self.pulse = 0.0

    # ------------------------------------------------------------------

    def update(self, dt: float) -> Weather:
        # 底色缓慢回归平静基线（解析解，任意 dt 稳定）
        tau_b = self.cfg["base_regress_tau_s"]
        self.base = self.baseline + (self.base - self.baseline) * math.exp(
            -dt / max(tau_b, 1e-6))
        # 波澜指数衰减
        tau_p = self.cfg["pulse_decay_tau_s"]
        self.pulse *= math.exp(-dt / max(tau_p, 1e-6))
        return Weather(
            base=round(self.base, 3),
            pulse=round(self.pulse, 3),
            label=self._label(),
        )

    def push_base(self, v: float) -> None:
        """心境底色事件（对话开心/低落、重聚振奋）。"""
        self.base = max(-1.0, min(1.0, self.base + v))

    def push_pulse(self, v: float) -> None:
        """情绪波澜事件（惊吓/惊喜/受挫）。"""
        self.pulse = max(0.0, min(1.0, self.pulse + v))

    # ------------------------------------------------------------------

    def _label(self) -> str:
        """天气标签（唯一权威映射，loop 与 runtime 都读这里）。
        pulse=波澜强度（0~1，无方向）；base=心境底色（-1~1，有正负）。
        标定（baseline=0.35 平静=多云）：
          强波澜(惊吓) → 阵雨/雷雨；开心 → 晴；小委屈/生气 → 阴；
          主人难过/连续负面 → 小雨；极度糟糕 → 雷雨。"""
        if self.pulse >= 0.60:
            return "雷雨" if self.base < 0.0 else "阵雨"
        if self.base > 0.45:
            return "晴"
        if self.base > 0.25:
            return "多云"
        if self.base > 0.05:
            return "阴"
        if self.base > -0.15:
            return "小雨"
        return "雷雨"
