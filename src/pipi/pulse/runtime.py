"""PulseRuntime —— LivingLoop 的真实系统适配层（asyncio）。

职责：把 PULSE 同步心脏（LivingLoop，1 仿真秒 = 1 真实秒）接入现有系统：
  - 1Hz tick：呼吸/潮汐/天气/意愿实时运转（1 天 = 真实 1 天，思念/认主/成长
    全部按真实时间演化）；
  - 事件注入：会话（人声/打断/应答完成/告别）、视觉感知（到场/离场/陌生人/
    主人低落）、外部模块（提醒触发）统一映射 loop.inject()；
  - 自发发声：按主导意图低频开口（零 LLM 固定句库，冷却+概率克制）；
  - 每日入睡整理：consolidate_night() 生成日记 + 种子漂移（真实"一天一记"）；
  - 监控上报：snapshot/事件/聊天经 sink 推给 monitor（与旧 LifeEngine 同协议）。

对外接口与 LifeEngine 兼容（session/cli 无需改动即可切换）：
  start / stop / observe_user_voice / on_barge_in / on_response_done /
  on_farewell / observe_vision / log_chat / set_sink / snapshot / note_event
  / physio.observe_dialogue_mood
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any, Callable

from pipi.pulse.loop import LivingLoop

logger = logging.getLogger(__name__)

# 主导意图 → 自发话语（零 LLM 兜底；生产可换成 LLM 渲染，接口不变）
_VOCALS: dict[str, tuple[str, ...]] = {
    "loneliness": ("有点想你了……", "你什么时候回来呀……", "一个人好安静……"),
    "curiosity": ("那边好像有什么动静……", "咦，刚才是什么声音？", "想去看看……"),
    "attachment": ("嗯……你在旁边真好。", "嘿嘿，靠着你很安心。"),
    "useless": ("（小声哼歌）", "（伸了个懒腰）", "（对着窗外发了一会儿呆）"),
}

_MOOD_ZH = {"晴": "开心", "多云": "平静", "阴": "一般",
            "小雨": "低落", "阵雨": "烦躁", "雷雨": "不安"}

# 感知事件 → PULSE 事件（PerceptionLoop 的 kind → loop.inject 的 kind）
_VISION_MAP = {
    "owner_arrived": "owner_arrive",
    "owner_gone": "owner_depart",
    "stranger": "stranger",
    "owner_expression": "owner_sad",
}

# 对话情绪 → 心境底色微调。
# 兼容两种入参：英文枚举名（HAPPY/SAD...）与 cli 实际回灌的中文 mood.value
# （domain/emotion.py：开心/关心/吃醋/委屈/得意/傲娇/生气/困倦/调皮/平静）
_MOOD_BASE = {
    # 英文
    "HAPPY": 0.18, "SAD": -0.18, "ANGRY": -0.12, "NEUTRAL": 0.0,
    "EXCITED": 0.22, "TIRED": -0.08,
    # 中文（Mood enum value）
    "开心": 0.18, "得意": 0.16, "调皮": 0.15, "关心": 0.05,
    "平静": 0.0, "困倦": -0.05, "傲娇": 0.02,
    "吃醋": -0.10, "委屈": -0.14, "生气": -0.16,
}


class _PhysioCompat:
    """旧 LifeEngine.physio 的兼容门面（cli 里 observe_dialogue_mood 用）。"""

    def __init__(self, runtime: "PulseRuntime"):
        self._rt = runtime

    def observe_dialogue_mood(self, mood: str) -> None:
        self._rt.observe_dialogue_mood(mood)


class PulseRuntime:
    """PULSE 心搏运行时（asyncio 版）。"""

    def __init__(self, cfg: Any, *, session: Any = None,
                 vision: Any | None = None,
                 loop: LivingLoop | None = None,
                 clock: Any = None,
                 mono: Any = None):
        p_cfg = (cfg or {}).get("pulse", {}) or {}
        self.cfg = p_cfg
        self.session = session
        self.vision = vision
        self.clock = clock or _LocalClock()
        # 单调时钟（发声冷却/人声节流）：真实系统用 time.monotonic，
        # 仿真可注入（验证时按仿真时间推进，避免"永远在冷却"）
        self._mono = mono or time.monotonic
        self.loop = loop or LivingLoop()
        self.physio = _PhysioCompat(self)

        self.tick_s = float(p_cfg.get("tick_s", 1.0))
        self.vocal_cooldown_s = float(p_cfg.get("vocal_cooldown_s", 90.0))
        self.vocal_prob = float(p_cfg.get("vocal_prob", 0.008))
        self.diary_memory = bool(p_cfg.get("diary_memory", False))

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._last_day = self.clock.now_day()
        self._last_vocal = 0.0
        self._since_vocal = 0.0            # 每 5s 检查一次自发发声
        self._last_chat_mood = ""          # 最近一轮对话情绪（中文）
        self._last_chat_mood_t = -1e9      # 时刻（_mono 口径，60s 有效）
        self._last_thought_desc = ""
        self._activity_desc = ""
        self._last_event_logged: float = 0.0   # user_voice 节流

        # 监控上报
        self._sink: Callable[[dict], None] | None = None
        self._outbox: deque = deque(maxlen=100)
        self._chat_outbox: deque = deque(maxlen=60)
        self.story_getter: Callable[[], str | None] | None = None   # 兼容赋值

        self._rng = random.Random()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._pulse_loop(), name="pulse-beat"),
            asyncio.create_task(self._monitor_flush_loop(), name="pulse-monitor"),
        ]
        # 感知循环（vision 注入且启用时）：事件 → inject
        if self.vision is not None:
            try:
                from pipi.life.perception import MonotonicClock, PerceptionLoop
                perc_cfg = {}
                full = getattr(self, "_full_cfg", None)
                if full is not None:
                    perc_cfg = ((full.get("life", {}) or {})
                                .get("perception", {}) or {})
                self._perception = PerceptionLoop(
                    self.vision, MonotonicClock(), perc_cfg,
                    on_event=self.observe_vision)
                self._perception.start()
                logger.info("感知循环已启动（PULSE 视觉接入）")
            except Exception as exc:
                logger.info("感知循环不可用（%s）", exc)
        logger.info("PULSE 心搏启动（tick=%.1fs）", self.tick_s)

    def bind_cfg(self, full_cfg: Any) -> None:
        """绑定完整配置（供感知循环读 life.perception 参数）。"""
        self._full_cfg = full_cfg

    async def stop(self) -> None:
        self._running = False
        if getattr(self, "_perception", None) is not None:
            try:
                await self._perception.stop()
            except Exception:
                pass
            self._perception = None
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------
    # 会话事件注入（由 InteractionSession 调用，与 LifeEngine 同接口）
    # ------------------------------------------------------------------

    def observe_user_voice(self) -> None:
        """聆听态有人声 → 一次互动（1s 节流防爆表）。"""
        now = self._mono()
        if now - self._last_event_logged < 1.0:
            return
        self._last_event_logged = now
        self._inject("user_voice", {"dur": 5.0})

    def on_barge_in(self) -> None:
        """被主人打断：意图挂起 + 委屈波澜（下一次 Will 重试/放弃）；夜间保持清醒。"""
        self.loop.will.interrupt()
        self.loop.affect.push_base(-0.10)
        self.loop.wake(10.0)
        self._event_log("barge_in", "被主人打断")

    def on_response_done(self) -> None:
        """一轮应答完成：小回暖 + 互动计数；夜间再保持 10 分钟清醒才睡回去。"""
        self.loop.affect.push_base(0.06)
        self.loop.bond.interact()
        self.loop.wake(10.0)
        self._event_log("response_done", "一轮应答完成")

    def on_farewell(self) -> None:
        """告别回待机：记一笔；夜间 5 分钟后自然睡回去。"""
        self.loop.wake(5.0)
        self._event_log("farewell", "对话结束，回待机")

    def observe_dialogue_mood(self, mood: str) -> None:
        """对话情绪回灌心境（旧 physio.observe_dialogue_mood 兼容）。"""
        key = (mood or "平静").strip()
        d = _MOOD_BASE.get(key, _MOOD_BASE.get(key.upper(), 0.0))
        if d:
            self.loop.affect.push_base(d)
        self.loop.bond.interact()
        # 留痕最近对话情绪（监控"心情"可见，60s 后回落）
        self._last_chat_mood = key
        self._last_chat_mood_t = self._mono()

    # ------------------------------------------------------------------
    # 视觉感知注入（PerceptionLoop → inject）
    # ------------------------------------------------------------------

    def observe_vision(self, event: Any) -> None:
        kind = _VISION_MAP.get(event.kind)
        if kind is None:
            return
        text = self._inject(kind, {})
        self._event_log(event.kind, f"{event.text} · {text}" if text
                        else event.text)

    # ------------------------------------------------------------------
    # 外部模块事件（提醒触发等）→ 当天日记留痕
    # ------------------------------------------------------------------

    def note_event(self, kind: str, text: str) -> None:
        """外部事件记入当天日记/生命流（不扰动动力学）。"""
        minute = self._now_minute()
        self.loop._note(kind, minute, text)
        self._event_log(kind, text)

    # ------------------------------------------------------------------
    # 聊天上报（cli 调用）
    # ------------------------------------------------------------------

    def log_chat(self, role: str, text: str) -> None:
        if self._sink is not None:
            self._chat_outbox.append(
                {"t": round(time.time(), 3), "role": role, "text": text})

    def set_sink(self, fn: Callable[[dict], None]) -> None:
        self._sink = fn

    # ------------------------------------------------------------------
    # 内部：事件 → 心脏
    # ------------------------------------------------------------------

    def _inject(self, kind: str, kw: dict) -> str:
        minute = self._now_minute()
        try:
            text = self.loop.inject(kind, minute=minute, **kw)
        except Exception as exc:
            logger.warning("事件注入失败（%s）：%s", kind, exc)
            return ""
        self._event_log(kind, text or kind)
        return text

    def _event_log(self, kind: str, text: str) -> None:
        entry = {"t": round(time.time(), 3), "kind": kind, "text": text}
        # 无条件留痕（监控未接时也保留最近 100 条，供本地查看）
        self._outbox.append(entry)

    def _now_minute(self) -> float:
        """当前仿真分钟（0-1439），供作息学习/提前判定。"""
        return self.clock.now_hour() * 60.0 + self.clock.now_minute()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def _pulse_loop(self) -> None:
        while self._running:
            try:
                self._beat()
            except Exception as exc:
                logger.warning("心搏异常（循环继续）：%s", exc)
            await asyncio.sleep(self.tick_s)

    def _beat(self) -> None:
        """一拍：跨天整理 → 心脏 tick → 自发发声决策。"""
        hour = self.clock.now_hour()
        day = self.clock.now_day()

        # 跨天 → 入睡整理（真实"一天一记"：日记 + 种子漂移 + 认主日历）
        if day != self._last_day:
            self._last_day = day
            try:
                n = self.loop.consolidate_night()
                short = (n.replace("\n", " ")[:200]
                         if n else "今日份生命流整理完成")
                self._event_log("diary", short)
                logger.info("PULSE 入睡整理（Day %s）：%s",
                            day, short[:80])
            except Exception as exc:
                logger.warning("入睡整理异常：%s", exc)

        snap = self.loop.tick(self.tick_s, hour)
        self._last_thought_desc = self._describe_intent(snap)
        self._activity_desc = snap["body"].get("act", "") or ""

        # 自发发声：每 5s 决策一次（非睡眠、冷却过、概率命中、会话空闲）
        self._since_vocal += self.tick_s
        if self._since_vocal >= 5.0:
            self._since_vocal = 0.0
            self._maybe_vocal(snap)

    def _maybe_vocal(self, snap: dict) -> None:
        if snap["sleeping"]:
            return
        if self.session is None or getattr(self.session, "is_busy", False):
            return
        now = self._mono()
        if now - self._last_vocal < self.vocal_cooldown_s:
            return
        intent = snap.get("intent")
        kind = intent.kind if intent is not None else "useless"
        lines = _VOCALS.get(kind)
        if not lines:
            return
        # 只想陪/好奇时才开口（克制：概率 × 主导强度）
        strength = snap["tension"].waves.get(
            kind if kind != "useless" else "loneliness", 0.0)
        prob = self.vocal_prob * (0.5 + strength)
        if self._rng.random() >= prob:
            return
        text = self._rng.choice(lines)
        if text.startswith("("):       # 哼歌/伸懒腰：仅内心与动作，不占发声
            self._event_log("inner", text)
            self._last_vocal = now
            return
        self._last_vocal = now
        ok = self.session.try_speak_spontaneous(text)
        self._event_log("vocal" if ok else "muted", text)

    def _describe_intent(self, snap: dict) -> str:
        intent = snap.get("intent")
        if intent is None:
            return ""
        from pipi.pulse.organs.will import REASON
        if intent.kind == "useless":
            return f"没什么要紧事，{intent.action or '发一会儿呆'}"
        r = REASON.get(intent.kind, "")
        return f"想{r}（{intent.kind}）" if r else intent.kind

    # ------------------------------------------------------------------
    # 监控快照（monitor 兼容结构 + PULSE 专属字段）
    # ------------------------------------------------------------------

    async def _monitor_flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)
            if self._sink is None:
                continue
            payload = {
                "events": list(self._outbox),
                "chat": list(self._chat_outbox),
                "snapshot": self.snapshot(),
            }
            self._outbox.clear()
            self._chat_outbox.clear()
            try:
                self._sink(payload)
            except Exception as exc:
                logger.debug("监控上报失败：%s", exc)

    def snapshot(self) -> dict:
        s = self.loop.snapshot()
        hour = self.clock.now_hour()
        # 睡眠联动：夜间被唤醒（对话/吓醒）后不再显示"睡觉"，保持期过后睡回去
        sleeping = self.loop.is_sleeping(hour)
        sl = self.loop._sleepiness_curve(hour)
        weather = s["weather"]["base"]
        # monitor 兼容三轴（mood/energy/social 与旧结构同形；平静≈0）
        mood = round((weather - 0.35) * 10.0, 2)
        weather_label = s["weather"]["label"]   # 唯一权威映射（affect._label）
        prof = self.loop.seed.profile()

        # 会话状态联动：聆听/应答中显示 chatting/muttering（即使在夜间被唤醒）
        sess_phase = self._session_phase()
        if sess_phase == "LISTEN":
            state, attention = "chatting", "focus"
        elif sess_phase == "RESPOND":
            state, attention = "muttering", "focus"
        else:
            state = "rest" if sleeping else "idle"
            attention = "rest" if sleeping \
                else ("wandering" if sl < 0.25 else "focus")
        return {
            "state": state,
            "attention": attention,
            "physio": {
                "mood": mood,
                "energy": round(10.0 - sl * 8.0, 2),
                "social": round(min(10.0, s["tension"].get("attachment", 0.0)
                                    * 8.0 + s["tension"].get("loneliness", 0.0)
                                    * 6.0), 2),
                "mood_value": weather_label,
                "summary": (f"{'睡着了' if sleeping else '醒着'}"
                            + (f" · {sess_phase_zh(sess_phase)}"
                               if sess_phase else "")
                            + f" · 天气{weather_label} · 呼吸 "
                            f"{s['breath']['rate']:.0f}bpm · "
                            f"信任 {s['trust']} · 自我深度 {s['self_depth']}"),
            },
            "activity": {
                "now": (sess_phase_zh(sess_phase) if sess_phase
                        else ("睡着了……" if sleeping
                              else (self._activity_desc
                                    or "安静待机，竖着耳朵听动静"))),
                "intent": self._last_thought_desc,
            },
            "queue": 0,
            "last_thought": self._last_thought_desc,
            "action_backend": "pulse",
            "pulse": {
                "breath": s["breath"],
                "struggle": s["struggle"],
                "weather": {"base": weather, "pulse": s["weather"]["pulse"],
                            "label": weather_label},
                "trust": s["trust"],
                "self_depth": s["self_depth"],
                "owner_present": s["owner_present"],
                "sleeping": sleeping,
                "sess_phase": sess_phase,
                "last_chat_mood": (self._last_chat_mood
                                   if self._mono() - self._last_chat_mood_t <= 60.0
                                   else ""),
                "seed": prof,
            },
        }

    def _session_phase(self) -> str:
        """读会话当前 phase（IDLE/LISTEN/RESPOND）；无 session 时为空。"""
        if self.session is None:
            return ""
        st = getattr(self.session, "state", None)
        return getattr(st, "name", st or "") or ""


def sess_phase_zh(phase: str) -> str:
    return {"LISTEN": "正在听主人说话",
            "RESPOND": "正在回应主人"}.get(phase, "")


class _LocalClock:
    """真实本地时钟（小时/分钟/日）。测试可注入替代。"""

    def now_hour(self) -> float:
        lt = time.localtime()
        return lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0

    def now_minute(self) -> int:
        return time.localtime().tm_min

    def now_day(self) -> int:
        lt = time.localtime()
        return lt.tm_yday
