"""Will 意愿 —— 决定/尝试/被打断/改主意（PULSE_DESIGN §4.5）。

意图循环：decide（张力场 argmax）→ 执行 → 被打断挂起重试 → 张力场变化改主意
→ 失败放弃。低张力时段随机做"无用的事"（伸懒腰/哼歌/发呆）。
P0 验证主要观测：主导意图切换（改主意）、挣扎时意图不稳定、无用的事出现。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from pipi.pulse.config import WILL
from pipi.pulse.organs.tension import TensionSnapshot

# 意图状态
IDLE = "idle"
ATTEMPTING = "attempting"
INTERRUPTED = "interrupted"
ABANDONED = "abandoned"

# P2：意图的理由（给表达层/日志，说明"为什么此刻想做这个"）
REASON = {
    "loneliness": "有点想你",
    "curiosity": "外面好像有动静",
    "sleepiness": "眼皮好重",
    "security": "想确认周围安不安全",
    "attachment": "想靠着你",
    "useless": "没什么要紧事",
}


@dataclass
class Intent:
    kind: str            # 波名 或 "useless"
    action: str | None = None
    state: str = IDLE
    reason: str = ""


class Will:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or WILL
        self.cfg = cfg
        self.state = IDLE
        self.current: Intent | None = None
        self._retries = 0
        self._rng = random.Random(11)

    # ------------------------------------------------------------------

    def decide(self, snap: TensionSnapshot) -> Intent | None:
        """每行为 tick（5s）调用一次：行动张力场 → 意图。

        挣扎度极高（两强相争）→ 意图不稳定（改主意）；低张力 → 无用的事。
        意图是"当前想做什么"，由**行动波**（排除 security 背景安全感）主导；
        改主意前持续成立（loop 以 will.current 为唯一权威）。
        """
        if self.state == INTERRUPTED:
            self._retries += 1
            if self._retries > self.cfg["attempt_max_retry"]:
                self.state = ABANDONED
                self.current = Intent(kind="abandoned", action=None,
                                      state=self.state,
                                      reason="被打断太多次，算了")
                return self.current
            return self.current  # 挂起重试

        # 行动波：排除 security 背景波（安全感不是行动意图）
        action = {k: v for k, v in snap.waves.items() if k != "security"}
        action_max = max(action.values()) if action else 0.0
        if action_max < 0.45:
            # 低张力时段：偶尔做无用的事；否则意图回落 → idle
            if self._rng.random() < self.cfg["useless_prob"]:
                motion = self._rng.choice(self.cfg["useless_motions"])
                self.current = Intent(kind="useless", action=motion,
                                      state=ATTEMPTING, reason=REASON["useless"])
                return self.current
            self.current = None
            return None

        dominant = max(action, key=action.get)
        if self.current is None or self.current.kind != dominant:
            # 主导行动波变化 → 改主意
            self.current = Intent(kind=dominant, state=ATTEMPTING,
                                  reason=REASON.get(dominant, ""))
            self._retries = 0
            return self.current
        return None   # 意图未变（持续成立）

    def interrupt(self) -> None:
        """被打断：意图挂起（至多重试 N 次）。"""
        if self.current is not None:
            self.state = INTERRUPTED

    def settle(self, result: bool) -> str:
        """尝试结果：成功 → 回到 IDLE；失败 → 放弃/委屈（由仿真计入 Affect）。"""
        self.state = IDLE
        self.current = None
        self._retries = 0
        return "satisfied" if result else "frustrated"
