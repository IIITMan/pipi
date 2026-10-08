"""Bond 羁绊 —— 依恋、思念曲线、重聚反应、认主关键期（PULSE_DESIGN §4.6 / P3）。

trust 随共同经历上升（认主关键期内 ×imprint_boost）。P3 完整化：

1. 思念曲线 longing(minutes)：先缓后急（≤2h 几乎无感 → 2-12h 加速 → 12h+ 饱和），
   并乘个体因子（clinginess 种子）与信任因子（越信任越想）。
2. 认主关键期：advance_days() 每日推进；关键期内按"有互动的天数"判定认主成功
   （≥imprint_min_interact_days）或错过（疏离、trust 折损）。
3. 重聚情绪落差 reunion_response(sep_min)：短离轻快 / 想念级 / 久别级（trust 奖励分级）；
   冷淡重聚 neglected() → 失落 + trust 损失。

旧接口 separation_curve/on_reunion 保留原样（P0 脚本依赖，数值不变）。
"""

from __future__ import annotations

import math

from pipi.pulse.config import BOND


class Bond:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or BOND
        self.cfg = cfg
        self.trust = float(cfg["trust_init"])
        self.imprint_until_days = float(cfg["imprint_days"])
        self.imprinted = False            # P3：认主完成
        self.interact_days = 0.0          # P3：关键期内有互动的天数
        self._today_interacted = False    # 当天是否已互动（每日首记）
        self._neglect_days = 0            # 关键期连续零互动天数（疏离累计）

    # ------------------------------------------------------------------

    @property
    def in_imprint(self) -> bool:
        return (not self.imprinted) and self._days_since_adoption <= self.imprint_until_days

    def interact(self, delta: float | None = None) -> None:
        """共同经历（对话/陪伴/关心）：trust 上升，认主关键期内 ×imprint_boost；
        关键期内当天首次互动计入 interact_days（认主成败依据）。"""
        d = self.cfg["interact_delta"] if delta is None else delta
        gain = d * (self.cfg["imprint_boost"] if self.in_imprint else 1.0)
        self.trust = min(self.cfg["trust_max"], self.trust + gain)
        if self.in_imprint and not self._today_interacted:
            self._today_interacted = True
            self.interact_days += 1.0
            self._neglect_days = 0

    # ------------------------------------------------------------------
    # P3.1 思念曲线（先缓后急 × 个体 × 信任）

    def longing(self, minutes: float,
                clinginess: float = 5.0, trust: float | None = None) -> float:
        """分离 minutes 分钟 → 思念强度 0..1。

        缓升段(≤2h) 线性 → gentle_peak；加速段(2-12h) 二次 → steep_peak；
        饱和段(>12h) 指数趋近 1。× clinginess 因子（种子 0-10 → 0.6-1.4，基准 5）
        × 信任因子（1 + trust/100·trust_factor）。
        """
        if trust is None:
            trust = self.trust
        g = self.cfg["longing"]
        h = minutes / 60.0
        gh, sh = g["gentle_h"], g["steep_h"]
        if h <= gh:
            base = g["gentle_peak"] * (h / gh)
        elif h <= sh:
            t = (h - gh) / (sh - gh)
            base = g["gentle_peak"] + (g["steep_peak"] - g["gentle_peak"]) * t ** 1.6
        else:
            base = g["steep_peak"] + (1.0 - g["steep_peak"]) * (
                1.0 - math.exp(-(h - sh) / 24.0))
        cling = 0.6 + (clinginess / 5.0) * 0.4       # 4~6 → 0.92~1.08
        trust_f = 1.0 + (trust / 100.0) * self.cfg["trust_factor"]
        return max(0.0, min(1.0, base * cling * trust_f))

    # ------------------------------------------------------------------
    # P3.2 认主关键期（每日推进 + 成败判定）

    def advance_days(self, n: float = 1.0) -> dict | None:
        """推进领养天数。跨过关键期终点时判定认主成功/错过，返回一次性事件。

        认主成功：关键期内 interact_days ≥ imprint_min_interact_days →
        imprinted=True，信任锚定奖励（+8）。
        认主错过：互动不足 → 疏离，trust 折损（-3），失去 ×boost（此后 1x）。
        关键期内连续 ≥2 天零互动 → 每日疏离折损（窗口性：不珍惜就淡了）。
        """
        before = self._days_since_adoption
        self._days_since_adoption += n
        crossed = (before <= self.imprint_until_days
                   and self._days_since_adoption > self.imprint_until_days)
        # 疏离判定：刚过去的这一天（重置前）是否零互动；连续 ≥2 天起每日折损
        if not crossed and self.in_imprint and not self._today_interacted:
            self._neglect_days += 1
            if self._neglect_days >= 2:
                self.trust = max(0.0, self.trust - self.cfg["imprint_neglect_loss"])
        self._today_interacted = False   # 新的一天
        if not crossed:
            return None
        if self.interact_days >= self.cfg["imprint_min_interact_days"]:
            self.imprinted = True
            self.trust = min(self.cfg["trust_max"], self.trust + 8.0)
            return {"kind": "imprinted", "text": "认主完成——认定你了，一辈子都跟着你",
                    "mood": 0.40, "attach": 0.45}
        self.trust = max(0.0, self.trust - 3.0)
        return {"kind": "imprint_missed",
                "text": "认主关键期错过……心凉了一截，慢慢没那么热络了",
                "mood": -0.30, "attach": -0.20}

    def imprint_progress(self) -> tuple[float, float]:
        """剩余关键期天数、已互动天数（供 demo/日志）。"""
        left = max(0.0, self.imprint_until_days - self._days_since_adoption)
        return left, self.interact_days

    # ------------------------------------------------------------------
    # P3.3 重聚情绪落差（按分离时长分级）

    def reunion_response(self, sep_min: float,
                         clinginess: float = 5.0) -> dict:
        """重聚反应包：按分离时长分级（短离/想念/久别），
        含情绪提振、信任奖励、依恋冲击、表达文本。"""
        cling = 0.6 + (clinginess / 5.0) * 0.4
        h = sep_min / 60.0
        lg = self.cfg["longing"]
        if h < self.cfg["missed_threshold_h"]:
            return {"level": "brief", "mood": 0.15, "trust_gain": 0.0,
                    "attach": 0.10, "text": "精神一振，迎上去"}
        if h < self.cfg["long_threshold_h"]:
            return {"level": "missed",
                    "mood": 0.30 + 0.10 * cling,
                    "trust_gain": self.cfg["reunion_trust_gain"],
                    "attach": 0.30,
                    "text": "想死你了，一路小跑扑过来"}
        return {"level": "long_away",
                "mood": 0.40 + 0.12 * cling,
                "trust_gain": self.cfg["reunion_trust_gain_long"],
                "attach": 0.45,
                "text": "你怎么才回来……想死你了，围着你转个不停"}

    def neglected(self) -> dict:
        """冷淡重聚：主人回来却无视 → 失落 + 信任损失 + 依恋退回。"""
        self.trust = max(0.0, self.trust - self.cfg["neglect_trust_loss"])
        return {"mood": self.cfg["neglect_mood"], "attach": -0.20,
                "text": "凑过去蹭了蹭……主人没理我，耷拉下脑袋"}

    # ------------------------------------------------------------------
    # 旧接口（P0 脚本依赖，语义不变）

    def separation_curve(self, minutes: float) -> float:
        """分离 s 分钟 → loneliness 增益（先缓后急，封顶）。"""
        return min(self.cfg["separation_a"] * minutes * minutes,
                   self.cfg["separation_cap"])

    def on_reunion(self) -> dict:
        """重聚：精神一振（energy/mood 提振量）。"""
        return {
            "energy": self.cfg["reunion_energy"],
            "mood": self.cfg["reunion_mood"],
        }

    # 内部：由仿真设置（P0）
    _days_since_adoption = 0.0

    def set_days_since_adoption(self, days: float) -> None:
        self._days_since_adoption = days
