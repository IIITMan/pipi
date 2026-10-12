"""PULSE 器官故障三级隔离（PULSE_REVIEW_TODO P0-2，借 CE fault_handling 思路）。

三级防护：
- 级1 单次异常恢复：NaN/inf/越界输出被拒绝——器官沿用最后有效状态，记错误日志；
- 级2 器官隔离：窗口内连续 N 次非法 → 隔离该器官（其输出不再被采纳），
    连续正常 M 次后自动恢复（invalid_output_isolation）；
- 级3 关键控制安全态：tension 是动作意图的唯一来源（critical 器官）——
    被隔离后 Will 意图决策短路（不产生活动意图），身体表达自然回安全态。

dt 治理（30-90 天仿真显式处理）：sanitize_dt 拒绝负 dt（时间回退，
tick_dt_reject_negative）；超大 dt（时间跳变）截断至上限并记日志——
现有仿真逐拍 dt ≤ 60s，上限 3600s 只挡真实异常。
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger("pulse.fault")


def is_bad(v: float) -> bool:
    """NaN / ±inf / 非有限数。"""
    return not math.isfinite(v)


class OrganGuard:
    """按器官的故障计数与隔离窗口（级2）+ dt 治理。"""

    def __init__(self, isolation_window: int = 5,
                 recovery_window: int = 10, max_dt: float = 3600.0):
        self.isolation_window = isolation_window   # 连续 N 次非法 → 隔离
        self.recovery_window = recovery_window     # 连续 M 次正常 → 恢复
        self.max_dt = max_dt                       # 单拍 dt 上限（秒）
        self._fails: dict[str, int] = {}
        self._ok_streak: dict[str, int] = {}
        self.isolated: set[str] = set()
        self.log: list[dict] = []                  # 故障审计（调试/报告）

    # ------------------------------------------------------------- 级2 隔离
    def record(self, organ: str, ok: bool) -> None:
        """器官本拍输出是否合法。连续非法达窗口 → 隔离；连续正常 → 恢复。"""
        if ok:
            self._ok_streak[organ] = self._ok_streak.get(organ, 0) + 1
            self._fails[organ] = 0
            if organ in self.isolated \
                    and self._ok_streak[organ] >= self.recovery_window:
                self.isolated.discard(organ)
                self.log.append({"organ": organ, "event": "recovered"})
                logger.warning("PULSE 器官恢复：%s", organ)
        else:
            self._fails[organ] = self._fails.get(organ, 0) + 1
            self._ok_streak[organ] = 0
            if self._fails[organ] >= self.isolation_window \
                    and organ not in self.isolated:
                self.isolated.add(organ)
                self.log.append({"organ": organ, "event": "isolated",
                                 "fails": self._fails[organ]})
                logger.warning("PULSE 器官隔离（连续 %d 次非法）：%s",
                               self._fails[organ], organ)

    def is_isolated(self, organ: str) -> bool:
        return organ in self.isolated

    # ------------------------------------------------------------- dt 治理
    def sanitize_dt(self, dt: float) -> float:
        """拒绝负 dt（时间回退）；超大 dt（时间跳变）截断至上限并告警。"""
        if is_bad(dt) or dt < 0.0:
            self.log.append({"organ": "clock", "event": "dt_negative",
                             "dt": dt})
            logger.warning("PULSE 拒绝异常 dt（%r）→ 0，仿真不回退", dt)
            return 0.0
        if dt > self.max_dt:
            self.log.append({"organ": "clock", "event": "dt_jump",
                             "dt": dt, "clamped": self.max_dt})
            logger.warning("PULSE dt 跳变（%.1fs）截断至 %.1fs",
                           dt, self.max_dt)
            return self.max_dt
        return dt

    # ------------------------------------------------------------- 级1 校验
    def finite(self, *vals: float) -> bool:
        return all(not is_bad(v) for v in vals)

    def snapshot(self) -> dict:
        """故障状态快照（监控/报告用）。"""
        return {
            "isolated": sorted(self.isolated),
            "fails": dict(self._fails),
            "log_len": len(self.log),
        }
