"""PULSE P0 数值验证 —— 72h 确定性仿真。

目的：验证八器官内部动力学是否呈现"类似生命的形态"
（PULSE_DESIGN §8 评审建议：先证明曲线像活的，再进入 body 与系统接入）。

输入（全部确定性，固定 seed）：
  - 固定事件脚本：昼夜作息、主人出门/回家、对话两轮、陌生人、主人低落、深夜噪声
  - 模拟视觉/对话数据流：owner_arrived/gone、stranger、owner_expression、user_voice
输出：
  - scripts/pulse_p0_output.csv   每 10min 采样全序列
  - scripts/pulse_p0_stats.json   关键统计（供验证报告引用）
  - stdout ASCII 关键节点摘要
运行：python scripts/pulse_p0_validation.py
"""

from __future__ import annotations

import asyncio
import csv
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipi.pulse import derive
from pipi.pulse.organs.tension import TensionField
from pipi.pulse.organs.will import Will
from pipi.pulse.organs.bond import Bond
from pipi.pulse.organs.river import River
from pipi.pulse.organs.seed import Seed

# ---------------------------------------------------------------- 场景参数
SIM_HOURS = 72
DT = 1.0                       # 仿真步长 1s
TOTAL_STEPS = int(SIM_HOURS * 3600 / DT)
SAMPLE_EVERY = 600             # 每 10min 采样
WILL_TICK = 5                  # 行为尺度 5s

MIN = 60.0
HOUR = 3600.0

# 固定事件脚本（分钟轴）：(day, minute, kind, payload)
SCENARIO = [
    # (day, minute, kind)
    (1, 7 * 60 + 30, "owner_depart", {}),          # 7:30 主人出门
    (1, 19 * 60 + 0, "owner_arrive", {}),          # 19:00 回家
    (1, 19 * 60 + 10, "chat", {"dur": 10}),        # 19:10 对话 10min
    (1, 21 * 60 + 30, "chat", {"dur": 10}),        # 21:30 对话 10min
    (2, 7 * 60 + 30, "owner_depart", {}),
    (2, 14 * 60 + 0, "stranger", {"dur": 2}),      # 14:00 陌生人出现 2min
    (2, 19 * 60 + 0, "owner_arrive", {}),
    (2, 19 * 60 + 10, "chat", {"dur": 10}),
    (2, 20 * 60 + 45, "owner_sad", {}),            # 20:45 主人表情低落
    (2, 21 * 60 + 30, "chat", {"dur": 10}),
    (3, 7 * 60 + 30, "owner_depart", {}),
    (3, 3 * 60 + 5, "night_noise", {}),            # 3:05 深夜噪声
    (3, 10 * 60 + 0, "quiet_home", {"dur": 60}),   # 10:00 主人在家安静工作 1h
    (3, 19 * 60 + 0, "owner_arrive", {}),
    (3, 19 * 60 + 10, "chat", {"dur": 10}),
    (3, 21 * 60 + 30, "chat", {"dur": 10}),
]

EVENTS = {}                     # (day, minute) -> list[(kind, dur)]
for day, minute, kind, payload in SCENARIO:
    EVENTS.setdefault((day, minute), []).append((kind, payload.get("dur")))

# ---------------------------------------------------------------- 情境评估
def night_targets() -> dict:
    return {"loneliness": 0.05, "curiosity": 0.15, "sleepiness": 0.85,
            "security": 0.60, "attachment": 0.10}

def daytime_targets(owner_present: bool, sep_min: float,
                    alert: bool, curious: bool, bond: Bond,
                    quiet_home: bool = False) -> dict:
    lon = 0.05
    if not owner_present:
        lon = min(0.75, 0.35 + bond.separation_curve(sep_min) * 0.5)
    return {
        "loneliness": lon,
        "curiosity": 0.15 if quiet_home else (0.55 if curious else 0.20),
        "sleepiness": 0.08,
        "security": 0.12 if alert else (0.70 if owner_present else 0.55),
        "attachment": 0.35 if quiet_home else (0.60 if owner_present else 0.20),
    }

def evening_targets(owner_present: bool, bond: Bond) -> dict:
    t = daytime_targets(owner_present, 0.0, False, False, bond)
    t["sleepiness"] = 0.62
    return t

# ---------------------------------------------------------------- 主循环
async def main() -> dict:
    tension = TensionField()
    will = Will()
    bond = Bond()
    river = River()
    seed = Seed()
    seed.set_depth(1)
    bond.set_days_since_adoption(0.2)     # 领养 0.2 天，认主关键期内

    # 简化习惯化（8→4：原 Anticipation 只剩"异常事件终身只吓一次"）
    habit_done = {"stranger": False, "night_noise": False}

    # 派生展示状态（8→4：原 Rhythm/Affect 器官的数值，见 pulse/derive.py）
    rate, depth, phase = derive.BASE_RATE_BPM, 0.4, 0.0
    burst = 0.0
    mood_base = derive.MOOD_BASELINE
    mood_pulse = 0.0

    # 仿真状态
    owner_present = True
    sep_min = 0.0                         # 已分离分钟
    alert_until = 0.0                     # 警觉结束时刻
    curious_until = 0.0
    quiet_home_until = 0.0
    chatting_until = 0.0
    sleeping = False
    day = 1
    startle_count = 0
    curiosity_count = 0
    intent_changes = 0
    useless_actions = 0
    sleep_narratives: list[str] = []
    first_depart_learned = False

    rows = []
    last_will = 0.0
    prev_intent: str | None = None

    def events_at(t: float):
        """当前秒触发的事件：事件时刻转秒轴，精确命中。"""
        d = int(t // (24 * HOUR)) + 1
        t_in_day = t - (d - 1) * 24 * HOUR
        out = []
        for (ed, em), evs in EVENTS.items():
            if ed == d and abs(t_in_day - em * MIN) < DT * 0.5:
                out.extend(evs)
        return out

    for step in range(TOTAL_STEPS):
        t = step * DT
        minute = t / MIN
        d = int(t // (24 * HOUR)) + 1
        day = d
        h = (minute % 1440) / 60.0

        # ---- 昼夜状态 ----
        sleeping = (h >= 0.0 and h < 7.0)
        if sleeping:
            targets = night_targets()
        elif h >= 22.5:
            targets = evening_targets(owner_present, bond)
        else:
            targets = daytime_targets(owner_present, sep_min,
                                      t < alert_until, t < curious_until,
                                      bond, t < quiet_home_until)

        # ---- 事件处理（离散触发）----
        evs = events_at(t)
        for kind, dur in evs:
            if kind == "owner_depart":
                owner_present = False
                sep_min = 0.0
            elif kind == "owner_arrive":
                owner_present = True
                # 重聚：精神一振（孤独即时消解 + 底色回暖）
                boost = bond.on_reunion()
                mood_base = derive.clamp(mood_base + boost["mood"], -1.0, 1.0)
                tension.push("loneliness", -0.6)
            elif kind == "chat":
                chatting_until = t + dur * MIN
                tension.push("loneliness", -0.45)
                mood_base = derive.clamp(mood_base + 0.20, -1.0, 1.0)
                bond.interact()
            elif kind == "quiet_home":
                quiet_home_until = t + dur * MIN
                owner_present = True
            elif kind == "stranger":
                if not habit_done["stranger"]:
                    habit_done["stranger"] = True
                    startle_count += 1
                    burst = 0.9
                    mood_pulse = derive.clamp(mood_pulse + 0.60, 0.0, 1.0)
                    tension.push("security", -0.45)
                    seed.drift("sensitivity", +1.0)
                alert_until = t + 30 * MIN
                curious_until = t + 10 * MIN
            elif kind == "owner_sad":
                mood_base = derive.clamp(mood_base - 0.30, -1.0, 1.0)
                tension.push("attachment", +0.25)
                bond.interact()                    # 关心也是共同经历
            elif kind == "night_noise":
                if not habit_done["night_noise"]:
                    habit_done["night_noise"] = True
                    startle_count += 1
                    burst = 0.9
                    mood_pulse = derive.clamp(mood_pulse + 0.60, 0.0, 1.0)
                    tension.push("security", -0.4)
                    seed.drift("sensitivity", +0.8)
                alert_until = t + 20 * MIN

        # ---- 持续状态推进 ----
        if not owner_present:
            sep_min += DT / MIN
        if t < chatting_until:
            targets["loneliness"] = 0.02     # 对话中孤独持续被满足
            targets["attachment"] = 0.60

        # 夜晚入睡整理（0:00 后第一次 sleeping，每天一次）
        if sleeping and (t % (24 * HOUR)) < DT and river.nights == day - 1:
            narrative = river.consolidate_night()
            sleep_narratives.append(narrative)
            seed.set_depth(river.self_depth)

        # ---- 器官更新（快循环：Tension + 派生呼吸/心境）----
        arousal = tension.total * (0.4 if sleeping else 1.0)
        burst = derive.burst_decay(burst, DT)
        breath = derive.breath_step(rate, depth, phase, arousal, burst,
                                    DT, sleeping=sleeping)
        rate, depth, phase = (breath["rate_bpm"], breath["depth"],
                              breath["phase"])
        snap = tension.update(DT, targets, sleeping=sleeping)
        mood_base, mood_pulse = derive.decay_mood(mood_base, mood_pulse, DT)
        weather = {"base": mood_base, "pulse": mood_pulse,
                   "label": derive.weather_label(mood_base, mood_pulse)}

        # 行为尺度：Will 5s
        if t - last_will >= WILL_TICK:
            last_will = t
            intent = will.decide(snap)
            if intent is not None:
                if intent.kind == "useless":
                    useless_actions += 1
                elif intent.kind != "abandoned":
                    if prev_intent is not None and prev_intent != intent.kind:
                        intent_changes += 1
                    prev_intent = intent.kind

        # 采样（每 10min）
        if step % SAMPLE_EVERY == 0:
            rows.append({
                "t_min": round(minute, 1),
                "day": day,
                "hour": round(h, 2),
                "sleeping": int(sleeping),
                "owner": int(owner_present),
                "rate_bpm": breath["rate_bpm"],
                "depth": round(breath["depth"], 3),
                "loneliness": snap.waves["loneliness"],
                "curiosity": snap.waves["curiosity"],
                "sleepiness": snap.waves["sleepiness"],
                "security": snap.waves["security"],
                "attachment": snap.waves["attachment"],
                "struggle": snap.struggle,
                "base": weather["base"],
                "pulse": weather["pulse"],
                "weather": weather["label"],
                "trust": round(bond.trust, 2),
                "self_depth": river.self_depth,
            })

    # -------------------------------------------------------- 统计
    def waves_series(name: str):
        return [r[name] for r in rows]

    stats = {
        "sim_hours": SIM_HOURS,
        "rows": len(rows),
        "startle_count": startle_count,
        "curiosity_count": curiosity_count,
        "intent_changes": intent_changes,
        "useless_actions": useless_actions,
        "nights": river.nights,
        "self_depth": river.self_depth,
        "trust_final": round(bond.trust, 2),
        "sleep_narratives": sleep_narratives,
        "seed_profile_final": seed.profile(),
        # 关键节点（供报告引用）
        "key_points": {
            "d1_depart_lon_2h": round(rows[57]["loneliness"], 3),   # 9:30（出门 2h）
            "lon_peak_day1": round(max(waves_series("loneliness")[:72]), 3),
            "lon_after_reunion": round(rows[114]["loneliness"], 3), # 19:00 重聚
            "rate_peak_startle": round(max(r["rate_bpm"] for r in rows
                                           if r["day"] == 2), 2),
            "rate_night_mean": round(sum(r["rate_bpm"] for r in rows
                                         if r["sleeping"]) / max(1, sum(
                r["sleeping"] for r in rows)), 2),
            "rate_day_mean": round(sum(r["rate_bpm"] for r in rows
                                       if not r["sleeping"]) / max(1, sum(
                not r["sleeping"] for r in rows)), 2),
            "struggle_peak_night": round(max(r["struggle"] for r in rows
                                             if r["hour"] >= 22.5 or
                                             (r["hour"] < 7 and r["day"] > 1)), 3),
            "base_after_chat": round(rows[120]["base"], 3),
            "base_day1_mean": round(sum(r["base"] for r in rows
                                        if r["day"] == 1) / 72.0, 3),
            "pulse_after_startle": round(max(r["pulse"] for r in rows
                                             if r["day"] == 2), 3),
        },
    }
    return rows, stats

# ---------------------------------------------------------------- 输出
def write_csv(rows: list[dict]) -> None:
    out = ROOT / "scripts" / "pulse_p0_output.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"CSV -> {out}")

def write_json(stats: dict) -> None:
    out = ROOT / "scripts" / "pulse_p0_stats.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"JSON -> {out}")

def print_summary(stats: dict) -> None:
    kp = stats["key_points"]
    print("=" * 46)
    print("PULSE P0 VALIDATION SUMMARY (72h, deterministic)")
    print("=" * 46)
    print(f"startle events       : {stats['startle_count']}  (expect >=2: stranger + night_noise)")
    print(f"curious events       : {stats['curiosity_count']}")
    print(f"intent changes       : {stats['intent_changes']}  (改主意)")
    print(f"useless actions      : {stats['useless_actions']}  (无用的事)")
    print(f"nights consolidated  : {stats['nights']}  self_depth={stats['self_depth']}")
    print(f"trust final          : {stats['trust_final']}  (start 20, imprint x3)")
    print("-" * 46)
    print(f"loneliness 2h after depart  : {kp['d1_depart_lon_2h']} (思念曲线上升)")
    print(f"loneliness peak day1         : {kp['lon_peak_day1']}")
    print(f"loneliness after reunion     : {kp['lon_after_reunion']} (重聚回落)")
    print(f"rate_bpm peak day2 (startle) : {kp['rate_peak_startle']} (惊吓呼吸加快)")
    print(f"rate_bpm night mean vs day   : {kp['rate_night_mean']} vs {kp['rate_day_mean']} (昼夜放缓)")
    print(f"struggle peak deep-night     : {kp['struggle_peak_night']} (又困又孤独)")
    print(f"affect base after chat       : {kp['base_after_chat']} (对话回暖)")
    print(f"affect pulse after startle   : {kp['pulse_after_startle']} (惊吓波澜)")
    print("-" * 46)
    print("sleep narratives:")
    for n in stats["sleep_narratives"]:
        print("  ", n)
    print("seed profile final:")
    for k, v in stats["seed_profile_final"].items():
        print(f"  {k}: {v}")

if __name__ == "__main__":
    rows, stats = asyncio.run(main())
    write_csv(rows)
    write_json(stats)
    print_summary(stats)
