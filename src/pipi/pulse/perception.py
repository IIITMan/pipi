"""感知循环 —— 摄像头帧 → 离散事件 → PULSE（原 pipi.life.perception，
8→4 精简时随 legacy 删除迁入 pulse 包，是 PULSE 视觉接入的唯一消费者）。

设计要点（见 PERCEPTION_PLAN §8 评审修订）：
  - 本循环是 VisionPipeline.capture() 的**唯一消费者**；
    到场/离场由循环内部对比前后帧判定，不依赖 pipeline 的沿标志；
  - 表情事件只在状态转变时推一次（持续 sad 不重推，防 mood 被钉死）；
  - 陌生人/到场类事件分别有冷却与去重窗口；
  - `latest` 暴露最近一帧观测，供对话注入视觉上下文。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pipi.capabilities.perception import PerceptionService

logger = logging.getLogger(__name__)


@dataclass
class PerceptionEvent:
    """离散感知事件（只在状态变化时产生）。"""

    t: float
    kind: str            # owner_arrived | owner_gone | stranger | owner_expression
    text: str
    extra: dict = field(default_factory=dict)


class MonotonicClock:
    """感知循环用的单调时钟（事件去重基于运行时长，与时区无关）。"""

    def now(self) -> float:
        return time.monotonic()


class PerceptionLoop:
    """常驻感知循环：低频拍帧 → 事件判定 → 去重 → 回调。"""

    def __init__(self, vision: Any, clock: Any, cfg: dict,
                 on_event: Callable[[PerceptionEvent], None]):
        self.vision = vision
        self.clock = clock
        self.on_event = on_event
        cfg = cfg or {}
        self.fps = float(cfg.get("fps", 1.0))
        self.debounce_s = float(cfg.get("debounce_s", 30.0))
        self.stranger_cooldown_s = float(cfg.get("stranger_cooldown_s", 300.0))
        self._service = PerceptionService()

        self._running = False
        self._task: asyncio.Task | None = None
        self._prev_owner_present: bool | None = None
        self._prev_expression = ""
        self._last_event_t: dict[str, float] = {}

        # 对外状态：对话注入视觉 / 其他模块查询
        self.latest: Any | None = None          # 最近一帧 VisionObservation
        self.owner_present = False              # 最新在场状态

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="life-perception")
        logger.info("感知循环启动（fps=%.1f, debounce=%.0fs）",
                    self.fps, self.debounce_s)

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
        interval = 1.0 / max(self.fps, 0.1)
        empty_streak = 0          # 连续无帧计数（自愈用）
        reopen_fails = 0          # 重建失败次数（超限放弃，防反复触碰坏设备）
        while self._running:
            try:
                # 重推理（InsightFace/hsemotion，首次还有模型下载）丢进线程池，
                # 不阻塞事件循环 —— 否则麦克风帧泵堆积、TTS/对话全部卡顿
                if hasattr(self.vision, "camera"):
                    frame = await self.vision.camera.read()
                    obs = None
                    if frame is not None:
                        empty_streak = 0
                        reopen_fails = 0
                        obs = await asyncio.to_thread(
                            self.vision.process_frame, frame)
                        # 原帧挂到观测上：云端视觉识别/监控推流复用（免二次抓拍）
                        if obs is not None:
                            obs.raw["_frame"] = frame
                    else:
                        # 连续 ~10s 无帧 → 摄像头掉了（被占用/句柄未释放），
                        # 自动重建设备；连续 3 次失败就放弃（防反复触碰坏设备
                        # 触发 C++ abort），下次重启皮皮时工厂会重新探测
                        empty_streak += 1
                        if empty_streak >= 10 and reopen_fails < 3:
                            empty_streak = 0
                            reopen_fails += 1
                            logger.warning("摄像头连续无帧，重建设备（第 %d 次）…",
                                           reopen_fails)
                            try:
                                await self.vision.camera.close()
                            except Exception:
                                pass
                            try:
                                await self.vision.camera.open()
                                if getattr(self.vision.camera, "_cap", None) \
                                        is not None:
                                    reopen_fails = 0
                            except Exception as exc:
                                logger.warning("摄像头重建失败：%s", exc)
                            if reopen_fails >= 3:
                                logger.warning("摄像头多次重建失败，本次运行"
                                               "停用视觉（重启皮皮可重试）")
                else:
                    obs = await self.vision.capture()
                if obs is not None:
                    self.latest = obs
                    self.owner_present = obs.owner_present
                    for ev in self._events_from(obs):
                        self.on_event(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("感知帧异常（循环继续）：%s", exc)
            await asyncio.sleep(interval)

    def _events_from(self, obs: Any) -> list[PerceptionEvent]:
        """一帧观测 → 离散事件（沿触发 + 状态转变 + 去重）。"""
        now = self.clock.now()
        events: list[PerceptionEvent] = []
        prev = self._prev_owner_present
        present = bool(obs.owner_present)

        # 到场 / 离场：本循环自己对比前后帧（capture 唯一消费者，无竞争）
        if prev is not None and present != prev:
            kind = "owner_arrived" if present else "owner_gone"
            events.append(PerceptionEvent(
                now, kind, "主人回来了" if present else "主人走了"))
        self._prev_owner_present = present

        # 陌生人：检测到人脸但非主人
        stranger = bool(getattr(obs, "extra_person_count", 0) or
                        any(not f.is_owner for f in getattr(obs, "faces", [])))
        if stranger:
            events.append(PerceptionEvent(now, "stranger", "有陌生人出现"))

        # 表情：仅状态转变且需要关心时（持续 sad 不重推，防 mood 被钉死）
        ex = ""
        if obs.expressions.get("owner"):
            ex = obs.expressions["owner"].label
        if (ex and ex != self._prev_expression
                and PerceptionService.expression_to_comfort(ex)):
            events.append(PerceptionEvent(
                now, "owner_expression", f"主人好像不太开心（{ex}）",
                {"label": ex}))
        self._prev_expression = ex

        # 去重 / 冷却
        out: list[PerceptionEvent] = []
        for ev in events:
            gap = (self.stranger_cooldown_s if ev.kind == "stranger"
                   else self.debounce_s)
            if now - self._last_event_t.get(ev.kind, 0.0) < gap:
                continue
            self._last_event_t[ev.kind] = now
            out.append(ev)
        return out
