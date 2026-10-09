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
                 motion: Any | None = None,
                 weather: Any | None = None,
                 loop: LivingLoop | None = None,
                 clock: Any = None,
                 mono: Any = None):
        p_cfg = (cfg or {}).get("pulse", {}) or {}
        self.cfg = p_cfg
        self.session = session
        self.vision = vision
        self.motion = motion          # 人脸跟随（云台 LOOK_AT）用
        # 真实天气源（WeatherService）：周期拉取 + 订阅对话查询触发 → 心境同步
        self.weather = weather
        self.weather_sync_s = float((cfg or {}).get("weather", {})
                                    .get("sync_interval_s", 1800.0))
        self._last_weather_kind: str | None = None
        self._weather_ext = ""        # 外面天气（中文），供快照/内心注入
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
        # 身体/行为（每帧缓存 → 监控展示；硬件端口 action_sink 预留）
        self._body_state: dict = {"lamp": 0.0, "eye": "正常",
                                  "pose": "松弛", "act": ""}
        self._behavior: dict = {"motion": "idle", "target": "none",
                                "zh": "安静地待着"}
        self.action_sink = None       # 硬件接入：赋值实现 ActionSink 协议的对象
        # 主动说话的 LLM 渲染端口（统一说话）：传入"内心事件"，返回一句自然台词；
        # 默认 None → 用固定句库。由 cli/orchestrator 注册（走 handle_proactive）。
        self.vocal_renderer: Callable[[str], Any] | None = None

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
        # 真实天气 → 心境：周期同步 + 对话查询触发（类别变化才调制）
        if self.weather is not None:
            self._tasks.append(asyncio.create_task(
                self._weather_sync_loop(), name="pulse-weather"))
            try:
                self.weather.subscribe(self._on_weather_change)
            except Exception as exc:
                logger.debug("天气订阅失败（周期同步仍可用）：%s", exc)
            self._sync_weather()   # 启动即同步一次（首次必然触发）
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
        # 人脸跟随：检测到人脸且偏离画面中心 → 云台 LOOK_AT
        v_cfg = (getattr(self, "_full_cfg", None) or {}).get("vision", {}) or {}
        if (self.motion is not None and v_cfg.get("follow", True)
                and self.vision is not None):
            self._follow_deadzone = float(v_cfg.get("follow_deadzone", 0.12))
            self._follow_cooldown = float(v_cfg.get("follow_cooldown_s", 2.0))
            self._last_follow_t = 0.0
            self._tasks.append(asyncio.create_task(
                self._follow_loop(), name="face-follow"))
            logger.info("人脸跟随已启动（deadzone=%.2f, cooldown=%.1fs）",
                        self._follow_deadzone, self._follow_cooldown)
        logger.info("PULSE 心搏启动（tick=%.1fs）", self.tick_s)

    def bind_cfg(self, full_cfg: Any) -> None:
        """绑定完整配置（供感知循环读 life.perception 参数）。"""
        self._full_cfg = full_cfg

    def latest_observation(self):
        """最新一帧视觉观测（感知循环产出；未启动/无帧返回 None）。

        对话应答应从这里取观测注入 prompt（PERCEPTION_PLAN ①裁定：
        感知循环是 capture() 的唯一消费者，对话侧不得抢拍，
        否则会吃掉 owner_newly_arrived 状态沿 → 回家问候丢失）。
        """
        perc = getattr(self, "_perception", None)
        return perc.latest if perc is not None else None

    async def _follow_loop(self) -> None:
        """人脸跟随：最新帧里取最大人脸，偏离画面中心超死区 → 云台 LOOK_AT。"""
        W, H = 640, 480   # 与 OpenCVCamera 输出一致
        while self._running:
            await asyncio.sleep(0.4)
            try:
                perc = getattr(self, "_perception", None)
                obs = perc.latest if perc is not None else None
                if obs is None or not obs.faces:
                    continue
                face = max(obs.faces,
                           key=lambda f: (f.bbox.w or 0) * (f.bbox.h or 0))
                b = face.bbox
                dx = (b.x + b.w / 2) / W - 0.5
                dy = (b.y + b.h / 2) / H - 0.5
                if abs(dx) <= self._follow_deadzone and \
                        abs(dy) <= self._follow_deadzone:
                    continue
                now = self._mono()
                if now - self._last_follow_t < self._follow_cooldown:
                    continue
                self._last_follow_t = now
                await self.motion.execute_gesture(
                    Gesture.LOOK_AT, target=(int(b.x + b.w / 2),
                                             int(b.y + b.h / 2)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("人脸跟随失败：%s", exc)

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
    # 内心状态 → 对话（消除"两张皮"的关键：LLM 每轮读到此刻真实身心）
    # ------------------------------------------------------------------

    def inner_brief(self) -> str:
        """渲染此刻真实的内心状态（第二人称，注入对话 system prompt）。
        覆盖：心境天气 / 睡眠刚醒 / 主人在场与思念 / 信任与认主 /
        当前意图 / 惊吓余韵。只写有信息量的行。"""
        s = self.loop.snapshot()
        hour = self.clock.now_hour()
        sleeping = self.loop.is_sleeping(hour)
        sim = self.loop.sim_seconds
        lines = [f'- 心境天气：{s["weather"]["label"]}']
        if self._weather_ext:
            lines.append(f"- 外面{self._weather_ext}"
                         "（这影响着你的心情：阴雨更蔫、晴天更有精神）")

        # 睡眠 / 刚被叫醒
        if sleeping:
            lines.append("- 你正在睡觉，是被他叫醒的，脑子和声音都还迷糊、发懵")
        elif sim < self.loop.wake_until and hour < 8.0:
            lines.append("- 你刚从睡梦里被叫醒没多久，还没完全缓过神")

        # 主人在场 / 离开与思念
        if s["owner_present"]:
            lines.append("- 他此刻就在你身边")
        else:
            sep_h = self.loop.sep_min / 60.0
            lines.append(f"- 他不在，已经离开约 {sep_h:.1f} 小时，"
                         "你心里的思念一直在涨")

        # 关系：信任与认主
        trust = s["trust"]
        if self.loop.bond.imprinted:
            rel = "你已经认定他是主人，满心都是他"
        elif trust < 40:
            rel = "你们还在熟悉阶段，你有点认生"
        elif trust < 70:
            rel = "你越来越信任、依赖他"
        else:
            rel = "你非常信任他，和他在一起很放松"
        lines.append(f"- 关系：信任 {trust:.0f}/100，{rel}")

        # 当前意图
        if self._last_thought_desc:
            lines.append(f"- 你心里此刻正想：{self._last_thought_desc}")

        # 惊吓余韵（60s 内）
        if self.loop._startle and sim - self.loop._startle_t < 60.0:
            lines.append("- 你刚刚被吓了一跳，心还在突突，语气会发紧、发虚")

        return ("你此刻真实的身心状态（你的语气、用词、内容必须和它一致，"
                "不能和它矛盾）：\n" + "\n".join(lines))

    # ------------------------------------------------------------------
    # 真实天气 → 心境同步（WeatherService → loop.observe_weather）
    # ------------------------------------------------------------------

    async def _weather_sync_loop(self) -> None:
        """周期拉取真实天气类别并同步（默认 30min；类别变化才调制心境）。"""
        while self._running:
            try:
                self._sync_weather()
            except Exception as exc:
                logger.debug("天气周期同步失败：%s", exc)
            await asyncio.sleep(max(self.weather_sync_s, 60.0))

    def _on_weather_change(self, kind: str) -> None:
        """对话查询天气成功后由 WeatherService 触发（同线程，直接同步）。"""
        try:
            self._sync_weather()
        except Exception as exc:
            logger.debug("天气订阅同步失败：%s", exc)

    def _sync_weather(self) -> None:
        """拉当前类别，与上次不同才注入 loop + 留事件日志。"""
        get_kind = getattr(self.weather, "kind", None)
        if get_kind is None:
            return
        kind = get_kind()
        if not kind or kind == self._last_weather_kind:
            return
        self._last_weather_kind = kind
        from pipi.capabilities.weather import KIND_ZH
        zh = KIND_ZH.get(kind, kind)
        self._weather_ext = zh
        text = self.loop.observe_weather(kind)
        self._event_log("weather", f"外面{zh}" + (f" · {text}" if text else ""))

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
        # 身体状态 + 意图→行为：缓存供监控，并驱动硬件端口（若已接）
        self._body_state = dict(snap["body"])
        self._activity_desc = snap["body"].get("act", "") or ""
        from pipi.pulse.expression import behavior_from_intent
        self._behavior = behavior_from_intent(
            snap.get("intent"), bool(self.loop.owner_present))
        if self.action_sink is not None:
            try:
                self.action_sink.apply(self._body_state, self._behavior)
            except Exception as exc:
                logger.debug("动作端口 apply 失败：%s", exc)

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
        # 克制：概率 × 主导强度
        strength = snap["tension"].waves.get(
            kind if kind != "useless" else "loneliness", 0.0)
        prob = self.vocal_prob * (0.5 + strength)
        if self._rng.random() >= prob:
            return
        # 命中：先占冷却，再异步表达（LLM 统一渲染，句库兜底）
        self._last_vocal = now
        fallback = self._rng.choice(_VOCALS.get(kind, ("（发了一会儿呆）",)))
        if fallback.startswith("("):     # 内心动作（哼歌/懒腰）：不发声
            self._event_log("inner", fallback)
            return
        try:
            asyncio.create_task(self._render_and_speak(kind, fallback))
        except RuntimeError:
            self._speak_fallback(fallback)

    async def _render_and_speak(self, kind: str, fallback: str) -> None:
        """统一说话：优先 LLM 按内心状态渲染；失败/无渲染器 → 固定句库。"""
        text = fallback
        if self.vocal_renderer is not None:
            try:
                r = self.vocal_renderer(self._vocal_event(kind))
                if asyncio.iscoroutine(r):
                    r = await r
                if r:
                    text = r
            except Exception as exc:
                logger.debug("主动说话 LLM 渲染失败，用句库：%s", exc)
        ok = self.session.try_speak_spontaneous(text)
        self._event_log("vocal" if ok else "muted", text)

    def _speak_fallback(self, text: str) -> None:
        ok = self.session.try_speak_spontaneous(text)
        self._event_log("vocal" if ok else "muted", text)

    def _vocal_event(self, kind: str) -> str:
        """构造给 LLM 的内心事件（结合意图理由/分离时长，一句话）。"""
        sep_h = self.loop.sep_min / 60.0
        if kind == "loneliness":
            return (f"你已经 {sep_h:.1f} 小时没见到主人了，有点想他、"
                    "也有点小委屈，想主动说句话")
        if kind == "curiosity":
            return "你察觉到一点动静，好奇心上来了，想主动念叨一句"
        if kind == "attachment":
            return "主人就在旁边，你心里暖暖的，想主动撒个娇"
        from pipi.pulse.organs.will import REASON
        return f"你{REASON.get(kind, '有点想搭话')}，主动开口说一句"

    def _describe_intent(self, snap: dict) -> str:
        intent = snap.get("intent")
        if intent is None:
            return ""
        from pipi.pulse.organs.will import REASON
        if intent.kind == "useless":
            return f"没什么要紧事，{intent.action or '发一会儿呆'}"
        return REASON.get(intent.kind, intent.kind)

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
                            f"{s['breath']['rate']:.0f}次/分 · "
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
                "weather_ext": self._weather_ext,
                "trust": s["trust"],
                "self_depth": s["self_depth"],
                "owner_present": s["owner_present"],
                "sleeping": sleeping,
                "sess_phase": sess_phase,
                "body": self._body_state,
                "behavior": self._behavior,
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
