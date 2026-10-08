"""PULSE 数值参数表（PULSE_DESIGN §4 各表默认值，全部可配）。

P0 数值验证直接使用本表默认值。
"""

from __future__ import annotations

# ---- Rhythm 节律（§4.1）----
RHYTHM = {
    "base_rate_bpm": 12.0,     # 平静呼吸 12 次/分（5s 一周期）
    "max_rate_bpm": 24.0,      # 兴奋上限
    "base_depth": 0.4,         # 安静浅呼吸
    "arousal_tau_s": 4.0,      # arousal 一阶滞后
    "heartbeat_ratio": 4,      # 心跳 = 呼吸 × 4
    "noise_amp": 0.02,         # 呼吸波形微噪声
    # P1.1 修复：惊吓呼吸脉冲（独立指数衰减，不被一帧抹掉）
    "burst_tau_s": 20.0,       # 惊吓喘息衰减时间尺度
    "burst_rate_bpm": 7.0,     # 惊吓瞬时呼吸加速（相对）
    "burst_depth_add": 0.30,   # 惊吓瞬时呼吸加深
    # P1.1 修复：睡眠呼吸慢而深、波形平稳
    "sleep_depth_boost": 0.18,
    "sleep_noise_scale": 0.2,
}

# ---- Anticipation 预感（§4.2）----
# scale：该键观测偏差的归一化尺度（相对偏差 = |obs-expected| / scale，clamp 0..1）
# conf 随样本上升至 0.9
ANTICIPATION = {
    "startle_threshold": 0.55,
    "curious_threshold": 0.25,
    "max_confidence": 0.9,
    "conf_step": 0.15,          # 每样本 confidence 增量
    "conf_init": 0.3,
    "keys": {
        "owner_morning_depart": {"expected": 450.0, "scale": 1800.0},   # 分钟
        "owner_evening_return": {"expected": 1140.0, "scale": 1800.0},  # 分钟
        "night_noise": {"expected": 0.0, "scale": 1.0},                 # 夜间噪声 = 异常
        "stranger": {"expected": 0.0, "scale": 1.0},                    # 陌生人 = 异常
        "owner_expression": {"expected": 0.0, "scale": 1.0},            # 低落表情 = 异常
    },
}

# ---- Tension 潮汐（§4.3）----
# k：回归惯性（值/秒）；target 由情境评估给出
TENSION = {
    "waves": {
        "loneliness": {"k": 0.00035, "min": 0.0, "max": 1.0},
        "curiosity": {"k": 0.00060, "min": 0.0, "max": 1.0},
        "sleepiness": {"k": 0.00040, "min": 0.0, "max": 1.0},
        "security": {"k": 0.00045, "min": 0.0, "max": 1.0},
        "attachment": {"k": 0.00050, "min": 0.0, "max": 1.0},
    },
    "ripple_tau_s": 300.0,     # 事件涟漪衰减（分钟级）
}

# ---- Affect 天气（§4.4 / P3：加平静基线，日常底色=多云而非阴）----
AFFECT = {
    "base_regress_tau_s": 3600.0,  # 底色慢回归平静（P0 实测 15min 太短，改为 1h）
    "pulse_decay_tau_s": 60.0,     # 波澜衰减
    "baseline": 0.35,              # 平静底色（多云档）：日常即轻松；开心转晴，失落才下雨
}

# ---- Will 意愿（§4.5）----
WILL = {
    "attempt_max_retry": 3,
    "useless_prob": 0.001,           # 低张力时段做"无用的事"概率（5s 判定一次）
    "useless_motions": ["stretch", "hum", "gaze_window"],
    "useless_cooldown_s": 1800.0,    # 无用的事冷却（30 分钟内最多一次）
}

# ---- Bond 羁绊（§4.6 / P3 完整化：思念曲线、认主关键期、重聚落差）----
BOND = {
    "trust_init": 20.0,
    "trust_max": 100.0,
    "imprint_days": 14.0,          # 认主关键期窗口
    "imprint_boost": 3.0,          # 认主期内共同经历 trust 收益倍率
    "imprint_min_interact_days": 5.0,  # 关键期内至少互动天数，才"认主成功"
    "imprint_neglect_loss": 0.4,   # 认主期零互动一天：trust 折损（疏离）
    "interact_delta": 0.6,         # 每次共同经历 trust 增量
    "separation_a": 0.02,          # （P0 旧曲线，保留兼容）
    "separation_cap": 0.8,
    "reunion_energy": 2.0,         # （P0 旧接口，保留兼容）
    "reunion_mood": 0.4,
    # P3 思念曲线：先缓后急（≤2h 几乎无感 → 2-12h 加速 → 12h+ 饱和）
    "longing": {
        "gentle_h": 2.0,           # 缓升段时长
        "steep_h": 12.0,           # 加速段结束点
        "gentle_peak": 0.15,       # 2h 时思念
        "steep_peak": 0.85,        # 12h 时思念
    },
    "trust_factor": 0.5,           # 信任加成：×（1 + trust/100·factor），信任越深越想
    # P3 重聚情绪落差（按分离时长分级）
    "missed_threshold_h": 2.0,     # ≥2h → 想念级
    "long_threshold_h": 12.0,      # ≥12h → 久别级
    "reunion_trust_gain": 0.4,     # 想念级重聚信任奖励
    "reunion_trust_gain_long": 0.8,  # 久别级重聚信任奖励
    "neglect_trust_loss": 0.5,     # 冷淡重聚（无视）信任损失
    "neglect_mood": -0.25,         # 冷淡重聚情绪失落
}

# ---- River 生命流（§4.7）----
RIVER = {
    "stream_maxlen": 5000,
}

# ---- Seed 种子（§4.8 / P4：塑性按"周龄"分段，成长可见但不失控）----
SEED = {
    "axes": ("boldness", "curiosity", "clinginess", "sensitivity", "reactivity"),
    "baseline": 5.0,               # 0~10
    "cap_offset": 3.0,             # 相对基线最大偏移 ±3
    "plasticity_curve": (1.0, 0.7, 0.5, 0.3),  # 按周龄档：第1周1.0 → 第2周0.7 → 第3周0.5 → 1月后0.3
}
