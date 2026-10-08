"""声纹识别 —— sherpa-onnx SpeakerEmbedding（CAM++ ERes2Net，中文 16k）。

用途：多用户场景下区分「主人」与「陌生人」：
  1. 首次听到说话（且尚未建档）→ 自动登记为主人声纹（kv_store 持久化）；
  2. 之后每段语音算 embedding，与主人声纹做余弦相似度：
     ≥ threshold → user_id = "owner"；否则 → "stranger"（陌生人分轨）。

说明：embedding 只是一段 192 维向量，不是原始音频——原始音频照旧不落盘。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_KV_KEY = "voiceprint.owner.embedding"


def _cosine(a: list[float], b: list[float]) -> float:
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


class VoicePrintManager:
    """主人声纹注册 + 说话人识别（sherpa-onnx 离线）。"""

    name = "sherpa_voiceprint"

    def __init__(self, model_path: str, kv_repo: Any = None,
                 threshold: float = 0.5, auto_enroll: bool = True):
        import sherpa_onnx

        p = Path(model_path)
        if not p.is_absolute():
            from pipi.infra.config import PROJECT_ROOT

            p = PROJECT_ROOT / p
        if not p.exists():
            raise FileNotFoundError(f"声纹模型缺失：{p}")
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(p), num_threads=2)
        self._ext = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.kv = kv_repo
        self.threshold = float(threshold)
        self.auto_enroll = bool(auto_enroll)
        self._owner_emb: list[float] | None = None
        self._load_owner()
        logger.info("声纹识别就绪（%s，已建档=%s，阈值=%.2f）",
                    p.name, self.enrolled, self.threshold)

    # ------------------------------------------------------------------

    @property
    def enrolled(self) -> bool:
        return self._owner_emb is not None

    def is_available(self) -> bool:
        return True

    def _load_owner(self) -> None:
        if self.kv is None:
            return
        raw = self.kv.get(_KV_KEY)
        if raw:
            try:
                self._owner_emb = json.loads(raw)
            except Exception:
                self._owner_emb = None

    def _embed(self, audio: np.ndarray) -> list[float] | None:
        """int16 音频 → 说话人 embedding（短于 0.6s 或全静音返回 None）。"""
        samples = np.asarray(audio, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) < 16000 * 0.6:
            return None
        if float(np.sqrt(np.mean(samples ** 2))) < 0.005:
            return None
        stream = self._ext.create_stream()
        stream.accept_waveform(16000, samples)
        emb = self._ext.compute(stream)
        return emb if emb else None

    def enroll_owner(self, audio: np.ndarray) -> bool:
        """登记主人声纹（kv_store 持久化，跨重启有效）。"""
        emb = self._embed(audio)
        if emb is None:
            return False
        self._owner_emb = emb
        if self.kv is not None:
            self.kv.set(_KV_KEY, json.dumps(emb), time.time())
        logger.info("主人声纹已登记")
        return True

    def reset_owner(self) -> None:
        """清掉主人声纹（下次说话重新自动登记）。"""
        self._owner_emb = None
        if self.kv is not None:
            self.kv.set(_KV_KEY, "", time.time())

    def identify(self, audio: np.ndarray) -> str | None:
        """识别说话人。返回 "owner" | "stranger" | None（音频太短/未启用）。"""
        emb = self._embed(audio)
        if emb is None:
            return None
        if not self.enrolled:
            if self.auto_enroll:
                self._owner_emb = emb
                if self.kv is not None:
                    self.kv.set(_KV_KEY, json.dumps(emb), time.time())
                logger.info("主人声纹自动登记（第一段语音）")
                return "owner"
            return None
        sim = _cosine(emb, self._owner_emb)
        logger.debug("声纹相似度 %.3f（阈值 %.2f）", sim, self.threshold)
        return "owner" if sim >= self.threshold else "stranger"
