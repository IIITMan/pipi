"""CosyVoice TTS —— 阿里 FunAudioLLM 的语音合成。

特点：支持情感/音色控制、零样本音色克隆、指令跟随。
"用委屈的语气说"这种指令它能听懂 —— 这正是皮皮需要的。

依赖：pip install cosyvoice  或 modelscope + funasr
模型：iic/CosyVoice-300M（约 300MB~1GB）
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any, AsyncIterator

from pipi.providers.tts.base import AudioChunk, TTSRequest

logger = logging.getLogger(__name__)


class CosyVoiceTTS:
    """本地 CosyVoice 语音合成。"""

    name = "cosyvoice"

    def __init__(
        self,
        model: str = "iic/CosyVoice-300M",
        device: str = "auto",
        speaker: str = "中文女",
        sample_rate: int = 22050,
    ):
        self.model_id = model
        self.speaker = speaker
        self.sample_rate = sample_rate
        self._device = self._resolve_device(device)
        self._model: Any = None
        self._error: str | None = None

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
        """需要官方 CosyVoice 仓库结构（cosyvoice.cli.cosyvoice）。

        注意：PyPI 上的 cosyvoice 包只是 API 封装，不含模型推理代码；
        官方栈依赖 ttsfrd/pynini，仅支持 Linux（目标机器人 N100/RK3588 即 Linux）。
        Windows 上此 Provider 恒为不可用，由降级链落到 edge_tts。
        """
        try:
            from cosyvoice.cli.cosyvoice import CosyVoice  # noqa: F401
            return True
        except Exception:
            self._error = (
                "缺少官方 CosyVoice 仓库（cosyvoice.cli.cosyvoice）。"
                "官方栈仅支持 Linux：git clone https://github.com/FunAudioLLM/CosyVoice2-0.5B "
                "后 pip install -r requirements.txt"
            )
            return False

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            from cosyvoice.cli.cosyvoice import CosyVoice
            logger.info("加载 CosyVoice 模型 %s …", self.model_id)
            self._model = CosyVoice(self.model_id)
        except Exception as exc:
            logger.error("CosyVoice 加载失败：%s", exc)
            raise

    # ------------------------------------------------------------------

    async def synthesize(self, req: TTSRequest) -> AsyncIterator[AudioChunk]:
        """合成并逐块产出。

        CosyVoice 本身是生成器，我们包成异步流以便适配播放/打断。
        """
        try:
            chunks = await asyncio.to_thread(self._synth_sync, req)
        except Exception as exc:
            logger.error("CosyVoice 合成失败：%s", exc)
            yield AudioChunk(pcm=b"", sample_rate=self.sample_rate, is_last=True)
            return

        for i, pcm in enumerate(chunks):
            yield AudioChunk(
                pcm=pcm,
                sample_rate=self.sample_rate,
                is_last=(i == len(chunks) - 1),
            )

    def _synth_sync(self, req: TTSRequest) -> list[bytes]:
        self._ensure_model()
        out: list[bytes] = []
        try:
            speaker = req.voice_id or self.speaker
            # 带情感指令（CosyVoice 支持 instruct 模式）
            gen = self._model.inference_sft(req.text, speaker)
            for item in gen:
                speech = item.get("tts_speech")
                if speech is not None:
                    # torch tensor → bytes
                    try:
                        import torch
                        out.append(speech.numpy().tobytes())
                    except Exception:
                        out.append(bytes(speech))
        except Exception as exc:
            logger.error("CosyVoice 推理失败：%s", exc)
        return out

    async def stop(self) -> None:
        # CosyVoice 是同步生成器，打断通过丢弃后续块实现
        pass

    async def say(self, text: str, emotion: str = "neutral") -> None:
        """合成并播放（用系统播放器或 sounddevice）。"""
        req = TTSRequest(text=text, emotion=emotion)
        import wave

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            chunks = [c.pcm async for c in self.synthesize(req) if c.pcm]
            if not chunks:
                return
            pcm = b"".join(chunks)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(pcm)
            await asyncio.to_thread(_play_file, path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


def _play_file(path: str) -> None:
    """用可用的播放器播放 wav。"""
    import shutil
    import subprocess

    for player, args in (
        ("aplay", ["aplay", "-q", path]),
        ("afplay", ["afplay", path]),
        ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]),
    ):
        if shutil.which(player):
            try:
                subprocess.run(args, check=False, timeout=120)
                return
            except Exception:
                continue
    logger.debug("未找到可用的音频播放器，跳过播放")
