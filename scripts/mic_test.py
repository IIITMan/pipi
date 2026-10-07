#!/usr/bin/env python3
"""麦克风 → SenseVoice 语音识别测试。

用法：
    python scripts/mic_test.py            # 交互模式：按回车开始录音，说完自动结束
    python scripts/mic_test.py --auto     # 自动模式：录 5 秒即识别（用于管道/自动化）
    python scripts/mic_test.py --list     # 列出音频设备

依赖：pip install sounddevice funasr modelscope torch soundfile
模型：iic/SenseVoiceSmall（首次自动下载，已缓存则直接加载）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import numpy as np
import sounddevice as sd

# Windows 控制台默认 GBK，强制 UTF-8 避免输出崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pipi.providers.asr.sensevoice import SenseVoiceASR  # noqa: E402

SAMPLE_RATE = 16000
CHANNELS = 1
SILENCE_RMS = 300.0       # 静音能量阈值（16bit PCM）
MAX_SECONDS = 15
MIN_SECONDS = 1.0
SILENCE_TAIL = 1.2        # 连续静音多久判定说完


def list_devices() -> None:
    print(sd.query_devices())
    print(f"\n默认输入设备：{sd.default.device[0]}")


def record_until_silence() -> np.ndarray:
    """边录边检测静音，说完自动停止。返回 16bit 单声道 PCM。"""
    frames: list[np.ndarray] = []
    silent_run = 0.0
    duration = 0.0
    block = int(SAMPLE_RATE * 0.1)  # 100ms 一块
    started = False
    print("  ● 请说话（最长 15s，说完自动停止）… ", end="", flush=True)

    def _cb(indata, _frames, _time, status) -> None:
        nonlocal silent_run, duration, started
        frames.append(indata.copy())
        duration += len(indata) / SAMPLE_RATE
        quiet = rms(indata.flatten()) < SILENCE_RMS
        if not quiet:
            started = True
            silent_run = 0.0
        elif started:
            silent_run += len(indata) / SAMPLE_RATE

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
                        blocksize=block, callback=_cb):
        while duration < MAX_SECONDS and not (started and silent_run >= SILENCE_TAIL):
            sd.sleep(50)
    print("完成")
    return np.concatenate(frames).flatten() if frames else np.array([], dtype="int16")


def record_fixed(seconds: float) -> np.ndarray:
    """固定时长录音。"""
    print(f"  ● 录音 {seconds}s … ", end="", flush=True)
    audio = sd.rec(int(SAMPLE_RATE * seconds), samplerate=SAMPLE_RATE,
                   channels=CHANNELS, dtype="int16")
    sd.wait()
    print("完成")
    return audio.flatten()


def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if audio.size else 0.0


async def transcribe_once(asr: SenseVoiceASR, audio: np.ndarray) -> None:
    level = rms(audio)
    print(f"  音量 RMS = {level:.0f}（静音阈值 {SILENCE_RMS:.0f}）")
    if level < SILENCE_RMS:
        print("  ✗ 基本没录到声音，检查麦克风是否选对/被静音")
        return
    pcm = audio.tobytes()
    t = await asr.transcribe(pcm, SAMPLE_RATE)
    print(f"  文本 : {t.text or '(空)'}")
    print(f"  情感 : {t.emotion}  语言 : {t.language}")
    print(f"  事件 : {[e.type.name for e in t.events]}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="录 5 秒自动识别一轮后退出")
    parser.add_argument("--list", action="store_true", help="列出音频设备")
    args = parser.parse_args()

    if args.list:
        list_devices()
        return

    print("正在加载 SenseVoice（首次需下载，已缓存则数秒）…")
    asr = SenseVoiceASR(device="cpu")
    if not asr.is_available():
        print("✗ funasr 未安装：", asr._load_error)
        return
    asr._ensure_model()
    print("✓ 模型就绪，设备：", asr._device)

    print("\n默认输入设备：", sd.query_devices(kind="input")["name"])

    if args.auto:
        await transcribe_once(asr, record_fixed(5))
        return

    print("按回车开始说话（说完自动停止），Ctrl+C 退出")
    while True:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            return
        await transcribe_once(asr, record_until_silence())


if __name__ == "__main__":
    asyncio.run(main())
