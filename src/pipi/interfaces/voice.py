"""SentenceTTSPlayer —— 流式 TTS 播放器（边生成边播放）。

免提交互主循环在 session.py（InteractionSession 四阶段状态机）。
旧的 VoiceLoop 免提实现已移除：被 InteractionSession 取代的死代码，
其 sleep_after_s 从未在现链路生效（会话时长由 mic.session.timeout_s 控制）。
隐私：所有音频只在内存里转一圈，识别完即丢弃。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)


class SentenceTTSPlayer:
    """流式 TTS 播放器 —— 真·边生成边播放。

    双层流水线：
      1. 合成层：feed(delta) 累积 LLM 增量，按句末标点切句，
         每个完整句子立刻起后台合成任务（与 LLM 生成并行）；
      2. 播放层：后台消费者按顺序取已合成的句子立即播放——
         第一句合成完就出声，不等 LLM 说完整段。

    finish() 只负责冲刷尾部并等待播完；abort() 供 Barge-in 立即清空。
    """

    _SENT_END = re.compile(r"[。！？!?…；;\n]")

    @staticmethod
    def _has_content(seg: str) -> bool:
        """段内是否含实际文字（防"～""……"等纯标点碎片浪费一次 TTS）。"""
        return any(ch.isalnum() for ch in seg)

    def __init__(self, tts: Any, max_pending: int = 6):
        self.tts = tts
        self.max_pending = max_pending
        self._buf = ""
        self._tasks: list[asyncio.Task] = []   # 按句序排列的合成任务
        self._consumed = 0                     # 播放层已消费到的位置
        self._closed = False
        self._player: asyncio.Task | None = None

    async def feed(self, delta: str) -> None:
        self._buf += delta
        while True:
            m = self._SENT_END.search(self._buf)
            if not m:
                break
            seg, self._buf = self._buf[:m.end()], self._buf[m.end():]
            if seg.strip() and self._has_content(seg):
                self._enqueue(seg)

    async def finish(self) -> None:
        """冲刷剩余文本，等待播放层播完全部句子。"""
        if self._buf.strip():
            self._enqueue(self._buf)
            self._buf = ""
        self._closed = True
        if self._player is not None:
            await self._player

    def abort(self) -> None:
        """Barge-in：立刻停止播放、丢弃所有未播段落。"""
        self._closed = True
        self._buf = ""
        if self._player is not None:
            self._player.cancel()
            self._player = None
        pending, self._tasks = self._tasks, []
        self._consumed = 0
        for task in pending:
            task.cancel()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------

    def _enqueue(self, text: str) -> None:
        if len(self._tasks) - self._consumed >= self.max_pending:
            return  # 播放远落后于生成时丢弃，防积压
        self._tasks.append(asyncio.create_task(self._synth(text)))
        # 首句入队即启动播放消费者——不等 LLM 说完
        if self._player is None or self._player.done():
            self._player = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """播放消费者：按句序取合成结果，立即播放。"""
        while True:
            if self._consumed < len(self._tasks):
                task = self._tasks[self._consumed]
                audio = await task
                await self._play(audio)
                self._consumed += 1
            elif self._closed:
                return
            else:
                await asyncio.sleep(0.03)

    # ------------------------------------------------------------------

    async def _synth(self, text: str) -> bytes:
        from pipi.providers.tts.base import TTSRequest

        buf = bytearray()
        # edge-tts 偶发 "No audio was received"，重试一次
        for attempt in (1, 2):
            buf = bytearray()
            try:
                async for chunk in self.tts.synthesize(TTSRequest(text=text)):
                    if chunk.pcm:
                        buf.extend(chunk.pcm)
                if buf:
                    break
                logger.warning("第 %d 次合成为空：%r", attempt, text[:20])
            except Exception as exc:
                logger.warning("分段合成失败（第 %d 次）：%s", attempt, exc)
            if attempt == 1:
                await asyncio.sleep(0.6)
        return bytes(buf)

    async def _play(self, mp3: bytes) -> None:
        if not mp3:
            return
        try:
            import io

            import sounddevice as sd
            import soundfile as sf

            data, sr = sf.read(io.BytesIO(mp3), dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)
            sd.play(data, sr)
            await asyncio.to_thread(sd.wait)
        except Exception as exc:
            logger.warning("分段播放失败：%s", exc)


__all__ = ["VoiceLoop", "SentenceTTSPlayer"]
