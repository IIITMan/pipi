"""edge-tts TTS —— 微软免费的在线语音合成。

无 GPU 环境的最佳性价比兜底：
  - 完全免费，无需 API Key
  - 有网络即可用
  - 中文音色自然度高

依赖：pip install edge-tts
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import AsyncIterator

from pipi.providers.tts.base import AudioChunk, TTSRequest

logger = logging.getLogger(__name__)


class EdgeTTS:
    """基于 edge-tts 的语音合成。"""

    name = "edge_tts"

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural"):
        self.voice = voice
        self._stopped = False
        self._proc = None

    def is_available(self) -> bool:
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            logger.debug("未安装 edge-tts（pip install edge-tts）")
            return False

    async def synthesize(self, req: TTSRequest) -> AsyncIterator[AudioChunk]:
        """合成 mp3。注意 edge-tts 输出 mp3 而非 pcm。"""
        try:
            import edge_tts
        except ImportError:
            yield AudioChunk(pcm=b"", is_last=True)
            return

        self._stopped = False
        communicate = edge_tts.Communicate(req.text, req.voice_id or self.voice)
        buf = bytearray()
        try:
            async for chunk in communicate.stream():
                if self._stopped:
                    break
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
        except Exception as exc:
            logger.error("edge-tts 失败：%s", exc)

        # edge-tts 返回 mp3，用 ffplay/mpg123 播放
        if buf:
            yield AudioChunk(pcm=bytes(buf), sample_rate=24000, is_last=True)
        else:
            yield AudioChunk(pcm=b"", is_last=True)

    async def stop(self) -> None:
        self._stopped = True
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass

    async def say(self, text: str, emotion: str = "neutral") -> None:
        """直接播放。"""
        import shutil
        import subprocess

        try:
            import edge_tts
        except ImportError:
            logger.debug("edge-tts 不可用，跳过语音输出")
            return

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        try:
            communicate = edge_tts.Communicate(text, self.voice)
            await communicate.save(path)

            for player, args in (
                ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]),
                ("mpg123", ["mpg123", "-q", path]),
                ("afplay", ["afplay", path]),
            ):
                if shutil.which(player):
                    self._proc = subprocess.Popen(args)
                    await asyncio.to_thread(self._proc.wait)
                    return

            # 没有 ffmpeg/mpg123 时（典型如裸 Windows），用 sounddevice 播放
            try:
                import sounddevice as sd
                import soundfile as sf

                data, sr = sf.read(path, dtype="float32")
                if data.ndim > 1:
                    data = data.mean(axis=1)
                sd.play(data, sr)
                await asyncio.to_thread(sd.wait)
                return
            except ImportError:
                logger.debug("无可用音频播放器（ffplay/mpg123/sounddevice）")
        except Exception as exc:
            logger.debug("edge-tts 播放失败：%s", exc)
        finally:
            self._proc = None
            try:
                os.unlink(path)
            except OSError:
                pass
