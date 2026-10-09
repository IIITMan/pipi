"""条件预警引擎 —— 贾维斯化 ②：不等你问，先开口。

三条规则（全部克制：有料才说、有冷却、会话忙/睡觉不打扰）：
  1. 回家汇报：主人回来时，若"今天有值得说的"（定过提醒/有陌生人预警），
     主动开口报一句平安 + 关键事；没料就只默默高兴（PULSE 惊喜照常）。
  2. 陌生人久留：当日陌生人事件 ≥ stranger_min 次 → 提醒主人留意（长冷却）。
  3. 提醒触发：由 ReminderService 自带发声（本引擎只做今日留痕，供回家汇报汇总）。

本引擎只产出"要说的内容/冷却判定"，发声统一走 runtime 的统一说话
（LLM 按内心状态渲染 + 句库兜底），不重复建设通道。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class AlertEngine:
    """条件预警判定（纯逻辑，无副作用；时钟可注入测试）。"""

    def __init__(self, cfg: dict | None = None,
                 mono: object | None = None,
                 day_fn: object | None = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.report_home = bool(cfg.get("report_home", True))
        self.report_stranger = bool(cfg.get("report_stranger", True))
        self.home_cooldown_s = float(cfg.get("home_cooldown_s", 1800.0))
        self.stranger_cooldown_s = float(cfg.get("stranger_cooldown_s", 3600.0))
        self.stranger_min = int(cfg.get("stranger_min", 2))
        self._mono = mono or time.monotonic
        self._day_fn = day_fn or (lambda: time.localtime().tm_yday)

        self._day = -1
        self._today_reminders: list[str] = []
        self._stranger_today = 0
        self._reported_stranger = False
        self._last_home_report = -1e9
        self._last_stranger_alert = -1e9

    # ------------------------------------------------------------------

    def _roll(self) -> None:
        """跨天重置当日计数（按本地日序，可注入 day_fn 测试）。"""
        day = self._day_fn()
        if day != self._day:
            self._day = day
            self._today_reminders = []
            self._stranger_today = 0
            self._reported_stranger = False

    # ------------------------------------------------------------------

    def on_reminder_fired(self, text: str) -> None:
        """提醒触发留痕（供回家汇报汇总）。"""
        self._roll()
        if text and (not self._today_reminders
                     or self._today_reminders[-1] != text):
            self._today_reminders.append(text)

    def on_stranger(self) -> None:
        """陌生人事件计数（当日累计）。"""
        self._roll()
        self._stranger_today += 1

    # ------------------------------------------------------------------

    def home_report(self) -> str | None:
        """主人回来 → 今日值得汇报的句子；无新料/冷却中 → None（克制不开口）。

        汇报成功后"消费"已说过的料（提醒清空、陌生人标记已报），
        冷却过后若无新料就不再开口——同一件事不重复打扰。
        """
        if not self.enabled or not self.report_home:
            return None
        self._roll()
        now = self._mono()
        if now - self._last_home_report < self.home_cooldown_s:
            return None
        items: list[str] = []
        if self._today_reminders:
            items.append(f"今天你还定过提醒：「{self._today_reminders[-1]}」")
        if (self._stranger_today >= self.stranger_min
                and not self._reported_stranger):
            items.append("今天好像有不认识的人在附近出现过，我有点警觉")
        if not items:
            return None                      # 没料就不汇报（生命感>服务感）
        self._last_home_report = now
        self._today_reminders.clear()        # 已说过的提醒不再重复
        self._reported_stranger = True
        return ("你回来啦。今天家里有这些事想跟你说："
                + "，".join(items) + "。")

    def stranger_alert(self) -> str | None:
        """陌生人当日累计 ≥ stranger_min → 提醒主人留意（长冷却）。"""
        if not self.enabled or not self.report_stranger:
            return None
        self._roll()
        if self._stranger_today < self.stranger_min:
            return None
        now = self._mono()
        if now - self._last_stranger_alert < self.stranger_cooldown_s:
            return None
        self._last_stranger_alert = now
        return "好像有人一直在附近转悠……我有点不放心，你留意一下。"


__all__ = ["AlertEngine"]
