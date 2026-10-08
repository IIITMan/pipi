"""TTS Provider 接口。

关键设计：返回**异步流**的音频块。
这样播放可以边合成边播，且**打断**时能立刻停止。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass
class TTSRequest:
    """合成请求。"""

    text: str
    emotion: str = "neutral"      # 由 EmotionState 映射而来
    speed: float = 1.0
    voice_id: str | None = None
    instruction: str | None = None  # 情感/语气指令（豆包 2.0 语音指令等）


@dataclass
class AudioChunk:
    """一段音频。"""

    pcm: bytes
    sample_rate: int = 24000
    is_last: bool = False

    @property
    def duration_ms(self) -> int:
        # 16bit 单声道
        return int(len(self.pcm) / 2 / self.sample_rate * 1000)


class TTSProvider(Protocol):
    """语音合成接口。"""

    name: str

    def is_available(self) -> bool: ...

    async def synthesize(self, req: TTSRequest) -> AsyncIterator[AudioChunk]:
        ...

    async def stop(self) -> None:
        """打断当前播放。"""
        ...

    async def say(self, text: str, emotion: str = "neutral") -> None:
        """便捷方法：合成并播放。"""
        ...
