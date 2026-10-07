"""麦克风采集 —— 真实语音输入的入口。

sounddevice（PortAudio）流式采集 + 能量 VAD：
  - 自动校准：静音阈值随环境噪声自适应（silence_rms: auto）
  - 预录缓冲：保留说话起始前 pre_roll_s 秒，防止唤醒词开头丢字
  - 软件增益：gain 放大安静麦克风，提高识别率
  - 说完（连续 silence_tail 秒低于阈值）自动结束，最长 max_seconds 兜底

依赖：pip install sounddevice
隐私：音频只进内存，识别后即丢弃，不落盘。
"""

from __future__ import annotations

import logging
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


def _rms(block: np.ndarray) -> float:
    """一块 PCM 的能量（16bit int16）。"""
    return float(np.sqrt(np.mean(block.astype(np.float64) ** 2))) if block.size else 0.0


class MicCapture:
    """默认输入设备的语音段采集器。"""

    name = "mic"

    def __init__(
        self,
        sample_rate: int = 16000,
        silence_rms: float | str = "auto",   # 数值=固定阈值；"auto"=随环境噪声
        noise_multiplier: float = 2.2,       # auto 模式：噪声底的倍数算"有人在说话"
        min_silence_rms: float = 120.0,      # auto 模式阈值下限（防超灵敏）
        gain: float = 1.0,                   # 采集增益（>1 放大安静麦克风）
        pre_roll_s: float = 0.6,             # 起始预录时长，防开头丢字
        silence_tail: float = 1.2,
        max_seconds: float = 15.0,
        min_seconds: float = 0.8,
        min_speech_s: float = 0.25,          # 短于此的"发声"视为噪声丢弃
    ):
        self.sample_rate = sample_rate
        self.silence_rms = silence_rms
        self.noise_multiplier = noise_multiplier
        self.min_silence_rms = min_silence_rms
        self.gain = gain
        self.pre_roll_s = pre_roll_s
        self.silence_tail = silence_tail
        self.max_seconds = max_seconds
        self.min_seconds = min_seconds
        self.min_speech_s = min_speech_s
        self.device_name: str = ""
        self.noise_floor: float = 0.0        # 校准得到的环境噪声 RMS
        self._calibrated = silence_rms != "auto"
        self._error: str | None = None

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """探测 sounddevice 与默认输入设备。"""
        try:
            import sounddevice as sd
        except ImportError:
            self._error = "未安装 sounddevice（pip install sounddevice）"
            return False
        try:
            dev = sd.query_devices(kind="input")
            if dev is None:
                self._error = "没有可用的输入设备"
                return False
            self.device_name = dev["name"]
            return True
        except Exception as exc:
            self._error = f"输入设备不可用：{exc}"
            return False

    @property
    def threshold(self) -> float:
        """当前判定"有人在说话"的能量阈值。"""
        if self._calibrated and isinstance(self.silence_rms, (int, float)):
            return float(self.silence_rms)
        return max(self.min_silence_rms, self.noise_floor * self.noise_multiplier)

    def calibrate(self, seconds: float = 0.8) -> float:
        """测环境噪声底，设置自适应阈值。返回噪声 RMS。"""
        import sounddevice as sd

        audio = sd.rec(int(self.sample_rate * seconds), samplerate=self.sample_rate,
                       channels=1, dtype="int16")
        sd.wait()
        block = self.sample_rate // 10
        levels = sorted(_rms(audio[i:i + block].flatten())
                        for i in range(0, len(audio) - block + 1, block))
        # 取中位数：抗偶发噪声
        self.noise_floor = levels[len(levels) // 2] if levels else 0.0
        self._calibrated = True
        logger.info("环境噪声校准：RMS=%.0f，说话阈值=%.0f",
                    self.noise_floor, self.threshold)
        return self.noise_floor

    # ------------------------------------------------------------------

    def record_until_silence(self, max_seconds: float | None = None,
                             calibrate: bool = True) -> np.ndarray:
        """录一段话（说完自动停）。返回 16bit 单声道 PCM（int16）。

        max_seconds 可临时覆盖兜底上限（待唤醒探测用短窗口）。
        首次调用且阈值是 auto 时会先自动校准环境噪声（约 0.8s）。
        """
        import sounddevice as sd

        if not self._calibrated and calibrate:
            self.calibrate()

        limit = max_seconds or self.max_seconds
        block = max(int(self.sample_rate * 0.1), 64)  # 100ms 一块
        pre_roll = deque(maxlen=max(int(self.pre_roll_s * 10), 1))
        frames: list[np.ndarray] = []
        state = {"duration": 0.0, "speech": 0.0, "silent": 0.0, "started": False}
        thr = self.threshold

        def _cb(indata, _frames, _time, _status) -> None:
            state["duration"] += len(indata) / self.sample_rate
            quiet = _rms(indata.flatten()) < thr
            if not quiet:
                if not state["started"]:
                    # 说话开始：补上起始前的缓冲，防止丢字
                    frames.extend(pre_roll)
                    state["started"] = True
                frames.append(indata.copy())
                state["speech"] += len(indata) / self.sample_rate
                state["silent"] = 0.0
            elif state["started"]:
                frames.append(indata.copy())
                state["silent"] += len(indata) / self.sample_rate
            else:
                pre_roll.append(indata.copy())

        with sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="int16",
            blocksize=block, callback=_cb,
        ):
            while state["duration"] < limit:
                if state["started"] and state["silent"] >= self.silence_tail \
                        and state["duration"] >= self.min_seconds:
                    break
                sd.sleep(30)

        if not frames or state["speech"] < self.min_speech_s:
            return np.array([], dtype="int16")
        audio = np.concatenate(frames).flatten()
        if self.gain != 1.0:
            audio = np.clip(audio.astype(np.float32) * self.gain,
                            -32768, 32767).astype(np.int16)
        logger.debug("录音 %.1fs speech=%.1fs RMS=%.0f thr=%.0f",
                     len(audio) / self.sample_rate, state["speech"],
                     _rms(audio), thr)
        return audio

    def level_of(self, audio: np.ndarray) -> float:
        return _rms(audio)


__all__ = ["MicCapture"]
