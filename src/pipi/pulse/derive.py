"""PULSE 派生展示层 —— 原 Affect/Rhythm 器官数值降为纯函数。

8→4 精简（2026-10-11，消融证据 scripts/pulse_ablation.py：二者对行为
零影响，纯展示层）：心境天气与呼吸波形不再作为独立器官对象，由
Tension 驱力 + 事件直接派生。保留原数值语义，P0/P1 断言不受影响。
"""

from __future__ import annotations

import math

# ---- 心境（原 Affect）：底色回归平静 + 波澜指数衰减 ----
MOOD_BASELINE = 0.35           # 平静底色（多云档）：日常即轻松
MOOD_REGRESS_TAU_S = 3600.0    # 底色回归平静 τ（1h，P0 实测 15min 太短）
PULSE_DECAY_TAU_S = 60.0       # 波澜衰减 τ


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def decay_mood(base: float, pulse: float, dt: float) -> tuple[float, float]:
    """一帧心境衰减：底色指数回归平静基线，波澜指数衰减（解析解）。"""
    base = MOOD_BASELINE + (base - MOOD_BASELINE) * math.exp(
        -dt / max(MOOD_REGRESS_TAU_S, 1e-6))
    pulse *= math.exp(-dt / max(PULSE_DECAY_TAU_S, 1e-6))
    return base, pulse


def weather_label(base: float, pulse: float) -> str:
    """天气标签（唯一权威映射，loop 与监控都读这里）。

    标定（baseline=0.35 平静=多云）：强波澜(惊吓) → 阵雨/雷雨；
    开心 → 晴；小委屈/生气 → 阴；主人难过/连续负面 → 小雨；极糟 → 雷雨。
    """
    if pulse >= 0.60:
        return "雷雨" if base < 0.0 else "阵雨"
    if base > 0.45:
        return "晴"
    if base > 0.25:
        return "多云"
    if base > 0.05:
        return "阴"
    if base > -0.15:
        return "小雨"
    return "雷雨"


# ---- 呼吸（原 Rhythm）：arousal 驱动 + 惊吓脉冲指数衰减 ----
BASE_RATE_BPM = 12.0
MAX_RATE_BPM = 24.0
AROUSAL_TAU_S = 4.0
BURST_TAU_S = 20.0
BURST_RATE_BPM = 7.0
BURST_DEPTH_ADD = 0.30
SLEEP_DEPTH_BOOST = 0.18


def burst_decay(burst: float, dt: float) -> float:
    """惊吓/惊喜呼吸脉冲：指数衰减（20s 尺度，受惊后缓缓回落）。"""
    return burst * math.exp(-dt / max(BURST_TAU_S, 1e-6))


def breath_step(rate: float, depth: float, phase: float,
                arousal: float, burst: float, dt: float,
                sleeping: bool = False) -> dict:
    """一帧呼吸波形（原 Rhythm.tick 的解析解，无状态对象）。

    呼吸频率向目标一阶滞后（τ=4s，任意 dt 稳定）；深度由 arousal 现算；
    惊吓脉冲独立于背景 arousal，按 burst 指数衰减。
    """
    target_rate = BASE_RATE_BPM + (MAX_RATE_BPM - BASE_RATE_BPM) * arousal
    rate += (target_rate - rate) * (1.0 - math.exp(-dt / AROUSAL_TAU_S))
    rate += burst * BURST_RATE_BPM

    depth = 0.4 + (1.0 - 0.4) * arousal + burst * BURST_DEPTH_ADD
    if sleeping:
        depth += SLEEP_DEPTH_BOOST

    phase = (phase + dt * rate / 60.0) % 1.0
    return {
        "phase": phase,
        "depth": round(clamp(depth, 0.0, 1.0), 3),
        "rate_bpm": round(rate, 2),
        "heartbeat_bpm": int(rate * 4),
    }
