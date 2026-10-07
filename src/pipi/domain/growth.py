"""成长档案 —— 可成长性的类型化载体（GROWTH_DESIGN §2.1）。

GrowthProfile 是「这一位用户与皮皮的关系快照」：
  阶段（S1~S4）× 里程碑（M1~M7）× 运行时偏好 × 学到的表达 × 相处统计。

持久化分工：
  stage/stage_since/meet_date/nickname_authorized → users 表列（A1）
  milestones                                      → growth_milestones 表
  preferences / learned_expressions / stats       → profiles.extra（JSON）
纯数据类，不做 IO。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


# 阶段常量
S1, S2, S3, S4 = 1, 2, 3, 4
STAGE_NAMES = {1: "婴儿", 2: "幼儿", 3: "少年", 4: "成熟"}


@dataclass
class GrowthProfile:
    """单个用户的成长状态。"""

    user_id: str
    stage: int = S1                       # S1~S4
    stage_since: float = 0.0              # 当前阶段进入时间
    meet_date: float = 0.0                # 相遇日（首次认主）
    nickname_authorized: bool = False     # M5：昵称授权

    milestones: list[str] = field(default_factory=list)      # ["M1", "M2", ...]
    preferences: dict = field(default_factory=dict)          # {"like": {...}, "dislike": {...}}
    learned_expressions: dict = field(default_factory=dict)  # {"文本": {"count": n, "last_used": t}}
    stats: dict = field(default_factory=dict)                # {"active_days": n, "turns": n, "night_wells": n}

    # ------------------------------------------------------------------
    # 序列化（round-trip：to_dict → from_dict 等价）
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "stage": self.stage,
            "stage_since": self.stage_since,
            "meet_date": self.meet_date,
            "nickname_authorized": self.nickname_authorized,
            "milestones": list(self.milestones),
            "preferences": dict(self.preferences),
            "learned_expressions": dict(self.learned_expressions),
            "stats": dict(self.stats),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GrowthProfile":
        return cls(
            user_id=data.get("user_id", ""),
            stage=int(data.get("stage") or 1),
            stage_since=float(data.get("stage_since") or 0.0),
            meet_date=float(data.get("meet_date") or 0.0),
            nickname_authorized=bool(data.get("nickname_authorized", False)),
            milestones=list(data.get("milestones") or []),
            preferences=dict(data.get("preferences") or {}),
            learned_expressions=dict(data.get("learned_expressions") or {}),
            stats=dict(data.get("stats") or {}),
        )

    # ------------------------------------------------------------------
    # 便捷语义
    # ------------------------------------------------------------------

    @property
    def stage_name(self) -> str:
        return STAGE_NAMES.get(self.stage, "未知")

    def has_milestone(self, code: str) -> bool:
        return code in self.milestones

    def active_days(self) -> int:
        return int(self.stats.get("active_days") or 0)

    def turns(self) -> int:
        return int(self.stats.get("turns") or 0)

    def plasticity(self, curve: list[float]) -> float:
        """当前阶段的可塑性系数（曲线越早越高）。"""
        idx = min(max(self.stage - 1, 0), len(curve) - 1)
        return float(curve[idx])

    def next_stage_gap(self, stage_days: list[int],
                       stage_milestones: list[int], now: float) -> dict:
        """距下一阶段还差什么（面板/提示用）。已到 S4 返回 done。"""
        if self.stage >= S4:
            return {"done": True}
        nxt = self.stage + 1
        need_days = int(stage_days[nxt]) - self.active_days() if nxt < len(stage_days) else 0
        need_ms = int(stage_milestones[nxt]) - len(self.milestones) \
            if nxt < len(stage_milestones) else 0
        return {"done": False, "next_stage": nxt,
                "need_days": max(0, need_days), "need_milestones": max(0, need_ms)}
