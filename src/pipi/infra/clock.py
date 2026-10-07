"""可注入时钟 —— 让测试可以"时间旅行"。

记忆衰减、主动搭话的闲置判定，都依赖时间。
如果直接调 time.time()，就没法测试"过了一天会发生什么"。
所以全部通过 Clock 取时间，测试时换成 FakeClock 一键快进。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> float: ...
    def datetime(self) -> datetime: ...
    def tz(self) -> ZoneInfo | None: ...


class SystemClock:
    """真实时钟。"""

    def __init__(self, tz_name: str = "Asia/Shanghai"):
        try:
            self._tz: ZoneInfo | None = ZoneInfo(tz_name)
        except Exception:
            self._tz = None

    def now(self) -> float:
        return time.time()

    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.now(), tz=self._tz)

    def tz(self) -> ZoneInfo | None:
        return self._tz

    # 便捷方法
    def time_str(self) -> str:
        return self.datetime().strftime("%Y-%m-%d %H:%M")

    def hour(self) -> int:
        return self.datetime().hour

    def date_str(self) -> str:
        return self.datetime().strftime("%Y-%m-%d")


class FakeClock:
    """测试用时钟 —— 支持任意快进。"""

    def __init__(self, start: float | None = None, tz_name: str = "Asia/Shanghai"):
        self._now = start if start is not None else time.time()
        try:
            self._tz: ZoneInfo | None = ZoneInfo(tz_name)
        except Exception:
            self._tz = None

    def now(self) -> float:
        return self._now

    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self._now, tz=self._tz)

    def tz(self) -> ZoneInfo | None:
        return self._tz

    def time_str(self) -> str:
        return self.datetime().strftime("%Y-%m-%d %H:%M")

    def hour(self) -> int:
        return self.datetime().hour

    def date_str(self) -> str:
        return self.datetime().strftime("%Y-%m-%d")

    # ---- 时间旅行 ----

    def advance(self, seconds: float = 0, *, hours: float = 0,
                minutes: float = 0, days: float = 0) -> None:
        self._now += seconds + hours * 3600 + minutes * 60 + days * 86400

    def set(self, when: float) -> None:
        self._now = when

    def set_datetime(self, dt: datetime) -> None:
        if dt.tzinfo is None and self._tz:
            dt = dt.replace(tzinfo=self._tz)
        self._now = dt.timestamp()

    def jump_to_hour(self, hour: int) -> None:
        """跳到当天的某个整点（用于测夜间静默/早间问候）。"""
        d = self.datetime().replace(hour=hour, minute=0, second=0, microsecond=0)
        self.set_datetime(d)
