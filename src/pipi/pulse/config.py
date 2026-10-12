"""PULSE 数值参数表（PULSE_DESIGN §4 各表默认值，全部可配）。

P0 数值验证直接使用本表默认值。
"""

from __future__ import annotations

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

# ---- Will 意愿（§4.5）----
WILL = {
    "attempt_max_retry": 3,
    "useless_prob": 0.001,           # 低张力时段做"无用的事"概率（5s 判定一次）
    "useless_motions": ["stretch", "hum", "gaze_window"],
    "useless_cooldown_s": 1800.0,    # 无用的事冷却（30 分钟内最多一次）
    # ---- P0 决策滞回（借 CE IntentManager 参数，见 PULSE_REVIEW_TODO P0-1）----
    "switch_margin": 0.15,           # 新意图须领先当前意图 ≥0.15 才可切换
    "min_dwell_ticks": 2,            # 普通意图最短持续（min_dwell_time_s=10 / 5s 判定）
    "consecutive_to_switch": 2,      # 新意图连续占优次数（滞回，防微扰横跳）
    "urgent_override_threshold": 0.25,  # 波值差 ≥0.25 = 环境突变，绕过 dwell/consecutive
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
