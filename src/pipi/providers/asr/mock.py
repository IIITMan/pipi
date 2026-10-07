"""Mock ASR —— 键盘输入 / 脚本注入。

无麦克风、无模型时的兜底。支持两种模式：
  1. 交互式：从 stdin 读一行
  2. 脚本式：从预设队列吐文本（用于 --sim 自动化演示）
"""

from __future__ import annotations

import asyncio
from typing import Iterable

from pipi.domain.audio import AudioEvent, AudioEventType, Transcript


class MockASR:
    """把"文本输入"伪装成语音识别结果。"""

    name = "mock"

    def __init__(self, scripted: Iterable[str] | None = None):
        self._queue: list[str] = list(scripted or [])
        self._idx = 0

    def is_available(self) -> bool:
        return True

    def feed_script(self, lines: Iterable[str]) -> None:
        """追加脚本台词。"""
        self._queue.extend(lines)

    async def transcribe(self, audio: bytes, sample_rate: int = 16000) -> Transcript:
        return await self._next_transcript()

    async def transcribe_file(self, path: str) -> Transcript:
        return await self._next_transcript()

    async def _next_transcript(self) -> Transcript:
        # 优先用脚本
        if self._idx < len(self._queue):
            text = self._queue[self._idx]
            self._idx += 1
        else:
            # 退到 stdin
            try:
                text = await asyncio.to_thread(input, "你（说话）> ")
            except (EOFError, KeyboardInterrupt):
                text = ""
        return _infer_transcript(text)


def _infer_transcript(text: str) -> Transcript:
    """从文本粗略推断情感与音频事件 —— 让模拟更真实。"""
    events: list[AudioEvent] = []
    emotion, conf = "NEUTRAL", 0.0

    neg = ["累", "烦", "难过", "难受", "压力", "崩", "哭", "焦虑", "不开心", "郁闷"]
    pos = ["开心", "高兴", "太棒", "哈哈", "笑死", "成功", "喜欢"]

    if any(w in text for w in neg):
        emotion, conf = "SAD", 0.7
    elif any(w in text for w in pos):
        emotion, conf = "HAPPY", 0.75

    if "哈哈" in text or "笑" in text:
        events.append(AudioEvent(AudioEventType.LAUGHTER, 0.9))
    if "唉" in text or "哎" in text:
        events.append(AudioEvent(AudioEventType.SIGH, 0.7))
    if "哭" in text:
        events.append(AudioEvent(AudioEventType.CRY, 0.8))

    return Transcript(
        text=text, is_final=True, emotion=emotion,
        emotion_confidence=conf, events=events,
        language="zh", duration_ms=len(text) * 200,
        raw={"source": "mock"},
    )
