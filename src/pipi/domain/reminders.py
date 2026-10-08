"""提醒 / 闹铃 / 待办 —— 领域模型与常量（阶段 F）。"""

from __future__ import annotations

from dataclasses import dataclass

# kind
KIND_REMINDER = "reminder"   # 柔性提醒：到点挑合适时机说
KIND_ALARM = "alarm"         # 硬性闹铃：到点必响（准点，静默窗口也响）
KIND_TODO = "todo"           # 待办：可无硬时间，列表管理

# repeat
REPEAT_ONCE = "once"
REPEAT_DAILY = "daily"
REPEAT_WEEKLY = "weekly"

# status
STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"


@dataclass
class Reminder:
    """一条提醒/闹铃/待办记录（与 reminders 表一一对应）。"""

    id: int
    user_id: str
    kind: str = KIND_REMINDER
    content: str = ""
    remind_at: float | None = None   # unix 秒；todo 无硬时间可为 None
    repeat: str = REPEAT_ONCE
    status: str = STATUS_PENDING
    created_at: float = 0.0
    completed_at: float | None = None
    fired_count: int = 0

    @property
    def is_due(self, now: float) -> bool:
        return (self.status == STATUS_PENDING
                and self.remind_at is not None
                and self.remind_at <= now)
