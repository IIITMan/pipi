"""ASR Provider 接口。"""

from __future__ import annotations

from typing import Protocol

from pipi.domain.audio import Transcript


class ASRProvider(Protocol):
    """语音识别接口。

    实现需一次性给出文本 + 情感 + 音频事件
    （SenseVoice 天然支持；云 API 至少给文本）。
    """

    name: str

    def is_available(self) -> bool: ...

    async def transcribe(self, audio: bytes, sample_rate: int = 16000) -> Transcript:
        """一次性识别整段音频。"""
        ...

    async def transcribe_file(self, path: str) -> Transcript:
        ...
