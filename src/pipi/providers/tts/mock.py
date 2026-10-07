"""Mock TTS —— 不发声，只打印。

用于无音频设备/无模型的环境，依然能看到"皮皮说了什么"。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from pipi.providers.tts.base import AudioChunk, TTSRequest


class MockTTS:
    """把合成结果打印出来，模拟"说话"。"""

    name = "mock"

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self._stopped = False
        self.spoken: list[str] = []

    def is_available(self) -> bool:
        return True

    async def synthesize(self, req: TTSRequest) -> AsyncIterator[AudioChunk]:
        self._stopped = False
        if self.verbose:
            emo = f"({req.emotion})" if req.emotion != "neutral" else ""
            print(f"  [语音{emo}] {req.text}")
        self.spoken.append(req.text)
        # 模拟播放耗时（按字数估）
        await asyncio.sleep(min(1.2, len(req.text) * 0.04))
        yield AudioChunk(pcm=b"", sample_rate=24000, is_last=True)

    async def stop(self) -> None:
        self._stopped = True

    async def say(self, text: str, emotion: str = "neutral") -> None:
        async for _ in self.synthesize(TTSRequest(text=text, emotion=emotion)):
            pass
