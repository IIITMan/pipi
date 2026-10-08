"""Seed 种子 —— 气质 × 经历 → 不可重置的独特个性（PULSE_DESIGN §4.8）。

五维气质（boldness/curiosity/clinginess/sensitivity/reactivity）基线 5.0；
经历事件以 step × plasticity 漂移，cap_offset ±3 防失控；出生一次不可重置。
"""

from __future__ import annotations

from pipi.pulse.config import SEED


class Seed:
    def __init__(self, cfg: dict | None = None, temperament: dict | None = None):
        cfg = cfg or SEED
        self.cfg = cfg
        if temperament is None:
            self.temperament = {axis: cfg["baseline"] for axis in cfg["axes"]}
        else:
            self.temperament = dict(temperament)

    # ------------------------------------------------------------------

    def drift(self, axis: str, direction: float, step: float | None = None) -> None:
        """经历漂移：特制 += direction × step × plasticity。"""
        if axis not in self.temperament:
            return
        base = self.cfg["baseline"]
        cap = self.cfg["cap_offset"]
        p = self._plasticity()
        s = (step if step is not None else 0.08) * p
        new = self.temperament[axis] + direction * s
        self.temperament[axis] = max(base - cap, min(base + cap, new))

    def _plasticity(self) -> float:
        """可塑性随"周龄"递减（第1周1.0 → 第2周0.7 → 第3周0.5 → 1月后0.3）。
        周龄由 self_depth 推算：越"厚"的自己越难被经历改变（人格渐稳但非冻结）。"""
        depth = max(1, getattr(self, "_depth", 1))
        week = (depth - 1) // 7
        curve = self.cfg["plasticity_curve"]
        idx = min(len(curve) - 1, week)
        return curve[idx]

    def set_depth(self, depth: int) -> None:
        self._depth = depth

    def profile(self) -> dict:
        return {k: round(v, 3) for k, v in self.temperament.items()}
