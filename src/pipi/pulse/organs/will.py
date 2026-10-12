"""Will 意愿 —— 决定/尝试/被打断/改主意（PULSE_DESIGN §4.5）。

意图循环：decide（张力场 argmax）→ 执行 → 被打断挂起重试 → 张力场变化改主意
→ 失败放弃。低张力时段随机做"无用的事"（伸懒腰/哼歌/发呆）。
P0 验证主要观测：主导意图切换（改主意）、挣扎时意图不稳定、无用的事出现。

P0 决策滞回（PULSE_REVIEW_TODO P0-1，借 CE IntentManager 参数）：
  - switch_margin：新意图必须明显领先当前意图才切换（微扰不横跳）
  - min_dwell_ticks + consecutive_to_switch：普通意图最短持续 + 连续占优
    次数（滞回）；urgent_override_threshold：波值大幅突变 = 环境变化，
    立即切换（绕过 dwell/consecutive）
  - 紧急打断（interrupt）与任务完成（settle）同样记录切换原因
  - 每次切换记入 switch_log，switch_stats() 按原因统计——区分
    "合理灵活"（环境变化/紧急/任务完成）与"无意义横跳"（频繁 dominant）
"""

from __future__ import annotations

import random
from collections import Counter
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

# P0-1：切换原因类别（TODO #5：区分"合理灵活"与"无意义横跳"）
SWITCH_REASON = {
    "dominant": "新驱力占优",   # 常规竞争：margin + dwell + consecutive 达标
    "completed": "原任务完成",  # settle 成功
    "abandon": "原任务放弃",    # settle 失败/被打断太多次
    "env": "环境变化",          # 波值突变 ≥ urgent_override_threshold
    "urgent": "紧急打断",       # interrupt()
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
        # P0-1 滞回状态
        self._dwell_ticks = 0          # 当前意图已持续的 decide tick 数
        self._candidate_streak = 0     # 新意图连续占优次数
        self.switch_log: list[dict] = []   # 切换原因审计（含计数）

    # ------------------------------------------------------------------

    def decide(self, snap: TensionSnapshot) -> Intent | None:
        """每行为 tick（5s）调用一次：行动张力场 → 意图。

        挣扎度极高（两强相争）→ 意图不稳定（改主意）；低张力 → 无用的事。
        意图是"当前想做什么"，由**行动波**（排除 security 背景安全感）主导；
        改主意前持续成立（loop 以 will.current 为唯一权威）。

        P0-1 滞回：新意图领先 < switch_margin 不切换；常规竞争需连续占优
        ≥ consecutive_to_switch 且当前意图已持续 ≥ min_dwell_ticks；波值差
        ≥ urgent_override_threshold 视为环境突变，立即切换。
        """
        if self.state == INTERRUPTED:
            self._retries += 1
            if self._retries > self.cfg["attempt_max_retry"]:
                self.state = ABANDONED
                self._log_switch(self.current.kind if self.current else None,
                                 "abandoned", "abandon")
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
                prev = self.current.kind if self.current else None
                self.current = Intent(kind="useless", action=motion,
                                      state=ATTEMPTING, reason=REASON["useless"])
                if prev is not None and prev != "useless":
                    self._log_switch(prev, "useless", "dominant")
                return self.current
            if self.current is not None:
                self._log_switch(self.current.kind, None, "abandon")
                self.current = None
                self._dwell_ticks = 0
                self._candidate_streak = 0
            return None

        dominant = max(action, key=action.get)
        dom_val = action[dominant]

        if self.current is None or self.current.kind != dominant:
            if self.current is None:
                # 无当前意图：直接建立（首次建立不算切换）
                self.current = Intent(kind=dominant, state=ATTEMPTING,
                                      reason=REASON.get(dominant, ""))
                self._dwell_ticks = 0
                self._candidate_streak = 0
                self._retries = 0
                return self.current

            # 滞回判定：新意图 vs 当前意图
            cur_val = action.get(self.current.kind, 0.0)
            diff = dom_val - cur_val
            if diff < self.cfg["switch_margin"]:
                # 领先不足：不切换（取消候选，意图保持）
                self._candidate_streak = 0
                self._dwell_ticks += 1
                return None

            if diff >= self.cfg["urgent_override_threshold"]:
                # 环境重大变化：立即切换（绕过 dwell/consecutive）
                self._switch_to(dominant, "env")
                return self.current

            # 常规竞争：连续占优 + 当前意图持续达标才切换
            self._candidate_streak += 1
            self._dwell_ticks += 1
            if self._candidate_streak >= self.cfg["consecutive_to_switch"] \
                    and self._dwell_ticks >= self.cfg["min_dwell_ticks"]:
                self._switch_to(dominant, "dominant")
            return None   # 尚未达标：保持当前意图（返回 None 不切换）
        else:
            # 意图未变：dwell 累积、候选清零
            self._dwell_ticks += 1
            self._candidate_streak = 0
            return None   # 意图未变（持续成立）

    def _switch_to(self, kind: str, reason: str) -> None:
        prev = self.current.kind if self.current else None
        self.current = Intent(kind=kind, state=ATTEMPTING,
                              reason=REASON.get(kind, ""))
        self._dwell_ticks = 0
        self._candidate_streak = 0
        self._retries = 0
        self._log_switch(prev, kind, reason)

    def _log_switch(self, frm: str | None, to: str | None, reason: str) -> None:
        self.switch_log.append({"from": frm, "to": to, "reason": reason})

    def switch_stats(self) -> dict[str, int]:
        """按切换原因统计（TODO #5 验收）：区分合理灵活与无意义横跳。"""
        return dict(Counter(e["reason"] for e in self.switch_log))

    def interrupt(self) -> None:
        """被打断：意图挂起（至多重试 N 次），记录紧急打断原因。"""
        if self.current is not None:
            self.state = INTERRUPTED
            self._log_switch(self.current.kind, None, "urgent")

    def settle(self, result: bool) -> str:
        """尝试结果：成功 → 回到 IDLE；失败 → 放弃/委屈（由仿真计入 Affect）。"""
        prev = self.current.kind if self.current else None
        self.state = IDLE
        self.current = None
        self._retries = 0
        self._dwell_ticks = 0
        self._candidate_streak = 0
        self._log_switch(prev, None, "completed" if result else "abandon")
        return "satisfied" if result else "frustrated"
