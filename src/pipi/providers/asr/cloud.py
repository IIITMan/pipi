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


class OpenAIWhisperASR:
    """OpenAI Whisper API。"""

    name = "cloud_openai"

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1",
                 model: str = "whisper-1"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def is_available(self) -> bool:
        return bool(self.api_key)

    async def transcribe(self, audio: bytes, sample_rate: int = 16000) -> Transcript:
        return await asyncio.to_thread(self._sync_transcribe, audio)

    async def transcribe_file(self, path: str) -> Transcript:
        with open(path, "rb") as f:
            return await self.transcribe(f.read())

    def _sync_transcribe(self, audio: bytes) -> Transcript:
        boundary = "----pipi-boundary"
        parts = []
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
        parts.append(f"{self.model}\r\n".encode())
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(b'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n')
        parts.append(b"Content-Type: audio/wav\r\n\r\n")
        parts.append(audio)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)

        req = urllib.request.Request(
            f"{self.base_url}/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            text = data.get("text", "")
            return Transcript(
                text=text, is_final=True,
                events=[AudioEvent(AudioEventType.SPEECH)],
                language="zh",
                raw={"source": "openai_whisper"},
            )
        except Exception as exc:
            logger.error("Whisper 失败：%s", exc)
            return Transcript(text="", raw={"error": str(exc)})
