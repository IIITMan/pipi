"""主动引擎 —— 决定"什么时候该主动开口"。

模型不会自己说话，必须由程序"戳"它。这里就是那个戳的人。

关键约束（否则陪伴变骚扰）：
  - 每日上限（人格决定，默认 3~5 次）
  - 同类触发冷却期
  - 夜间静默
  - 被连续忽视后自动降频
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pipi.domain.profile import UserProfile
from pipi.infra.clock import Clock
from pipi.infra.db import ProactiveRepo
from pipi.domain.emotion import EmotionState

logger = logging.getLogger(__name__)


@dataclass
class TriggerRule:
    """一条触发规则。"""

    key: str
    enabled: bool = True
    priority: int = 50
    cooldown_min: int = 30


@dataclass
class ProactiveDecision:
    """主动搭话决策结果。"""

    should_fire: bool
    trigger: str = ""
    event: str = ""          # 给 LLM 看的系统事件描述
    reason: str = ""
    suppressed_by: str = ""

    @property
    def suppressed(self) -> bool:
        return not self.should_fire and bool(self.suppressed_by)


class ProactiveEngine:
    """主动搭话决策引擎。"""

    def __init__(
        self,
        *,
        clock: Clock,
        repo: ProactiveRepo,
        daily_limit: int = 3,
        idle_hours: float = 2.0,
        quiet_hours: tuple[int, int] = (23, 7),
        return_cooldown_min: int = 30,
        triggers: dict[str, dict] | None = None,
        persona_clinginess: float = 0.5,
        cooldowns: dict[str, int] | None = None,
    ):
        self.clock = clock
        self.repo = repo
        self.daily_limit = daily_limit
        self.idle_hours = idle_hours
        self.quiet_hours = quiet_hours
        self.return_cooldown_min = return_cooldown_min
        self.persona_clinginess = persona_clinginess

        # 粘人的降低闲置阈值
        self.effective_idle_hours = max(0.5, idle_hours * (1.3 - 0.6 * persona_clinginess))

        self.rules: dict[str, TriggerRule] = {}
        default_rules = {
            "return_home": (90, 30),
            "concern_followup": (85, 60),
            "important_date": (80, 1440),
            "idle": (50, 180),
            "morning": (40, 720),
            "night_sleep": (40, 720),
            "random": (10, 240),
        }
        cd_override = cooldowns or {}
        for key, (prio, cd) in default_rules.items():
            cfg = (triggers or {}).get(key, {})
            self.rules[key] = TriggerRule(
                key=key,
                enabled=cfg.get("enabled", True),
                priority=cfg.get("priority", prio),
                cooldown_min=cd_override.get(key, cd),
            )

        self._ignored_streak = 0
        self._last_response_at: float = 0.0

    # ------------------------------------------------------------------

    def note_user_response(self, now: float) -> None:
        """用户回应了 —— 重置忽视计数。"""
        self._ignored_streak = 0
        self._last_response_at = now

    def note_ignored(self) -> None:
        """主动搭话没被回应。"""
        self._ignored_streak += 1

    def effective_daily_limit(self) -> int:
        """被连续忽视后自动降频 —— 避免变成骚扰。"""
        if self._ignored_streak >= 3:
            return 0
        if self._ignored_streak >= 2:
            return max(1, self.daily_limit // 2)
        return self.daily_limit

    # ------------------------------------------------------------------

    def _is_quiet_time(self) -> bool:
        hour = self.clock.hour()
        start, end = self.quiet_hours
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end   # 跨天

    def _day_start(self) -> float:
        d = self.clock.datetime().replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    def evaluate(
        self,
        user_id: str,
        *,
        profile: UserProfile,
        state: EmotionState,
        owner_present: bool = False,
        owner_newly_arrived: bool = False,
        last_user_message_at: float = 0.0,
    ) -> ProactiveDecision:
        """综合判断是否该主动搭话。"""
        now = self.clock.now()

        # ---- 1. 夜间静默 ----
        if self._is_quiet_time():
            return ProactiveDecision(False, suppressed_by="夜间静默期")

        # ---- 2. 每日上限 ----
        used = self.repo.count_today(user_id, self._day_start())
        limit = self.effective_daily_limit()
        if used >= limit:
            return ProactiveDecision(
                False, suppressed_by=f"今日已达上限 {used}/{limit}"
            )

        # ---- 3. 候选触发（按优先级）----
        candidates: list[tuple[int, str, str]] = []

        # 回家问候
        if (self.rules["return_home"].enabled and owner_newly_arrived):
            candidates.append((
                self.rules["return_home"].priority,
                "return_home",
                f"主人刚回到家。{self._vision_context_hint(owner_present)}",
            ))

        # 情绪低落跟踪
        if (self.rules["concern_followup"].enabled
                and state.companion_need >= 0.5):
            candidates.append((
                self.rules["concern_followup"].priority,
                "concern_followup",
                "他刚才情绪不太好，你有点不放心",
            ))

        # 重要日子提醒
        if self.rules["important_date"].enabled and profile.important_dates:
            d = self.clock.datetime()
            tomorrow = (d.timestamp() + 86400)
            import time as _t
            tmr = _t.strftime("%Y-%m-%d", _t.localtime(tomorrow))
            for label, date in profile.important_dates.items():
                if date == tmr:
                    candidates.append((
                        self.rules["important_date"].priority,
                        "important_date",
                        f"明天是他{label}的日子",
                    ))
                    break

        # 长时间没理
        ref = last_user_message_at or state.last_interaction
        if self.rules["idle"].enabled and ref:
            hours = (now - ref) / 3600.0
            if hours >= self.effective_idle_hours:
                candidates.append((
                    self.rules["idle"].priority,
                    "idle",
                    f"主人已经 {hours:.0f} 小时没理你了，你有点想他，也有点小委屈",
                ))

        # 早间问候
        hour = self.clock.hour()
        if self.rules["morning"].enabled and 7 <= hour <= 9:
            if not self.repo.last_fired(user_id, "morning"):
                candidates.append((
                    self.rules["morning"].priority, "morning",
                    "天亮了，主人可能刚醒",
                ))

        # 夜间催睡
        if self.rules["night_sleep"].enabled and 22 <= hour < 23:
            if not self.repo.last_fired(user_id, "night_sleep"):
                candidates.append((
                    self.rules["night_sleep"].priority, "night_sleep",
                    "快 23 点了，他还在忙",
                ))

        if not candidates:
            return ProactiveDecision(False, suppressed_by="无满足条件的触发器")

        # ---- 4. 取优先级最高的，检查冷却 ----
        candidates.sort(key=lambda x: x[0], reverse=True)
        for prio, trigger, event in candidates:
            rule = self.rules.get(trigger)
            cooldown = rule.cooldown_min if rule else 30
            if trigger == "return_home":
                cooldown = self.return_cooldown_min
            last = self.repo.last_fired(user_id, trigger)
            if last and (now - last) < cooldown * 60:
                continue
            return ProactiveDecision(
                True, trigger=trigger, event=event,
                reason=f"优先级 {prio}，冷却 {cooldown}min 已过",
            )

        return ProactiveDecision(False, suppressed_by="所有候选都在冷却期内")

    def record_fired(self, user_id: str, trigger: str, content: str) -> None:
        self.repo.log(user_id, trigger, content, self.clock.now())

    def record_suppressed(self, user_id: str, trigger: str, reason: str) -> None:
        self.repo.log(user_id, trigger, "", self.clock.now(),
                      suppressed=True, reason=reason)

    @staticmethod
    def _vision_context_hint(owner_present: bool) -> str:
        return "你能看见他" if owner_present else ""
