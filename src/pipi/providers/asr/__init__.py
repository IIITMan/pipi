"""ASR Provider 工厂。"""

from __future__ import annotations

import logging
from typing import Any

from pipi.providers.asr.base import ASRProvider
from pipi.providers.asr.cloud import DashScopeASR, OpenAIWhisperASR
from pipi.providers.asr.mock import MockASR
from pipi.providers.asr.sensevoice import SenseVoiceASR

logger = logging.getLogger(__name__)


def create_asr(name: str, cfg: Any) -> ASRProvider | None:
    """按名称创建 ASR Provider。不可用时返回 None。"""
    if name == "sensevoice":
        sv = cfg.get("audio.asr.sensevoice", {}) if hasattr(cfg, "get") else {}
        provider = SenseVoiceASR(
            model=sv.get("model", "iic/SenseVoiceSmall"),
            device=sv.get("device", "auto"),
            use_itn=sv.get("use_itn", True),
            language=sv.get("language", "auto"),
        )
        return provider if provider.is_available() else None

    if name == "cloud_dashscope":
        sub = cfg.get("audio.asr.cloud_dashscope", {}) if hasattr(cfg, "get") else {}
        key = sub.get("api_key", "") or cfg.get("llm.api_key", "")
        p = DashScopeASR(key, sub.get("model", "paraformer-realtime-v2"))
        return p if p.is_available() else None

    if name == "cloud_openai":
        sub = cfg.get("audio.asr.cloud_openai", {}) if hasattr(cfg, "get") else {}
        p = OpenAIWhisperASR(sub.get("api_key", ""))
        return p if p.is_available() else None

    if name == "mock":
        return MockASR()

    logger.warning("未知 ASR Provider：%s", name)
    return None


def create_mock_asr() -> MockASR:
    return MockASR()


__all__ = [
    "ASRProvider", "SenseVoiceASR", "DashScopeASR",
    "OpenAIWhisperASR", "MockASR", "create_asr", "create_mock_asr",
]
