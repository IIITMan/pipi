"""唤醒词语音循环 —— 免提交互的主循环。

流程：
  💤 待唤醒：短窗探测环境音，识别出唤醒词（如「皮皮」）即唤醒
  👂 会话中：直接说话即提问（ASR → on_query 回调 → TTS 播报回复）
  安静 sleep_after_s 秒后自动回到待唤醒

唤醒词识别复用 SenseVoice（零额外依赖）：它本来就够快（CPU RTF≈0.06），
且能顺带识别唤醒词后面的正文（「皮皮，今天几号」→ 直接拿「今天几号」提问）。

隐私：所有音频只在内存里转一圈，识别完即丢弃。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable

from pipi.providers.asr.mic import MicCapture

logger = logging.getLogger(__name__)

# 唤醒后正文的合法起始（去掉紧跟唤醒词的标点）
_LEADING_PUNCT = re.compile(r"^[\s，。,．.！!？?、~～]*")


class VoiceLoop:
    """待唤醒 / 会话 双状态语音循环。

    Args:
        asr: 语音识别引擎（需实现 transcribe(bytes, sample_rate)）
        capture: MicCapture 麦克风采集器
        wake_words: 唤醒词列表
        tts: 可选语音输出（需实现 say(text, emotion)）
        on_query: 会话回调，收到正文后调用；负责生成回复与播报
        on_state: 可选状态回调（"passive" / "awake" / "ignore"）
    """

    def __init__(
        self,
        asr: Any,
        capture: MicCapture,
        wake_words: tuple[str, ...] = ("皮皮", "pipi", "小皮"),
        tts: Any | None = None,
        on_query: Callable[[str], Awaitable[None]] | None = None,
        on_state: Callable[[str, str], None] | None = None,
        passive_max_s: float = 6.0,
        sleep_after_s: float = 30.0,
        wake_ack: str = "嗯，我在。",
    ):
        self.asr = asr
        self.capture = capture
        self.wake_words = tuple(w.lower() for w in wake_words)
        self.tts = tts
        self.on_query = on_query
        self.on_state = on_state
        self.passive_max_s = passive_max_s
        self.sleep_after_s = sleep_after_s
        self.wake_ack = wake_ack
        self._stop = False
        self._active_until = 0.0
        # 调试统计：最近一轮
        self.last_asr_ms = 0
        self.last_utt_s = 0.0

    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        """主循环。on_query 里抛异常不会打断循环。"""
        self._notify("passive", "")
        while not self._stop:
            active = self._active_until > time.monotonic()
            try:
                audio = await asyncio.get_running_loop().run_in_executor(
                    None, self.capture.record_until_silence,
                    None if active else self.passive_max_s,
                )
            except Exception as exc:
                logger.warning("录音失败，1s 后重试：%s", exc)
                await asyncio.sleep(1.0)
                continue
            if self._stop:
                return
            if audio.size == 0 or self.capture.level_of(audio) < self.capture.threshold:
                continue

            try:
                _t0 = time.perf_counter()
                t = await self.asr.transcribe(audio.tobytes(), self.capture.sample_rate)
                self.last_asr_ms = int((time.perf_counter() - _t0) * 1000)
                self.last_utt_s = len(audio) / self.capture.sample_rate
            except Exception as exc:
                logger.warning("识别失败：%s", exc)
                continue
            text = (t.text or "").strip()
            if not text:
                continue

            if active:
                # 会话中：整句都是正文
                self._active_until = time.monotonic() + self.sleep_after_s
                await self._fire(text)
            else:
                await self._handle_passive(text)

    # ------------------------------------------------------------------

    async def _handle_passive(self, text: str) -> None:
        hit, rest = self._match_wake(text)
        if not hit:
            self._notify("ignore", text)
            return
        # 唤醒成功
        self._active_until = time.monotonic() + self.sleep_after_s
        self._notify("awake", rest or text)
        if rest:
            await self._fire(rest)
        elif self.tts is not None:
            try:
                await self.tts.say(self.wake_ack)
            except Exception as exc:
                logger.debug("唤醒应答播报失败：%s", exc)

    def _match_wake(self, text: str) -> tuple[bool, str]:
        """判断文本是否含唤醒词；返回 (是否命中, 唤醒词后的正文)。"""
        low = text.lower()
        for w in self.wake_words:
            idx = low.find(w)
            if idx < 0:
                continue
            rest = text[idx + len(w):]
            rest = _LEADING_PUNCT.sub("", rest).strip()
            if len(rest) >= 2:
                return True, rest
            return True, ""
        return False, ""

    async def _fire(self, text: str) -> None:
        if self.on_query is None:
            return
        try:
            await self.on_query(text)
        except Exception as exc:
            logger.warning("处理语音指令失败：%s", exc)

    def _notify(self, state: str, text: str) -> None:
        if self.on_state is not None:
            try:
                self.on_state(state, text)
            except Exception:
                pass


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
        try:
            async for chunk in self.tts.synthesize(TTSRequest(text=text)):
                if chunk.pcm:
                    buf.extend(chunk.pcm)
        except Exception as exc:
            logger.warning("分段合成失败：%s", exc)
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
