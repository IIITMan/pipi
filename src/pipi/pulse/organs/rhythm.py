"""Rhythm 节律 —— 真实运行的呼吸/心跳波形（PULSE_DESIGN §4.1）。

身心耦合：arousal（来自 Tension 总和与外部刺激）驱动呼吸加快加深；
呼吸深浅经 Affect 底色回灌（本文件只负责波形，回灌在仿真中体现）。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from pipi.pulse.config import RHYTHM


@dataclass
class BreathSample:
    phase: float
    depth: float
    rate_bpm: float
    heartbeat_bpm: int


class Rhythm:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or RHYTHM
        self.cfg = cfg
        self.phase = 0.0
        self.rate_bpm = float(cfg["base_rate_bpm"])
        self.depth = float(cfg["base_depth"])
        self._arousal = 0.0
        self._burst = 0.0            # 惊吓/惊喜呼吸脉冲（独立指数衰减）
        self._rng = random.Random(7)  # 确定性噪声

    # ------------------------------------------------------------------

    def tick(self, dt: float, arousal_target: float,
             sleeping: bool = False) -> BreathSample:
        """解析解一阶滞后（任意 dt 稳定，避免显式欧拉在 dt>>τ 时发散）。

        惊吓脉冲 burst 独立于背景 arousal，按 burst_tau 指数衰减——受惊后
        呼吸急促并缓缓回落（20s 尺度），不再被下一帧背景一锤抹平。
        sleeping 时呼吸更深、波形更平稳（入睡≠浅呼吸）。
        """
        target = max(0.0, min(1.0, arousal_target))
        tau = self.cfg["arousal_tau_s"]

        def _toward(cur: float, goal: float, t: float) -> float:
            return goal + (cur - goal) * math.exp(-t / max(tau, 1e-6))

        self._arousal = _toward(self._arousal, target, dt)
        self._burst *= math.exp(-dt / max(self.cfg["burst_tau_s"], 1e-6))

        base = self.cfg["base_rate_bpm"]
        top = self.cfg["max_rate_bpm"]
        target_rate = base + (top - base) * self._arousal
        self.rate_bpm = _toward(self.rate_bpm, target_rate, dt)
        self.rate_bpm += self._burst * self.cfg["burst_rate_bpm"]

        self.depth = self.cfg["base_depth"] + (1.0 - self.cfg["base_depth"]) \
            * self._arousal
        self.depth += self._burst * self.cfg["burst_depth_add"]
        if sleeping:
            self.depth += self.cfg["sleep_depth_boost"]

        self.phase = (self.phase + dt * self.rate_bpm / 60.0) % 1.0

        wave = self.depth * math.sin(2.0 * math.pi * self.phase)
        if self.cfg["noise_amp"] > 0:
            amp = self.cfg["noise_amp"] * (self.cfg["sleep_noise_scale"]
                                           if sleeping else 1.0)
            wave += self._rng.uniform(-1, 1) * amp
        return BreathSample(
            phase=self.phase,
            depth=self.depth,
            rate_bpm=round(self.rate_bpm, 2),
            heartbeat_bpm=int(self.rate_bpm * self.cfg["heartbeat_ratio"]),
        )

    def burst(self, amp: float) -> None:
        """惊吓/惊喜：瞬时呼吸脉冲（指数衰减，取最大冲击）。"""
        self._burst = max(self._burst, max(0.0, min(1.0, amp)))

    def trigger_arousal(self, pulse: float) -> None:
        """兼容旧调用：惊吓脉冲走 burst（P0 脚本仍可用）。"""
        self.burst(pulse)

    @property
    def arousal(self) -> float:
        return round(self._arousal, 3)
