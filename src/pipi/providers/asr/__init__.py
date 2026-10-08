"""ASR Provider 工厂。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pipi.providers.asr.base import ASRProvider
from pipi.providers.asr.cloud import DashScopeASR
from pipi.providers.asr.mock import MockASR

logger = logging.getLogger(__name__)


def create_asr(name: str, cfg: Any) -> ASRProvider | None:
    """按名称创建 ASR Provider。不可用时返回 None。"""
    if name == "sensevoice":
        # 唯一后端：sherpa-onnx SenseVoice int8（无 torch/funasr，不降级）
        from pipi.infra.config import PROJECT_ROOT

        sv = cfg.get("audio.asr.sensevoice", {}) if hasattr(cfg, "get") else {}
        model_dir = sv.get(
            "sherpa_model_dir",
            "data/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17")
        mp = Path(model_dir)
        if not mp.is_absolute():
            mp = PROJECT_ROOT / mp
        if not (mp / "model.int8.onnx").exists():
            logger.error("SenseVoice 模型缺失：%s（唯一后端 sherpa-onnx）", mp)
            return None
        from pipi.providers.asr.sherpa_offline import SherpaSenseVoiceASR

        return SherpaSenseVoiceASR(
            str(mp),
            num_threads=int(sv.get("num_threads", 2)),
            language=sv.get("language", "auto"),
            use_itn=sv.get("use_itn", True),
        )

    if name == "cloud_dashscope":
        sub = cfg.get("audio.asr.cloud_dashscope", {}) if hasattr(cfg, "get") else {}
        key = sub.get("api_key", "") or cfg.get("llm.api_key", "")
        p = DashScopeASR(key, sub.get("model", "paraformer-realtime-v2"))
        return p if p.is_available() else None

    if name == "mock":
        return MockASR()

    logger.warning("未知 ASR Provider：%s", name)
    return None


def create_mock_asr() -> MockASR:
    return MockASR()


__all__ = [
    "ASRProvider", "DashScopeASR",
    "MockASR", "create_asr", "create_mock_asr",
]
