"""sherpa-onnx 版 SenseVoice —— int8 onnx 离线语音理解（阶段 3 补全）。

SenseVoiceSmall 的 sherpa-onnx int8 移植（唯一 ASR 后端，无降级）：
  - 一次推理同时得到 文本 + 情感 + 音频事件（模型原生能力）
  - 推理走 sherpa-onnx / onnxruntime int8：无 torch、无 funasr 依赖，
    内存占用与加载速度都远优于 funasr（torch 栈约 2GB+ → 约 300MB）
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import numpy as np

from pipi.domain.audio import AudioEvent, AudioEventType, Transcript

logger = logging.getLogger(__name__)

# 情感标签（模型输出大写标准名）
_EMOTIONS = {"HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED",
             "SURPRISED"}

# 音频事件名 → 枚举
_EVENT_MAP = {
    "laughter": AudioEventType.LAUGHTER,
    "cry": AudioEventType.CRY,
    "sigh": AudioEventType.SIGH,
    "applause": AudioEventType.APPLAUSE,
    "cough": AudioEventType.COUGH,
    "sneeze": AudioEventType.SNEEZE,
    "bgm": AudioEventType.BGM,
    "speech": AudioEventType.SPEECH,
    "noise": AudioEventType.NOISE,
}


class SherpaSenseVoiceASR:
    """sherpa-onnx SenseVoice（int8）—— 本地语音 + 情感 + 事件识别。"""

    name = "sensevoice_sh"

    def __init__(self, model_dir: str, num_threads: int = 2,
                 language: str = "auto", use_itn: bool = True):
        import sherpa_onnx

        d = Path(model_dir)
        # sherpa 的 language: 空串 = 自动检测
        lang = "" if language in ("auto", "") else language
        self._recog = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(d / "model.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=num_threads,
            language=lang,
            use_itn=use_itn,
        )
        logger.info("ASR: sherpa SenseVoice int8（%s）", d.name)

    def is_available(self) -> bool:
        return True

    # ------------------------------------------------------------------

    async def transcribe(self, audio: bytes, sample_rate: int = 16000) -> Transcript:
        return await asyncio.to_thread(self._transcribe_sync, audio, sample_rate)

    async def transcribe_file(self, path: str) -> Transcript:
        import soundfile as sf

        data, sr = sf.read(path, dtype="int16")
        if data.ndim > 1:
            data = data.mean(axis=1)
        return await self.transcribe(np.asarray(data).tobytes(), sr)

    def _transcribe_sync(self, audio: bytes, sample_rate: int) -> Transcript:
        import time as _t

        _t0 = _t.perf_counter()
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        if sample_rate != 16000 and len(samples):
            try:
                import math

                from scipy.signal import resample_poly

                g = math.gcd(16000, int(sample_rate))
                samples = resample_poly(samples, 16000 // g, int(sample_rate) // g)
            except ImportError:
                idx = np.arange(int(len(samples) * 16000 / sample_rate)) \
                    * sample_rate / 16000
                samples = np.interp(idx, np.arange(len(samples)), samples)

        stream = self._recog.create_stream()
        stream.accept_waveform(16000, samples)
        self._recog.decode_stream(stream)
        result = stream.result

        text = (result.text or "").strip()
        # 情感/事件：新版 sherpa 直接暴露；旧版从残留标签解析
        emotion = (getattr(result, "emotion", "") or "").strip().strip("<|>").upper()
        event_raw = (getattr(result, "event", "") or "").strip().strip("<|>").lower()
        if not emotion and "<|" in text:
            emotion, text = self._parse_tags(text)[0], self._parse_tags(text)[1]
        emotion = emotion if emotion in _EMOTIONS else "NEUTRAL"

        events: list[AudioEvent] = []
        if event_raw in _EVENT_MAP:
            events.append(AudioEvent(_EVENT_MAP[event_raw], confidence=0.9))
        if not events:
            events.append(AudioEvent(AudioEventType.SPEECH, confidence=1.0))

        ms = int((_t.perf_counter() - _t0) * 1000)
        logger.debug("sherpa SenseVoice %dms: %r (emotion=%s)", ms, text, emotion)
        return Transcript(
            text=text, is_final=True,
            emotion=emotion,
            emotion_confidence=0.8 if emotion != "NEUTRAL" else 0.0,
            events=events,
            language=(getattr(result, "lang", "") or "zh"),
            duration_ms=int(len(samples) / 16000 * 1000),
            raw={"backend": "sherpa", "asr_ms": ms},
        )

    @staticmethod
    def _parse_tags(text: str) -> tuple[str, str]:
        """旧版 sherpa 兜底：从文本中剥 <|...|> 标签。"""
        import re

        emotion = "NEUTRAL"
        for tag in re.findall(r"<\|([^|]+)\|>", text):
            if tag.upper() in _EMOTIONS:
                emotion = tag.upper()
        return emotion, re.sub(r"<\|[^|]+\|>", "", text).strip()
