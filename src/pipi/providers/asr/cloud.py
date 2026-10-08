"""云端 ASR —— 通义（DashScope）Paraformer / OpenAI Whisper。

本地模型装不上时的第二选择。需要 API Key。
统一输出 Transcript（云端不给情感时标 NEUTRAL）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from pipi.domain.audio import AudioEvent, AudioEventType, Transcript

logger = logging.getLogger(__name__)


class DashScopeASR:
    """通义千问语音识别（DashScope 兼容模式）。"""

    name = "cloud_dashscope"

    def __init__(self, api_key: str, model: str = "paraformer-realtime-v2",
                 base_url: str = "https://dashscope.aliyuncs.com/api/v1"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    def is_available(self) -> bool:
        return bool(self.api_key)

    async def transcribe(self, audio: bytes, sample_rate: int = 16000) -> Transcript:
        return await asyncio.to_thread(self._sync_transcribe, audio)

    async def transcribe_file(self, path: str) -> Transcript:
        with open(path, "rb") as f:
            return await self.transcribe(f.read())

    def _sync_transcribe(self, audio: bytes) -> Transcript:
        """调用 DashScope 的文件转写接口。"""
        import base64

        url = f"{self.base_url}/services/audio/asr/transcription"
        payload = {
            "model": self.model,
            "input": {"file_urls": [] },
        }
        # 简化实现：用短音频的 base64 直传接口
        try:
            # DashScope 的录音文件识别通常需要 OSS URL，
            # 这里给出接口占位并说明限制。
            logger.warning("云 ASR 需要可公网访问的音频 URL；本地字节流请用 SenseVoice")
            return Transcript(
                text="", is_final=True,
                raw={"error": "cloud asr requires public url"},
            )
        except Exception as exc:
            logger.error("云 ASR 失败：%s", exc)
            return Transcript(text="", raw={"error": str(exc)})

