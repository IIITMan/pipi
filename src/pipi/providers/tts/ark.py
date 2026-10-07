"""火山引擎 TTS —— 豆包语音合成大模型 2.0（seed-tts-2.0）。

接入方式：单向流式语音合成（HTTP Chunked）：
  POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
  鉴权：X-Api-Key（控制台 > API Key 管理）
       X-Api-Resource-Id: seed-tts-2.0（豆包语音合成大模型 2.0）
  响应：Chunked JSON 块流，data 字段为 base64 音频分片（按序拼接）

优点：真·流式合成（服务端边生成边吐分片）；2.0 支持语音指令
（context_texts，如「用委屈的语气说」）与文本情感自动演绎。

接口协议见 docs.volcengine.com「单向流式语音合成(HTTP)」。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from pipi.providers.tts.base import AudioChunk, TTSRequest

logger = logging.getLogger(__name__)

# 情绪 → 2.0 语音指令（context_texts 不计费；2.0 音色支持）
_EMOTION_INSTRUCTION = {
    "happy": "请用开心愉快的语气说话",
    "sad": "请用伤心委屈的语气说话",
    "angry": "请用生气一点的语气说话",
    "gentle": "请用温柔一点的语气说话",
}


class ArkTTS:
    """豆包语音合成大模型 2.0（HTTP Chunked 流式）。"""

    name = "ark_tts"

    ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"

    def __init__(self, api_key: str, resource_id: str = "seed-tts-2.0",
                 speaker: str = "zh_female_vv_uranus_bigtts",
                 model: str = "", sample_rate: int = 24000,
                 bitrate: int = 160000, emotion_instruction: bool = True,
                 timeout_s: float = 30.0, endpoint: str = "",
                 cache_enabled: bool = True, cache_dir: str = "data/tts_cache",
                 cache_max_chars: int = 120, cache_max_files: int = 300):
        self.api_key = api_key
        self.resource_id = resource_id
        self.speaker = speaker
        self.model = model
        self.sample_rate = sample_rate
        self.bitrate = bitrate
        self.emotion_instruction = emotion_instruction
        self.timeout_s = timeout_s
        self.endpoint = endpoint or self.ENDPOINT
        self.cache_enabled = cache_enabled
        self.cache_dir = Path(cache_dir)
        self.cache_max_chars = cache_max_chars      # 超长文本不缓存（命中低）
        self.cache_max_files = cache_max_files      # LRU：超量清最旧的
        self._stopped = False

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self.api_key)

    async def stop(self) -> None:
        self._stopped = True

    # ------------------------------------------------------------------

    def _build_payload(self, req: TTSRequest) -> dict:
        audio_params: dict = {
            "format": "mp3",
            "sample_rate": self.sample_rate,
            "bit_rate": self.bitrate,
        }
        if req.speed != 1.0:
            # speed 1.0=原速；speech_rate [-50,100]，100=2.0 倍速
            audio_params["speech_rate"] = int(round((req.speed - 1.0) * 100))
        params: dict = {
            "text": req.text,
            "speaker": req.voice_id or self.speaker,
            "audio_params": audio_params,
        }
        if self.model:
            params["model"] = self.model
        # 2.0 语音指令：按情绪语气演绎（不计费）
        if self.emotion_instruction:
            ins = _EMOTION_INSTRUCTION.get(req.emotion)
            if ins:
                params["context_texts"] = [ins]
        return {"req_params": params}

    async def synthesize(self, req: TTSRequest) -> AsyncIterator[AudioChunk]:
        """流式合成（带通用缓存）。

        命中缓存 → 整段本地音频一次返回（零网络）；
        未命中 → 流式 yield 的同时累积，结束后落盘，下次直接复用。
        常用回答（问候/兜底话术/重复句式）第二次起零延迟零成本。
        """
        path = self._cache_path(req.text, req.emotion) \
            if self.cache_enabled and len(req.text) <= self.cache_max_chars else None
        if path is not None and path.exists():
            try:
                audio = path.read_bytes()
                if audio:
                    self._touch_cache(path)
                    yield AudioChunk(pcm=audio, sample_rate=self.sample_rate)
                    return
            except OSError:
                pass

        buf = bytearray()
        async for chunk in self._synthesize_remote(req):
            if chunk.pcm:
                buf.extend(chunk.pcm)
            yield chunk

        if path is not None and buf:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bytes(buf))
                self._prune_cache()
            except OSError:
                pass

    async def _synthesize_remote(self, req: TTSRequest) -> AsyncIterator[AudioChunk]:
        """实际调用远端流式合成：服务端每吐一个分片立即 yield（mp3 块）。"""
        import urllib.request

        self._stopped = False
        body = json.dumps(self._build_payload(req), ensure_ascii=False).encode("utf-8")
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": uuid.uuid4().hex,
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def _worker() -> None:
            """阻塞读 Chunked 流（独立线程），增量解析 JSON 块推给异步侧。

            响应是连续的 JSON 块流（可能黏包、可能无换行分隔），
            用 raw_decode 增量解析：解出一个消费一个，解不出等更多数据。
            """
            try:
                decoder = json.JSONDecoder()
                buf = ""

                def _drain() -> str:
                    """从 buf 里解出所有完整 JSON 块，返回剩余（不完整的）尾巴。"""
                    nonlocal buf
                    buf = buf.lstrip()
                    while buf:
                        try:
                            obj, end = decoder.raw_decode(buf)
                        except json.JSONDecodeError:
                            break   # 半截块，等更多数据
                        buf = buf[end:].lstrip()
                        loop.call_soon_threadsafe(q.put_nowait, ("json", obj))
                    return buf

                r_ = urllib.request.Request(self.endpoint, data=body, headers=headers,
                                            method="POST")
                with urllib.request.urlopen(r_, timeout=self.timeout_s) as resp:
                    while True:
                        raw = resp.read1(4096) if hasattr(resp, "read1") \
                            else resp.read(4096)
                        if not raw:
                            break
                        buf += raw.decode("utf-8", errors="ignore")
                        _drain()
                    _drain()   # 流结束时把剩余完整块解干净
                loop.call_soon_threadsafe(q.put_nowait, ("done", None))
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, ("error", exc))

        threading.Thread(target=_worker, daemon=True).start()

        got_audio = False
        error: Exception | None = None
        while True:
            kind, item = await q.get()
            if kind == "json":
                code = item.get("code", 0)
                message = str(item.get("message", ""))
                # 实测成功码为 20000000（message=OK），文档写 0 以实测为准
                if code != 0 and not message.upper().startswith("OK"):
                    raise RuntimeError(f"方舟 TTS 错误 {code}: {message}")
                data = item.get("data")
                if data:
                    got_audio = True
                    yield AudioChunk(pcm=base64.b64decode(data),
                                     sample_rate=self.sample_rate)
            elif kind == "error":
                error = item
                break
            else:   # done
                return
            if self._stopped:
                return

        # 没拿到任何音频才算失败（有音频后中途断流不回退）
        if not got_audio:
            logger.warning("方舟 TTS 流式失败：%s", error)
            raise RuntimeError(f"方舟 TTS 失败：{error}")

    def _cache_path(self, text: str, emotion: str) -> Path:
        """缓存文件：文本+音色+情绪 的哈希 → data/tts_cache/{key}.mp3。"""
        key = hashlib.sha1(
            f"{self.speaker}|{self.resource_id}|{self.model}|{emotion}|{text}"
            .encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{key}.mp3"

    def _touch_cache(self, path: Path) -> None:
        """命中时刷新 mtime（LRU 依据）。"""
        try:
            import os

            os.utime(path, None)
        except OSError:
            pass

    def _prune_cache(self) -> None:
        """缓存超量时按最久未命中清理（LRU）。"""
        try:
            files = sorted(self.cache_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
            for old in files[:-self.cache_max_files] if len(files) > self.cache_max_files else []:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    async def prewarm(self, text: str, emotion: str = "neutral") -> None:
        """后台预热：提前合成固定短语（唤醒应答）存入缓存，唤醒时零延迟。"""
        if not self.cache_enabled:
            return
        path = self._cache_path(text, emotion)
        if path.exists():
            return
        buf = bytearray()
        async for chunk in self.synthesize(TTSRequest(text=text, emotion=emotion)):
            if chunk.pcm:
                buf.extend(chunk.pcm)
        if buf:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(buf))
            logger.info("预热缓存完成：%s（%d bytes）", path.name, len(buf))

    async def say(self, text: str, emotion: str = "neutral") -> None:
        """合成（自动走缓存）并经 sounddevice 播放。"""
        import io

        buf = bytearray()
        async for chunk in self.synthesize(TTSRequest(text=text, emotion=emotion)):
            if chunk.pcm:
                buf.extend(chunk.pcm)
        if not buf:
            return
        try:
            import sounddevice as sd
            import soundfile as sf

            data, sr = sf.read(io.BytesIO(bytes(buf)), dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)
            sd.play(data, sr)
            await asyncio.to_thread(sd.wait)
        except Exception as exc:
            logger.warning("播放失败：%s", exc)
