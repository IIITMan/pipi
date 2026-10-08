"""Tension 潮汐 —— 会涨落、有惯性、互相竞争的驱力波（PULSE_DESIGN §4.3）。

五波：loneliness / curiosity / sleepiness / security / attachment。
每波向情境 target 一阶回归（惯性 k 决定"性格"），事件冲击 = 瞬时涟漪
（由回归自然指数回落，等价文档的 ripple 衰减，简化实现）。
struggle（挣扎度）= 1 − |top1 − top2|：两强接近即"内心挣扎"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipi.pulse.config import TENSION

WAVES = ("loneliness", "curiosity", "sleepiness", "security", "attachment")


@dataclass
class TensionSnapshot:
    dominant: str
    struggle: float
    waves: dict[str, float] = field(default_factory=dict)


class TensionField:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or TENSION
        self.cfg = cfg
        self.values: dict[str, float] = {w: 0.2 for w in WAVES}
        self._ripples: dict[str, list[float]] = {w: [] for w in WAVES}

    # ------------------------------------------------------------------

    def update(self, dt: float, targets: dict[str, float],
               sleeping: bool = False) -> TensionSnapshot:
        for w in WAVES:
            conf = self.cfg["waves"][w]
            k = conf["k"]
            target = max(conf["min"], min(conf["max"], targets.get(w, 0.2)))
            self.values[w] += (target - self.values[w]) * k * dt
            # 涟漪：瞬时注入值由回归自然衰减（指数）
            self.values[w] = max(conf["min"], min(conf["max"], self.values[w]))

        # 挣扎度：前两名接近度
        ranked = sorted(self.values.values(), reverse=True)
        top1, top2 = ranked[0], ranked[1]
        struggle = 1.0 - abs(top1 - top2)
        # 平稳睡眠不是挣扎：安全波高位（无警报）时压低挣扎度；
        # 被惊醒（security 崩塌）时挣扎如实，让"吓醒"仍可观察
        if sleeping and self.values["security"] > 0.45:
            struggle = min(struggle, 0.30)
        dominant = max(self.values, key=self.values.get)
        return TensionSnapshot(
            dominant=dominant,
            struggle=round(struggle, 3),
            waves={w: round(v, 3) for w, v in self.values.items()},
        )

    def push(self, wave: str, magnitude: float) -> None:
        """事件涟漪：瞬时加值（随后被惯性回归拉回 target）。"""
        if wave not in self.values:
            return
        conf = self.cfg["waves"][wave]
        self.values[wave] = max(conf["min"], min(conf["max"],
                                                 self.values[wave] + magnitude))

    def snapshot(self, sleeping: bool = False) -> TensionSnapshot:
        ranked = sorted(self.values.values(), reverse=True)
        struggle = 1.0 - abs(ranked[0] - ranked[1])
        if sleeping and self.values["security"] > 0.45:
            struggle = min(struggle, 0.30)
        return TensionSnapshot(
            dominant=max(self.values, key=self.values.get),
            struggle=round(struggle, 3),
            waves={w: round(v, 3) for w, v in self.values.items()},
        )

    @property
    def total(self) -> float:
        return sum(self.values.values()) / len(WAVES)
