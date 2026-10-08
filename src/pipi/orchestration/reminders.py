"""提醒 / 闹铃 / 待办服务（阶段 F）。

职责：
  1. 定时 tick：到期的提醒/闹铃/待办触发发声（走 session 自发通道）；
  2. 对话入口：orchestrator.handle_turn 前调用 try_parse —— 规则解析
     「提醒我/闹钟/待办/知道了」等意图，命中直接返回确认文本；
  3. 确认闭环：用户说"知道了/完成" → 完成最近被触发的那条，沉淀事件。

发声策略：
  - alarm（闹铃）：准点硬响（静默窗口也响）；
  - reminder/todo：普通提醒，静默窗口内顺延到窗口结束再响；
  - speak 不可用（无 session）时仅记录 + 通知 sink（文本模式打印）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable

from pipi.domain.reminders import (
    KIND_ALARM,
    KIND_REMINDER,
    KIND_TODO,
    REPEAT_DAILY,
    REPEAT_ONCE,
    REPEAT_WEEKLY,
    STATUS_DONE,
    STATUS_PENDING,
)
from pipi.infra.clock import Clock

logger = logging.getLogger(__name__)

# ---------------- 创建意图（引导词 → kind / 内容提取）----------------

_CREATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"提醒我\s*(.+)"), KIND_REMINDER),
    (re.compile(r"帮我记(?:着|一下)?\s*(.+)"), KIND_REMINDER),
    (re.compile(r"记(?:一下|住)\s*(.+)"), KIND_REMINDER),
    (re.compile(r"别忘了\s*(.+)"), KIND_REMINDER),
    (re.compile(r"闹钟(?:定|设|调)?\s*(.+)"), KIND_ALARM),
    (re.compile(r"几点(?:叫我|喊我)\s*(.+)"), KIND_ALARM),
    # "明早八点叫我（起床）" —— 时刻在前的闹铃句式
    (re.compile(
        r"((?:明早|明天早上|明天|今晚|明天晚上|晚上|早上|上午|下午|凌晨)?\s*"
        r"[0-9一二两三四五六七八九十]{1,3}点半?)[，,]?\s*(?:叫|喊)(?:醒)?我(?:起床)?"),
     KIND_ALARM),
    (re.compile(r"到(?:点|时间)提醒我?\s*(.+)"), KIND_REMINDER),
    (re.compile(r"(?:待办|要做的事)[:：]?\s*(.+)"), KIND_TODO),
]

_QUERY_PATTERNS = re.compile(r"(?:有什么提醒|我的待办|待办清单|提醒我什么|还有.*提醒|看看.*待办)")
_CANCEL_PATTERNS = re.compile(r"(?:取消|删掉|去掉)(?:提醒|闹钟|待办)")
# 确认/完成：只在"最近确有被触发记录"时生效，避免误吞普通对话
_ACK_PATTERNS = re.compile(r"^(?:知道了|好的|好|嗯|收到|完成|做完了|搞定|记下了|嗯嗯|好哒|ok|OK)$")

# 中文数字（"一分钟后" "半小时后" "十五分钟后"）→ 数值
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "半": 0.5}


def _cn2num(s: str) -> float | None:
    """解析中文/阿拉伯数字：'一'→1 '两'→2 '十五'→15 '半小时'的'半'→0.5。"""
    s = s.strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    if s in _CN_DIGIT:
        return _CN_DIGIT[s]
    if s == "十":
        return 10.0
    m = re.fullmatch(r"([一二两三四五六七八九])?十([一二三四五六七八九])?", s)
    if m:
        return _CN_DIGIT.get(m.group(1), 1) * 10 + _CN_DIGIT.get(m.group(2), 0)
    return None


_NUM = r"([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十半]{1,3})"


def _hn(s: str | None, default: int) -> int:
    """时刻数字：支持中文（"八点"→8），缺省给 default。"""
    if not s:
        return default
    if s.isdigit():
        return int(s)
    n = _cn2num(s)
    return int(n) if n is not None else default

# 时间子串规则：返回 (unix ts, 匹配到的文本)
_WHEN_RULES: list[tuple[re.Pattern, Callable[[float, re.Match], float]]] = [
    (re.compile(_NUM + r"\s*分钟(?:之|以)?后?"),
     lambda now, m: now + (_cn2num(m.group(1)) or 1) * 60),
    (re.compile(_NUM + r"\s*(?:小时|钟头)(?:之|以)?后?"),
     lambda now, m: now + (_cn2num(m.group(1)) or 1) * 3600),
    (re.compile(r"(\d{1,2})\s*[:：](\d{2})"),
     lambda now, m: _next_hhmm(now, int(m.group(1)), int(m.group(2)))),
    (re.compile(r"(?:明天|明早)\s*([0-9一二两三四五六七八九十]{1,3})?点?(半)?"),
     lambda now, m: _next_hhmm(now, _hn(m.group(1), 9),
                              30 if m.group(2) else 0, day_delta=1)),
    (re.compile(r"(?:今晚|晚上)\s*([0-9一二两三四五六七八九十]{1,3})?点(半)?"),
     lambda now, m: _next_hhmm(now, _hn(m.group(1), 20),
                              30 if m.group(2) else 0)),
    (re.compile(r"(?:早上|早晨)\s*([0-9一二两三四五六七八九十]{1,3})?点(半)?"),
     lambda now, m: _next_hhmm(now, _hn(m.group(1), 8),
                              30 if m.group(2) else 0)),
    (re.compile(r"(?:下午)\s*([0-9一二两三四五六七八九十]{1,3})?点(半)?"),
     lambda now, m: _next_hhmm(now, _hn(m.group(1), 14),
                              30 if m.group(2) else 0)),
    (re.compile(r"([0-9一二两三四五六七八九十]{1,3})\s*点(半)?"),
     lambda now, m: _next_hhmm(now, _hn(m.group(1), 9),
                              30 if m.group(2) else 0)),
]

# 时间在前的说法："一分钟后提醒我" "过半小时提醒我吃药" "五分钟后叫我"
# （旧规则只认"提醒我 X"，这种说法整句漏配 → 沦为普通聊天，提醒从未创建）
_TIME_FIRST_RE = re.compile(
    r"^(?:帮我|给我)?(?:再|先)?(?:过|等)?(?P<when>[^，。！？]{1,12}?(?:分钟|小时|钟头|天)"
    r"(?:之|以)?后?)[，,]?\s*(?:提醒我?|叫我|喊我)(?:一下|一声)?(?P<what>.*)$")


def _next_hhmm(now: float, hour: int, minute: int, *, day_delta: int = 0) -> float:
    """今天（或 day_delta 天后）的 hour:minute；已过则推到下一天。"""
    lt = time.localtime(now)
    import calendar
    day = lt.tm_mday + day_delta
    month, year = lt.tm_mon, lt.tm_year
    while day > calendar.monthrange(year, month)[1]:
        day -= calendar.monthrange(year, month)[1]
        month += 1
        if month > 12:
            month, year = 1, year + 1
    ts = time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))
    if ts <= now and day_delta == 0:
        ts += 24 * 3600
    return ts


def _parse_when(text: str, now: float) -> tuple[float | None, str]:
    """返回 (时间戳, 匹配子串)；未命中返回 (None, "")。"""
    for pat, fn in _WHEN_RULES:
        m = pat.search(text)
        if m:
            try:
                return fn(now, m), m.group(0)
            except (ValueError, OverflowError):
                continue
    return None, ""


class ReminderService:
    """提醒/闹铃/待办：定时触发 + 意图解析 + 确认闭环。"""

    def __init__(self, repo: Any, clock: Clock, *, cfg: Any = None):
        self.repo = repo
        self.clock = clock
        r_cfg = (cfg or {}).get("reminders", {}) if cfg is not None else {}
        self.enabled = bool(r_cfg.get("enabled", True))
        self.tick_s = float(r_cfg.get("tick_s", 2.0))
        self.retry_max = int(r_cfg.get("retry_max", 3))
        self.retry_window_s = float(r_cfg.get("retry_window_s", 120.0))
        self.default_offset_s = float(r_cfg.get("default_offset_s", 3600.0))
        qh = r_cfg.get("quiet_hours")
        if not qh:
            qh = (cfg or {}).get("proactive", {}).get("quiet_hours", [23, 7])
        self.quiet_hours: tuple[int, int] = tuple(qh) if len(qh) == 2 else (23, 7)
        self.ack_window_s = float(r_cfg.get("ack_window_s", 600.0))
        self.active_uid = "owner"   # 监控面板展示谁的待办（声纹识别更新）

        self.speak: Callable[[str], bool] | None = None
        self.sink: Callable[[str], None] | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._retries: dict[int, dict] = {}   # rid -> {until, tries}
        self._last_snapshot: dict = {"items": [], "next_due": None}

    # ------------------------------------------------------------------

    def bind_speak(self, fn: Callable[[str], bool]) -> None:
        """绑定发声通道（语音会话的 try_speak_spontaneous）。"""
        self.speak = fn

    def set_sink(self, fn: Callable[[str, str], None]) -> None:
        """绑定事件通知（文本模式打印）。fn(kind, text)；kind ∈ fired/created/completed/cancelled/postponed。"""
        self.sink = fn

    def start(self) -> None:
        if self._running or not self.enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="reminders")
        logger.info("提醒服务启动（tick=%.1fs, quiet=%s）",
                    self.tick_s, self.quiet_hours)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception as exc:
                logger.warning("提醒 tick 异常（循环继续）：%s", exc)
            await asyncio.sleep(self.tick_s)

    def _tick(self) -> None:
        now = self.clock.now()
        due = self.repo.list_due(now)
        for r in due:
            try:
                self._fire(r, now)
            except Exception as exc:
                logger.warning("提醒触发失败（id=%s）：%s", r.get("id"), exc)
        self._retry_pending(now)

    def _retry_pending(self, now: float) -> None:
        for rid, st in list(self._retries.items()):
            r = self.repo.get(rid)
            if r is None or r.get("status") != STATUS_PENDING:
                self._retries.pop(rid, None)
                continue
            if now < st["until"]:
                continue
            if st["tries"] >= self.retry_max:
                logger.warning("提醒发声多次失败，放弃（id=%s）：%s",
                               rid, r.get("content"))
                self._finish(r, now, mark_done=True)
                self._retries.pop(rid, None)
                continue
            st["tries"] += 1
            st["until"] = now + self.retry_window_s / self.retry_max
            if self._emit(r):
                self._finish(r, now)
                self._retries.pop(rid, None)

    def _fire(self, r: dict, now: float) -> None:
        rid = int(r["id"])
        # 静默窗口：非闹铃顺延到窗口结束；闹铃准点必响
        if r.get("kind") != KIND_ALARM and self._in_quiet(now):
            end = self._quiet_end(now)
            self.repo.postpone(rid, end)
            logger.debug("提醒顺延到静默窗口结束：%s → %.0f", r.get("content"), end)
            self._notify("postponed", r)
            return
        if self._emit(r):
            self._finish(r, now)
        else:
            self._retries[rid] = {"until": now + self.retry_window_s / max(self.retry_max, 1),
                                  "tries": 1}

    def _emit(self, r: dict) -> bool:
        """发出提醒（发声或无声模式）。返回是否成功。"""
        text = self._render_fire(r)
        self._notify("fired", r, text=text)
        if self.speak is None:
            return True        # 无声模式：默认成功（文本模式已打印）
        try:
            return bool(self.speak(text))
        except Exception as exc:
            logger.warning("提醒发声异常：%s", exc)
            return False

    def _finish(self, r: dict, now: float, *, mark_done: bool = False) -> None:
        rid = int(r["id"])
        repeat = r.get("repeat") or REPEAT_ONCE
        if mark_done or repeat == REPEAT_ONCE:
            self.repo.bump_fired(rid)     # 计入"触发过"（确认闭环/审计用）
            self.repo.complete(rid, now)
            return
        # 周期项：推进到下一次（reschedule 内部自增 fired_count）
        base = float(r.get("remind_at") or now)
        delta = 24 * 3600 if repeat == REPEAT_DAILY else 7 * 24 * 3600
        self.repo.reschedule(rid, base + delta)

    # ---------------- 对话入口（由 orchestrator.handle_turn 调用）----------------

    def try_parse(self, user_id: str, text: str) -> tuple[bool, str]:
        """规则解析提醒/待办意图。返回 (handled, reply)。

        未命中返回 (False, "")，调用方继续走正常对话。
        """
        if not self.enabled:
            return False, ""
        t = (text or "").strip()
        if not t:
            return False, ""
        now = self.clock.now()

        # 1) 查询
        if _QUERY_PATTERNS.search(t):
            items = self.repo.list_active(user_id)
            if not items:
                return True, "目前没有待办和提醒，安安静静的～"
            lines = []
            for it in items[:5]:
                when = self._fmt_when(it.get("remind_at"), now)
                lines.append(f"· {it.get('content')}{when}")
            return True, "我记着这些：\n" + "\n".join(lines)

        # 2) 取消（取最近创建的一条，避免误删更早的）
        if _CANCEL_PATTERNS.search(t):
            items = self.repo.list_active(user_id)
            if items:
                target = max(items, key=lambda it: it.get("created_at") or 0)
                rid = int(target["id"])
                self.repo.cancel(rid)
                self._notify("cancelled", target)
                return True, f"好，{target.get('content')} 这条我划掉了。"
            return True, "现在没有可取消的提醒。"

        # 3) 确认/完成（只在最近有被触发的记录时生效，防误吞普通对话）
        if _ACK_PATTERNS.match(t):
            rid = self.repo.complete_recent_fired(user_id, now - self.ack_window_s)
            if rid is not None:
                r = self.repo.get(rid)
                self.repo.complete(rid, now)
                self._notify("completed", r)
                return True, "嗯，记下了。这一条就翻篇啦。"
            return False, ""

        # 4) 创建提醒/闹铃/待办

        # 4a) 时间在前："一分钟后提醒我" "过半小时提醒我吃药" "五分钟后叫我"
        m = _TIME_FIRST_RE.match(t)
        if m:
            when, _ = _parse_when(m.group("when"), now)
            if when is not None:
                content = m.group("what").strip("，,。 、") or "你刚才交代的事"
                rid = self.repo.create(
                    user_id, KIND_REMINDER, content, when, REPEAT_ONCE, now)
                r = self.repo.get(rid)
                self._notify("created", r)
                when_s = self._fmt_when(when, now).strip("（）")
                if content != "你刚才交代的事":
                    return True, f"好，{when_s}我叫你{content}。"
                return True, f"好，{when_s}我叫你。"

        # 4b) 引导词在前："提醒我10分钟后喝水" "明早八点叫我" "待办：……"
        for pat, kind in _CREATE_PATTERNS:
            m = pat.search(t)
            if not m:
                continue
            content = m.group(1).strip()
            when, matched = _parse_when(content, now)
            if when is not None and matched:
                content = content.replace(matched, "").strip("，,。 、")
            # 纯标点/无实义内容（如 ASR 残句"后提醒我。"抓到"。"）→ 用兜底文案
            if content and not any(ch.isalnum() for ch in content):
                content = ""
            if not content:
                content = (matched or "").strip() or "你交代的事"
            remind_at = when if when is not None else now + self.default_offset_s
            rid = self.repo.create(
                user_id, kind, content or "到点了", remind_at, REPEAT_ONCE, now)
            r = self.repo.get(rid)
            self._notify("created", r)
            if kind == KIND_ALARM:
                when_s = self._fmt_when(remind_at, now)
                return True, f"闹钟设好啦：{content}{when_s}，到点我叫你。"
            if kind == KIND_TODO:
                return True, f"记到待办了：{content}。做完了跟我说一声就行。"
            return True, f"好，我记着了：{content}{self._fmt_when(remind_at, now)}。到点提醒你。"

        return False, ""

    # ---------------- 快照（面板）----------------

    def snapshot(self) -> dict:
        """活跃提醒/待办列表 + 下次触发时间（供监控面板）。

        展示当前说话人（active_uid，由窗口端随声纹更新）的待办。
        """
        items = self.repo.list_active(self.active_uid)
        self._last_snapshot = {
            "items": [{
                "id": it["id"], "kind": it["kind"], "content": it["content"],
                "remind_at": it.get("remind_at"),
                "repeat": it.get("repeat"),
            } for it in items[:20]],
            "next_due": min(
                (it.get("remind_at") for it in items if it.get("remind_at")),
                default=None),
        }
        return self._last_snapshot

    # ---------------- 内部工具 ----------------

    def _in_quiet(self, now: float) -> bool:
        h = time.localtime(now).tm_hour
        start, end = self.quiet_hours
        if start <= end:
            return start <= h < end
        return h >= start or h < end   # 跨天窗口，如 [23,7]

    def _quiet_end(self, now: float) -> float:
        lt = time.localtime(now)
        end_h = self.quiet_hours[1]
        ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, end_h, 0, 0, 0, 0, -1))
        if ts <= now:
            ts += 24 * 3600
        return ts + 30.0   # 窗口结束后留 30s 缓冲

    def _fmt_when(self, ts: float | None, now: float) -> str:
        if ts is None:
            return ""
        delta = ts - now
        if delta < 60:
            return "（马上）"
        if delta < 3600:
            return f"（{int(delta // 60)} 分钟后）"
        lt = time.localtime(ts)
        day_str = "明天" if time.localtime(now).tm_mday != lt.tm_mday else ""
        return f"（{day_str}{lt.tm_hour}:{lt.tm_min:02d}）"

    def _render_fire(self, r: dict) -> str:
        content = r.get("content") or "到点了"
        kind = r.get("kind")
        if kind == KIND_ALARM:
            return f"叮咚——闹钟响啦，{content}！该起床了！"
        if kind == KIND_TODO:
            return f"嘿嘿，路过提醒一句：待办「{content}」还没做哦。"
        if content == "你交代的事":
            return "叮咚——到点啦！刚才你让我提醒的事，现在说吧。"
        return f"叮咚——到点啦！{content}"

    def _notify(self, kind: str, r: dict | None, *, text: str = "") -> None:
        if self.sink is None or r is None:
            return
        try:
            self.sink(kind, text or (r.get("content") or ""))
        except Exception as exc:
            logger.debug("提醒通知失败：%s", exc)
