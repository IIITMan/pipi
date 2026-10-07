"""SenseVoice ASR —— 阿里 FunAudioLLM 的语音理解模型。

一次推理同时得到：文本 + 情感 + 音频事件。
这是皮皮"听得懂情绪"的关键 —— 光靠文字会漏掉叹气、哭腔这些信号。

依赖：pip install funasr modelscope torch
模型：iic/SenseVoiceSmall（首次运行自动下载，约 900MB）
"""

from __future__ import annotations

import logging
from typing import Any

from pipi.domain.audio import AudioEvent, AudioEventType, Transcript

logger = logging.getLogger(__name__)

# SenseVoice 输出的情感标签 → 我们的标签
_EMOTION_MAP = {
    "<|HAPPY|>": "HAPPY",
    "<|SAD|>": "SAD",
    "<|ANGRY|>": "ANGRY",
    "<|NEUTRAL|>": "NEUTRAL",
    "<|FEARFUL|>": "FEARFUL",
    "<|DISGUSTED|>": "DISGUSTED",
    "<|SURPRISED|>": "SURPRISED",
}

# 音频事件标签 → 我们的枚举
_EVENT_MAP = {
    "<|Laughter|>": AudioEventType.LAUGHTER,
    "<|Cry|>": AudioEventType.CRY,
    "<|Sigh|>": AudioEventType.SIGH,
    "<|Applause|>": AudioEventType.APPLAUSE,
    "<|Cough|>": AudioEventType.COUGH,
    "<|Sneeze|>": AudioEventType.SNEEZE,
    "<|BGM|>": AudioEventType.BGM,
    "<|Speech|>": AudioEventType.SPEECH,
    "<|Noise|>": AudioEventType.NOISE,
}


class SenseVoiceASR:
    """基于 funasr 的本地语音识别。"""

    name = "sensevoice"

    def __init__(
        self,
        model: str = "iic/SenseVoiceSmall",
        device: str = "auto",
        use_itn: bool = True,
        language: str = "auto",
    ):
        self.model_id = model
        self.use_itn = use_itn
        self.language = language
        self._model: Any = None
        self._device = self._resolve_device(device)
        self._load_error: str | None = None

    @staticmethod
    def _resolve_device(device: str) -> str:
        if device != "auto":
            return device
        try:
            import torch
            return "cuda:0" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def is_available(self) -> bool:
        """探测依赖与模型是否就绪。"""
        try:
            import funasr  # noqa: F401
        except ImportError:
            self._load_error = "未安装 funasr（pip install funasr modelscope torch）"
            return False
        if self._load_error:
            return False
        # 模型按需加载，这里只检查依赖
        return True

    def _resolve_model_ref(self) -> str:
        """模型引用：本地路径优先（相对路径按项目根解析），否则交给 funasr 下载。"""
        from pathlib import Path

        from pipi.infra.config import PROJECT_ROOT

        p = Path(self.model_id)
        if p.is_absolute() and p.exists():
            return str(p)
        local = PROJECT_ROOT / self.model_id
        if not p.is_absolute() and local.exists():
            return str(local)
        return self.model_id

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        from funasr import AutoModel

        model_ref = self._resolve_model_ref()
        logger.info("正在加载 SenseVoice 模型 %s …", model_ref)
        self._model = AutoModel(
            model=model_ref,
            device=self._device,
            disable_update=True,
            trust_remote_code=False,
        )
        logger.info("SenseVoice 加载完成（device=%s）", self._device)

    # ------------------------------------------------------------------

    async def transcribe(self, audio: bytes, sample_rate: int = 16000) -> Transcript:
        return await self.transcribe_file_bytes(audio, sample_rate)

    async def transcribe_file(self, path: str) -> Transcript:
        import asyncio
        return await asyncio.to_thread(self._run_file, path)

    async def transcribe_file_bytes(self, audio: bytes, sample_rate: int) -> Transcript:
        """从内存字节识别。需要临时落盘 —— funasr 接受文件路径。"""
        import asyncio
        import tempfile
        import wave

        def _work() -> Transcript:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                path = f.name
            try:
                with wave.open(path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wf.writeframes(audio)
                return self._run_file(path)
            finally:
                import os
                try:
                    os.unlink(path)
                except OSError:
                    pass

        return await asyncio.to_thread(_work)

    def _run_file(self, path: str) -> Transcript:
        self._ensure_model()
        try:
            result = self._model.generate(
                input=path,
                cache={},
                language=self.language,
                use_itn=self.use_itn,
                batch_size_s=60,
                merge_vad=True,
                merge_length_s=15,
            )
        except Exception as exc:
            logger.error("SenseVoice 推理失败：%s", exc)
            return Transcript(text="", is_final=True, raw={"error": str(exc)})

        raw_text = result[0].get("text", "") if result else ""
        return self._parse(raw_text, path)

    def _parse(self, raw_text: str, source: str) -> Transcript:
        """解析 SenseVoice 的富文本输出。

        输出形如：<|zh|><|SAD|><|Speech|><|woitn|>其实我今天挺累的
        """
        emotion = "NEUTRAL"
        events: list[AudioEvent] = []
        language = "zh"

        text = raw_text
        # 提取所有 <|...|> 标签
        import re
        for tag in re.findall(r"<\|([^|]+)\|>", raw_text):
            full = f"<|{tag}|>"
            if full in _EMOTION_MAP:
                emotion = _EMOTION_MAP[full]
            elif full in _EVENT_MAP:
                events.append(AudioEvent(_EVENT_MAP[full], confidence=0.9))
            elif tag in ("zh", "en", "ja", "ko", "yue"):
                language = tag
        # 去掉所有标签得到纯文本
        text = re.sub(r"<\|[^|]+\|>", "", raw_text).strip()
        # 去掉可能残留的特殊标记
        text = text.replace("▁", " ").strip()

        if not events:
            events.append(AudioEvent(AudioEventType.SPEECH, confidence=1.0))

        duration_ms = 0
        return Transcript(
            text=text, is_final=True, emotion=emotion,
            emotion_confidence=0.8 if emotion != "NEUTRAL" else 0.0,
            events=events, language=language,
            duration_ms=duration_ms,
            raw={"raw_text": raw_text, "source": source, "device": self._device},
        )
