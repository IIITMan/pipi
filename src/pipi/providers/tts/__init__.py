"""TTS Provider 工厂。"""

from __future__ import annotations

import logging
from typing import Any

from pipi.providers.tts.base import AudioChunk, TTSProvider, TTSRequest
from pipi.providers.tts.edge import EdgeTTS
from pipi.providers.tts.mock import MockTTS

logger = logging.getLogger(__name__)


def create_tts(name: str, cfg: Any) -> TTSProvider | None:
    if name == "ark_tts":
        from pipi.providers.tts.ark import ArkTTS

        sub = cfg.get("audio.tts.ark_tts", {}) if hasattr(cfg, "get") else {}
        p = ArkTTS(
            api_key=sub.get("api_key", ""),
            resource_id=sub.get("resource_id", "seed-tts-2.0"),
            speaker=sub.get("speaker", "zh_female_vv_uranus_bigtts"),
            model=sub.get("model", ""),
            sample_rate=sub.get("sample_rate", 24000),
            bitrate=sub.get("bitrate", 160000),
            emotion_instruction=sub.get("emotion_instruction", True),
            cache_enabled=sub.get("cache_enabled", True),
            cache_dir=sub.get("cache_dir", "data/tts_cache"),
            pitch=sub.get("pitch", 0),
            speed=sub.get("speed", 1.0),
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
    "ArkTTS", "EdgeTTS", "MockTTS",
    "create_tts", "create_mock_tts",
]
