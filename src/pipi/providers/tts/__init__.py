"""TTS Provider 工厂。"""

from __future__ import annotations

import logging
from typing import Any

from pipi.providers.tts.base import AudioChunk, TTSProvider, TTSRequest
from pipi.providers.tts.cosyvoice import CosyVoiceTTS
from pipi.providers.tts.edge import EdgeTTS
from pipi.providers.tts.mock import MockTTS

logger = logging.getLogger(__name__)


def create_tts(name: str, cfg: Any) -> TTSProvider | None:
    if name == "cosyvoice":
        sub = cfg.get("audio.tts.cosyvoice", {}) if hasattr(cfg, "get") else {}
        p = CosyVoiceTTS(
            model=sub.get("model", "iic/CosyVoice-300M"),
            device=sub.get("device", "auto"),
            speaker=sub.get("speaker", "中文女"),
        )
        return p if p.is_available() else None

    if name == "edge_tts":
        sub = cfg.get("audio.tts.edge_tts", {}) if hasattr(cfg, "get") else {}
        p = EdgeTTS(voice=sub.get("voice", "zh-CN-XiaoxiaoNeural"))
        return p if p.is_available() else None

    if name == "mock":
        return MockTTS()

    logger.debug("TTS Provider %s 不可用", name)
    return None


def create_mock_tts() -> MockTTS:
    return MockTTS()


__all__ = [
    "TTSProvider", "TTSRequest", "AudioChunk",
    "CosyVoiceTTS", "EdgeTTS", "MockTTS",
    "create_tts", "create_mock_tts",
]
