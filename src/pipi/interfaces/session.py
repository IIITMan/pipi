"""交互状态机 —— 待机(KWS) → 聆听(VAD+ASR) → 应答(流式TTS+Barge-in) → 超时回待机。

四阶段设计（真人对话感的来源）：

  阶段1 待机（休眠）
    麦克风 20~32ms 一帧持续采集，只跑轻量 KWS（能量门控 + 有声突发才动用
    识别，或接入 sherpa-onnx 等专用 KWS）。SenseVoice/LLM/TTS 全部不跑。

  阶段2 会话聆听
    唤醒后进入连续对话：Silero VAD 做人声检测 + 端点检测（600~800ms 静音
    判句），切出整段音频送 SenseVoice 识别，文本交给 on_query 回调。

  阶段3 机器人应答
    LLM 流式输出 → 分句 TTS 边生成边播放；期间 VAD 不停，
    检测到持续人声（默认 >200ms）→ Barge-in：立即停播、清缓冲、
    取消 LLM 生成，切回聆听，用户直接接着说。

  阶段4 会话超时
    会话内安静超过 timeout_s（默认 10s）→ 回到待机，需再次唤醒。

隐私：所有音频只在内存流转，不落盘。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

logger = logging.getLogger(__name__)


def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float64) ** 2))) if block.size else 0.0


FRAME_MS = 0.032        # 512 样本 @16k = 32ms/帧


def match_wake(text: str, wake_words: tuple[str, ...]) -> tuple[bool, str]:
    """判断文本是否含唤醒词；返回 (是否命中, 唤醒词后的正文)。"""
    low = text.lower()
    for w in wake_words:
        idx = low.find(w)
        if idx < 0:
            continue
        rest = text[idx + len(w):]
        rest = re.sub(r"^[\s，。,．.！!？?、~～]*", "", rest).strip()
        if len(rest) >= 2:
            return True, rest
        return True, ""
    return False, ""


# ---------------------------------------------------------------------------
# VAD：人声检测 + 端点检测
# ---------------------------------------------------------------------------

class SileroVAD:
    """Silero VAD（torch，约 2MB 模型）—— 质量最好的人声/端点检测。"""

    name = "silero"

    def __init__(self, threshold: float = 0.5, min_silence_ms: int = 600,
                 speech_pad_ms: int = 30):
        from silero_vad import VADIterator, load_silero_vad

        self._it = VADIterator(
            load_silero_vad(), sampling_rate=16000,
            threshold=threshold, min_silence_duration_ms=min_silence_ms,
            speech_pad_ms=speech_pad_ms,
        )

    def reset(self) -> None:
        self._it.reset_states()

    def process(self, frame: np.ndarray) -> str | None:
        """输入 16k 单声道 int16 帧（512 样本 = 32ms）。返回 'start'/'end'/None。"""
        frame = np.asarray(frame).reshape(-1)   # (512,1) → (512,)，silero 只认一维
        if len(frame) < 512:
            return None  # silero 要求 ≥512 样本（上游已保证固定帧，这里兜底）
        import torch

        t = torch.from_numpy(frame.astype(np.float32) / 32768.0)
        d = self._it(t, 16000)
        if not d:
            return None
        if "start" in d:
            return "start"
        if "end" in d:
            return "end"
        return None


class EnergyVAD:
    """能量 VAD —— silero-vad 不可用时的兜底（RMS 阈值 + 静音尾判定）。"""

    name = "energy"

    def __init__(self, threshold: float, min_silence_ms: int = 600,
                 speech_pad_ms: int = 30):
        self._thr = threshold
        self._min_silence_frames = max(min_silence_ms // 32, 1)
        self._silence_run = 0
        self._in_speech = False

    def reset(self) -> None:
        self._silence_run = 0
        self._in_speech = False

    def process(self, frame: np.ndarray) -> str | None:
        loud = _rms(frame) >= self._thr
        if not self._in_speech:
            if loud:
                self._in_speech = True
                self._silence_run = 0
                return "start"
            return None
        if loud:
            self._silence_run = 0
        else:
            self._silence_run += 1
            if self._silence_run >= self._min_silence_frames:
                self._in_speech = False
                return "end"
        return None


def make_vad(capture: Any, cfg: Any) -> SileroVAD | EnergyVAD:
    provider = cfg.get("audio.mic.vad.provider", "auto")
    threshold = cfg.get("audio.mic.vad.threshold", 0.5)
    min_silence = cfg.get("audio.mic.vad.min_silence_ms", 600)
    if provider in ("auto", "silero"):
        try:
            vad = SileroVAD(threshold=threshold, min_silence_ms=min_silence)
            logger.info("VAD: silero")
            return vad
        except Exception as exc:
            logger.info("silero-vad 不可用（%s），退回能量 VAD", exc)
    thr = capture.threshold if hasattr(capture, "threshold") else 300.0
    return EnergyVAD(threshold=thr, min_silence_ms=min_silence)


# ---------------------------------------------------------------------------
# KWS：待机唤醒词检测
# ---------------------------------------------------------------------------

class SenseVoiceKWS:
    """能量门控 + 按需识别的 KWS。

    待机态逐帧只做能量判断（几乎零开销）；检测到有声突发并结束时，
    才把这一小段送 SenseVoice 验证是否为唤醒词。
    好处：安静房间里 SenseVoice 几乎不会被调用，待机功耗趋近于零；
    相比纯能量门控又不会把任何响声误判成唤醒。
    """

    name = "sensevoice"
    FRAME_MS = 0.032

    def __init__(self, asr: Any, capture: Any, wake_words: tuple[str, ...],
                 min_speech_s: float = 0.3, max_seg_s: float = 8.0,
                 silence_frames: int = 12, pre_roll_s: float = 0.4,
                 on_debug: Callable[[str], None] | None = None):
        self.asr = asr
        self.capture = capture
        self.wake_words = tuple(w.lower() for w in wake_words)
        self.min_speech_s = min_speech_s
        self.max_seg_s = max_seg_s
        self.silence_frames = silence_frames   # 12 帧 ≈ 0.38s 静音判定段结束
        self.on_debug = on_debug
        self.last_asr_ms = 0
        self._pre: deque = deque(maxlen=max(int(pre_roll_s / self.FRAME_MS), 1))
        self._pre_saved: list[np.ndarray] = []
        self._seg: list[np.ndarray] = []
        self._silence = 0
        self._active = False

    def reset(self) -> None:
        self._seg.clear()
        self._silence = 0
        self._active = False

    def _debug(self, msg: str) -> None:
        if self.on_debug is not None:
            try:
                self.on_debug(msg)
            except Exception:
                pass

    def _amplify(self, audio: np.ndarray) -> np.ndarray:
        gain = getattr(self.capture, "gain", 1.0)
        if gain != 1.0:
            audio = np.clip(audio.astype(np.float32) * gain,
                            -32768, 32767).astype(np.int16)
        return audio

    async def process(self, frame: np.ndarray) -> tuple[bool, str] | None:
        """逐帧推进。命中唤醒返回 (True, 唤醒词后正文)，否则 None。"""
        loud = _rms(frame) >= self.capture.threshold
        if not self._active:
            if loud:
                self._active = True
                self._pre_saved = list(self._pre)   # 起始前预录，防丢字
                self._seg = [frame]
                self._silence = 0
            else:
                self._pre.append(frame)
            return None

        self._seg.append(frame)
        if loud:
            self._silence = 0
        else:
            self._silence += 1

        dur = len(self._seg) * self.FRAME_MS
        if self._silence < self.silence_frames and dur < self.max_seg_s:
            return None

        # 有声突发结束：整段送识别验证
        audio = np.concatenate(self._pre_saved + self._seg).flatten()
        speech_s = dur - self._silence * self.FRAME_MS
        self.reset()
        if speech_s < self.min_speech_s:
            return None

        import time as _t
        _t0 = _t.perf_counter()
        try:
            t = await self.asr.transcribe(
                self._amplify(audio).tobytes(), self.capture.sample_rate)
        except Exception as exc:
            logger.warning("KWS 识别失败：%s", exc)
            return None
        self.last_asr_ms = int((_t.perf_counter() - _t0) * 1000)
        text = (t.text or "").strip()
        if not text:
            self._debug(f"  （KWS 段识别为空，speech={speech_s:.1f}s）")
            return None
        hit, rest = match_wake(text, self.wake_words)
        self._debug(f"  （KWS 段：{text!r} → 命中={hit} 正文={rest!r}）")
        if hit:
            return True, rest
        return None


class SherpaKWS:
    """sherpa-onnx KeywordSpotter —— 专用流式 KWS（k2-fsa / WeNet 系）。

    zipformer 3.3M 参数中文模型，逐帧流式扫描声学特征，纯 CPU 毫瓦级；
    唤醒词经 keywords.txt 以拼音 token 定义（如 `p í p í @皮皮`），
    无需 SenseVoice 参与，待机功耗与延迟都远优于"能量门控+按需识别"。
    命中返回输出标签（@皮皮），唤醒词后的正文由聆听态 VAD 切句单独识别。
    """

    name = "sherpa"

    def __init__(self, model_dir: str, keywords_score: float = 1.0,
                 keywords_threshold: float = 0.25, min_speech_s: float = 0.3):
        import sherpa_onnx

        self.min_speech_s = min_speech_s
        d = Path(model_dir)
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(d / "tokens.txt"),
            encoder=str(d / "encoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx"),
            decoder=str(d / "decoder-epoch-99-avg-1-chunk-16-left-64.onnx"),
            joiner=str(d / "joiner-epoch-99-avg-1-chunk-16-left-64.onnx"),
            keywords_file=str(d / "keywords.txt"),
            num_threads=1,
            provider="cpu",
            keywords_score=keywords_score,
            keywords_threshold=keywords_threshold,
        )
        self._stream = self.kws.create_stream()

    def reset(self) -> None:
        try:
            self.kws.reset_stream(self._stream)
        except Exception:
            pass

    async def process(self, frame: np.ndarray) -> tuple[bool, str] | None:
        """逐帧推进流式 KWS。命中返回 (True, '')。"""
        samples = np.asarray(frame).reshape(-1).astype(np.float32) / 32768.0
        self._stream.accept_waveform(16000, samples)
        while self.kws.is_ready(self._stream):
            self.kws.decode_stream(self._stream)
            result = self.kws.get_result(self._stream)
            if result:
                self.kws.reset_stream(self._stream)
                return True, ""
        return None


def make_kws(asr: Any, capture: Any, cfg: Any,
             wake_words: tuple[str, ...],
             on_debug: Callable[[str], None] | None = None):
    """KWS 工厂：auto 时优先 sherpa-onnx 专用 KWS（模型在则用），
    否则退回"能量门控 + SenseVoice 验证"方案。
    """
    from pipi.infra.config import PROJECT_ROOT

    provider = cfg.get("audio.mic.kws.provider", "auto")
    model_dir = cfg.get("audio.mic.kws.model_dir", "data/models/kws")
    if not Path(model_dir).is_absolute():
        model_dir = PROJECT_ROOT / model_dir

    if provider in ("auto", "sherpa"):
        try:
            import sherpa_onnx  # noqa: F401
            if Path(model_dir, "tokens.txt").exists():
                kws = SherpaKWS(
                    str(model_dir),
                    keywords_score=cfg.get("audio.mic.kws.keywords_score", 1.0),
                    keywords_threshold=cfg.get("audio.mic.kws.keywords_threshold", 0.25),
                    min_speech_s=cfg.get("audio.mic.kws.min_speech_s", 0.3),
                )
                logger.info("KWS: sherpa-onnx（%s）", model_dir)
                return kws
        except Exception as exc:
            logger.info("sherpa KWS 不可用（%s），退回 SenseVoice 方案", exc)

    return SenseVoiceKWS(
        asr, capture, wake_words,
        min_speech_s=cfg.get("audio.mic.kws.min_speech_s", 0.3),
        silence_frames=cfg.get("audio.mic.kws.silence_frames", 12),
        on_debug=on_debug,
    )


# ---------------------------------------------------------------------------
# 交互状态机
# ---------------------------------------------------------------------------

class InteractionSession:
    """四阶段交互状态机。"""

    IDLE = "idle"          # 待机：只跑 KWS
    LISTEN = "listen"      # 聆听：VAD 切句 → ASR
    RESPOND = "respond"    # 应答：流式 TTS + Barge-in 监听

    FRAME_MS = 0.032       # 512 样本 @16k

    def __init__(self, asr: Any, capture: Any, *, tts: Any | None = None,
                 on_query: Callable[[str], Awaitable[None]] | None = None,
                 on_interrupt: Callable[[], Any] | None = None,
                 wake_ack: str = "嗯，我在。",
                 cfg: Any | None = None, verbose: bool = False,
                 life: Any | None = None):
        self.asr = asr
        self.capture = capture
        self.tts = tts
        self.life = life
        self.on_query = on_query
        self.on_interrupt = on_interrupt
        self.wake_ack = wake_ack
        self.verbose = verbose

        self.wake_words = tuple(cfg.get("audio.mic.wake_words", ["皮皮"]))
        self.timeout_s = cfg.get("audio.mic.session.timeout_s", 10)
        self.barge_in = cfg.get("audio.mic.session.barge_in", True)
        self.barge_min_ms = cfg.get("audio.mic.session.barge_in_min_ms", 200)
        self.barge_arm_s = cfg.get("audio.mic.session.barge_in_arm_s", 0.6)
        self.barge_mult = cfg.get("audio.mic.session.barge_in_multiplier", 1.6)

        # 唤醒应答预热：后台提前合成并缓存，首次唤醒即零延迟
        prewarm = getattr(self.tts, "prewarm", None)
        if prewarm is not None:
            asyncio.create_task(self._safe_prewarm(prewarm))

        self.vad = make_vad(capture, cfg)
        self.kws = make_kws(
            asr, capture, cfg, self.wake_words,
            on_debug=(lambda m: print(m, flush=True)) if verbose else None,
        )

        self.state = self.IDLE
        self.last_asr_ms = 0
        self.last_utt_s = 0.0
        self._aq: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._last_voice = 0.0
        self._seg: list[np.ndarray] = []
        self._seg_active = False
        self._resp_task: asyncio.Task | None = None
        self._resp_started = 0.0
        self._loud_frames = 0
        self._ignore_until = 0.0
        self._wake_hit = False
        self._exit_pending = False             # 退出意图：告别播完即回待机
        self._pending: list[np.ndarray] = []   # 应答期间缓冲的音频（打断时保话头）
        self._seg_start = 0
        # 预录缓冲：VAD 触发前先攒着最近 N 帧，段开始时补进去，防首字被截
        pre_frames = max(int(float(cfg.get("audio.mic.pre_roll_s", 0.32)) / 0.032), 1)
        self._pre_frames: deque = deque(maxlen=pre_frames)

        # 语音退出短语（§3 idle/sleep 切换的语音出口）
        self.exit_phrases = tuple(cfg.get("audio.mic.exit_phrases",
                                          ["退下", "别说话了", "闭嘴", "别出声",
                                           "安静会儿", "安静一会", "走开",
                                           "别烦我", "不用你了", "你可以走了",
                                           "歇着吧"]))
        self.exit_farewells = tuple(cfg.get("audio.mic.exit_farewells",
                                            ["哼，那我走啦，想我了就喊我。",
                                             "好吧……那我去那边待着，不打扰你。",
                                             "嗯，那我不出声了，需要我的时候喊我哦。",
                                             "行吧行吧，反正你肯定会想我的。"]))

    # ------------------------------------------------------------------

    def _notify(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    @property
    def is_busy(self) -> bool:
        """会话是否忙（对话/机器人说话/正在收集用户语音）。"""
        return self.state != self.IDLE or self._seg_active

    # ------------------------------------------------------------------
    # 生命模块接入：自发念头发声（走现有 TTS + Barge-in 通道）
    # ------------------------------------------------------------------

    def try_speak_spontaneous(self, text: str) -> bool:
        """空闲时让皮皮说一句自发念头。返回 False 表示此刻不方便。"""
        if self.state != self.IDLE or self._seg_active:
            return False
        self.state = self.RESPOND
        self._resp_started = time.monotonic()
        self._loud_frames = 0
        self._pending = []
        self._resp_task = asyncio.create_task(self._speak_spontaneous(text))
        return True

    # -- 语音退出 ------------------------------------------------------

    def _is_exit_intent(self, text: str) -> bool:
        """判断是否退出意图（短语子串匹配，限短句防误触）。"""
        if not text or len(text) > 15:
            return False
        hit = any(p in text for p in self.exit_phrases)
        if self.verbose:
            print(f"  [exit-check] {text!r} → {hit}")
        return hit

    def _start_farewell(self) -> None:
        """播一句告别 → 播完自动回待机（期间可被 Barge-in 打断）。"""
        if self.verbose:
            print("  [exit] start_farewell")
        self.state = self.RESPOND
        self._resp_started = time.monotonic()
        self._loud_frames = 0
        self._exit_pending = True
        self._pending = []
        self._resp_task = asyncio.create_task(
            self._speak_line(random.choice(self.exit_farewells)))

    def _go_idle(self, now: float) -> None:
        """回待机：只留 KWS，等下一次唤醒。"""
        self.state = self.IDLE
        self._seg = []
        self._seg_active = False
        self._wake_hit = False
        self._exit_pending = False
        self._pending = []
        self._seg_start = 0
        self.vad.reset()
        self.kws.reset()
        self._pre_frames.clear()
        self._ignore_until = now + 0.4
        if self.life is not None:
            self.life.on_farewell()   # 告别计入念叨冷却，防止退出后立刻自言自语
        self._notify("  💤 已回待机（再喊唤醒词继续）")

    async def _speak_spontaneous(self, text: str) -> None:
        await self._speak_line(text, prefix="皮皮（自言自语）> ")

    async def _speak_line(self, text: str, prefix: str = "皮皮 > ") -> None:
        self._notify(f"{prefix}{text}")
        if self.tts is not None:
            from pipi.interfaces.voice import SentenceTTSPlayer

            speaker = SentenceTTSPlayer(self.tts)
            self._speaker = speaker
            try:
                await speaker.feed(text)
                await speaker.finish()
            finally:
                self._speaker = None
        else:
            print(f"{prefix}{text}", flush=True)

    async def run(self) -> None:
        """主循环：麦克风帧泵 + 状态机。"""
        import sounddevice as sd

        self._loop = asyncio.get_running_loop()
        self._aq = asyncio.Queue(maxsize=400)
        self._running = True
        acc = np.empty((0, 1), dtype=np.int16)   # 帧累积器：凑满固定 512 样本

        FRAME = 512

        def _mic_cb(indata, frames, _t, _status) -> None:
            nonlocal acc
            if self._aq is None:
                return
            # Windows 声卡回调帧长不固定（WASAPI 常给 480 等），
            # silero-vad 要求 ≥512 样本/帧，这里累积成固定帧再入队
            acc = np.concatenate((acc, indata))
            while len(acc) >= FRAME:
                out = acc[:FRAME, 0].copy()   # (512,1) → 一维 (512,)
                acc = acc[FRAME:]
                try:
                    self._loop.call_soon_threadsafe(self._aq.put_nowait, out)
                except (asyncio.QueueFull, RuntimeError):
                    pass  # 忙时丢帧，宁缺毋滥

        self._notify(f"  💤 待机中…（喊「{'/'.join(self.wake_words)}」唤醒）")
        with sd.InputStream(samplerate=self.capture.sample_rate, channels=1,
                            dtype="int16", blocksize=512, callback=_mic_cb):
            while self._running:
                frame = await self._aq.get()
                await self._step(frame)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------

    async def _step(self, frame: np.ndarray) -> None:
        now = time.monotonic()
        if now < self._ignore_until:
            return  # 回声保护窗

        if self.state == self.IDLE:
            await self._step_idle(frame, now)
            return

        if self.state == self.LISTEN:
            await self._step_listen(frame, now)
            return

        if self.state == self.RESPOND:
            self._step_respond(frame, now)

    # -- 阶段 1：待机（VAD 攒段 + KWS 扫描，命中才送 ASR）--------------

    async def _step_idle(self, frame: np.ndarray, now: float) -> None:
        """待机双引擎协同：

        - KWS（3.3M，逐帧流式）：判断这段声音里有没有唤醒词
        - VAD（切句）：攒"从开口到停顿"的整段音频
        KWS 命中 → 本段结束时整段送 SenseVoice（支持「皮皮，今天天气」
        一步到位）；KWS 未命中 → 段结束直接丢弃（ASR 零调用）。
        """
        hit = await self.kws.process(frame)
        if hit and not self._wake_hit:
            self._wake_hit = True
            self._notify("  🔑 检测到唤醒词…（说完需求或停顿即可）")

        ev = self.vad.process(frame)
        if ev == "start" and not self._seg_active:
            self._seg = list(self._pre_frames) + [frame]   # 预录补头，防首字丢失
            self._pre_frames.clear()
            self._seg_active = True
        elif self._seg_active:
            self._seg.append(frame)
            if ev == "end":
                seg = self._seg
                self._seg = []
                self._seg_active = False
                await self._finish_idle_segment(seg)
        else:
            self._pre_frames.append(frame)

    async def _finish_idle_segment(self, seg: list[np.ndarray]) -> None:
        """待机段结束：KWS 命中过才识别，否则丢弃。"""
        if not self._wake_hit:
            self._notify("  （有声但无唤醒词，丢弃）")
            return
        self._wake_hit = False

        audio = np.concatenate(seg).flatten()
        gain = getattr(self.capture, "gain", 1.0)
        if gain != 1.0:
            audio = np.clip(audio.astype(np.float32) * gain,
                            -32768, 32767).astype(np.int16)
        dur = len(audio) / self.capture.sample_rate
        if dur < getattr(self.kws, "min_speech_s", 0.3):
            return

        import time as _t
        _t0 = _t.perf_counter()
        try:
            t = await self.asr.transcribe(audio.tobytes(), self.capture.sample_rate)
        except Exception as exc:
            self._notify(f"  ✗ 唤醒段识别失败：{exc}")
            return
        self.last_asr_ms = int((_t.perf_counter() - _t0) * 1000)
        text = (t.text or "").strip()
        self._notify(f"  🎤 {text or '(空)'}")

        # 唤醒段里的退出意图（「皮皮，退下吧」）
        if self._is_exit_intent(text):
            self._notify("  👋 退出对话，回待机")
            self._start_farewell()
            return

        # KWS 已在声学层命中，ASR 文本只用来剥离唤醒词拿正文
        _hit, rest = match_wake(text, self.wake_words)
        if rest:
            self._notify("  👂 已唤醒（带需求）")
            self.state = self.RESPOND
            self._resp_started = time.monotonic()
            self._loud_frames = 0
            self._resp_task = asyncio.create_task(self._safe_query(rest))
            return

        # 纯唤醒（「皮皮」或文本剥离失败）→ 应答后进聆听
        self._notify("  👂 已唤醒！")
        await self._play_ack()
        self._enter_listen(time.monotonic())

    # -- 阶段 2 --------------------------------------------------------

    async def _step_listen(self, frame: np.ndarray, now: float) -> None:
        level = _rms(frame)
        if level >= self.capture.threshold:
            self._last_voice = now
            if self.life is not None:
                self.life.observe_user_voice()

        # 会话超时：安静太久回待机
        if now - self._last_voice > self.timeout_s and not self._seg_active:
            self._notify(f"  💤 安静 {self.timeout_s}s，回待机（再喊唤醒词继续）")
            self.vad.reset()
            self.kws.reset()
            self._wake_hit = False
            self.state = self.IDLE
            return

        ev = self.vad.process(frame)
        if ev == "start" and not self._seg_active:
            self._seg = list(self._pre_frames) + [frame]   # 预录补头，防首字丢失
            self._pre_frames.clear()
            self._seg_active = True
        elif self._seg_active:
            self._seg.append(frame)
            if ev == "end":
                seg = self._seg
                self._seg = []
                self._seg_active = False
                await self._on_segment(seg)
        else:
            self._pre_frames.append(frame)

    async def _on_segment(self, seg: list[np.ndarray]) -> None:
        """一句话说完 → 识别 → 进入应答。"""
        audio = np.concatenate(seg).flatten()
        gain = getattr(self.capture, "gain", 1.0)
        if gain != 1.0:   # 与 KWS 一致：识别前加软件增益
            audio = np.clip(audio.astype(np.float32) * gain,
                            -32768, 32767).astype(np.int16)
        self.last_utt_s = len(audio) / self.capture.sample_rate
        if self.last_utt_s < 0.4:
            return
        import time as _t

        _t0 = _t.perf_counter()
        try:
            t = await self.asr.transcribe(audio.tobytes(), self.capture.sample_rate)
        except Exception as exc:
            self._notify(f"  ✗ 识别失败：{exc}")
            return
        self.last_asr_ms = int((_t.perf_counter() - _t0) * 1000)
        text = (t.text or "").strip()
        if not text:
            return
        # 噪声过滤：没有实际文字（纯标点/单字符误识别）不进对话
        if not any(ch.isalnum() for ch in text) or len(text) < 2:
            self._notify(f"  （噪声丢弃：{text!r}）")
            return
        self._notify(f"  🎤 {text}")

        # 退出意图：告别一句 → 回待机
        if self._is_exit_intent(text):
            self._notify("  👋 退出对话，回待机")
            self._start_farewell()
            return

        self.state = self.RESPOND
        self._resp_started = time.monotonic()
        self._loud_frames = 0
        self._resp_task = asyncio.create_task(self._safe_query(text))

    async def _safe_query(self, text: str) -> None:
        if self.on_query is None:
            return
        try:
            await self.on_query(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("应答失败：%s", exc)

    # -- 阶段 3 --------------------------------------------------------

    def _step_respond(self, frame: np.ndarray, now: float) -> None:
        task = self._resp_task

        # 应答期间持续缓冲音频：打断时保留用户已说的话头
        self._pending.append(frame)
        if len(self._pending) > 220:            # 只留最近 ~7s
            drop = len(self._pending) - 220
            self._pending = self._pending[drop:]
            self._seg_start = max(0, self._seg_start - drop)

        # 应答自然结束 → 回聆听（重置计时，防立刻超时）
        if task is None or task.done():
            if self._exit_pending:
                self._go_idle(now)
                return
            if self.life is not None:
                self.life.on_response_done()
            self._back_to_listen(now)
            return

        if not self.barge_in:
            return
        # 开播头 arm_s 秒不响应打断（防扬声器回声自触）
        if now - self._resp_started < self.barge_arm_s:
            return
        level = _rms(frame)
        if level >= self.capture.threshold * self.barge_mult:
            if self._loud_frames == 0:
                # 用户声音起点：从这里截取缓冲作为打断后的句子开头
                self._seg_start = max(0, len(self._pending) - 1)
            self._loud_frames += 1
            if self.verbose and self._loud_frames % 3 == 0:
                self._notify(f"  ✋ 打断累计 {self._loud_frames * self.FRAME_MS * 1000:.0f}ms"
                             f"（RMS {level:.0f} / 需 {self.capture.threshold * self.barge_mult:.0f}）")
        else:
            self._loud_frames = 0
        if self._loud_frames * (self.FRAME_MS * 1000) >= self.barge_min_ms:
            self._do_barge_in(now)

    def _do_barge_in(self, now: float) -> None:
        self._notify("  ✋ 打断！停播、听你说…")
        if self.life is not None:
            self.life.on_barge_in()
        if self._resp_task is not None:
            self._resp_task.cancel()
            self._resp_task = None
        if self.on_interrupt is not None:
            try:
                r = self.on_interrupt()
                if asyncio.iscoroutine(r):
                    asyncio.ensure_future(r)
            except Exception:
                pass
        # 关键：不打断用户的话头 —— 以用户声音起点为段首继续收集，
        # 不清 VAD 状态（它正在"说话中"，停顿后会自然触发 'end'）
        self.state = self.LISTEN
        self._seg = self._pending[max(0, self._seg_start - 2):]
        self._pending = []
        self._seg_start = 0
        self._seg_active = True
        self._last_voice = now
        self._exit_pending = False   # 用户插话=改主意了，取消退出
        self._pre_frames.clear()
        self._ignore_until = 0.0     # 立即监听，不再丢 0.4s

    def _back_to_listen(self, now: float) -> None:
        self.state = self.LISTEN
        self._last_voice = now
        self._seg = []
        self._pending = []
        self._seg_start = 0
        self._seg_active = False
        self._wake_hit = False
        self._exit_pending = False
        self._pre_frames.clear()
        self.vad.reset()
        # 播放尾音回声保护
        self._ignore_until = now + 0.4

    async def _safe_prewarm(self, prewarm) -> None:
        try:
            await prewarm(self.wake_ack)
        except Exception as exc:
            logger.warning("唤醒应答预热失败（首次唤醒改为实时合成）：%s", exc)

    async def _play_ack(self) -> None:
        if self.tts is None or not self.wake_ack:
            return
        try:
            await self.tts.say(self.wake_ack)
            self._ignore_until = time.monotonic() + 0.5  # 回声保护
        except Exception as exc:
            logger.warning("唤醒应答播报失败：%s", exc)

    def _enter_listen(self, now: float) -> None:
        self.state = self.LISTEN
        self._last_voice = now
        self._seg = []
        self._seg_active = False
        self._pre_frames.clear()
        self.vad.reset()
