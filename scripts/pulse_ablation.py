# -*- coding: utf-8 -*-
"""P3 器官消融验证 —— 关闭某器官后，行为质量是否下降（收益表）。

方法：同一事件流（random/intimate/busy × 14 天，固定 seed），
对 8 个器官逐一禁用（monkey-patch 为恒值/空操作，不改产品代码），
对比全量版本的关键行为指标，判定每个器官的独立价值。

判定（PULSE_REVIEW_TODO P3）：
  - 关闭后行为质量无明显下降 → 无独立价值，考虑简化/移除
  - 关闭后明显损害预测/决策/长期连续性 → 有可验证依据

用法：python scripts/pulse_ablation.py [--days 14] [--seed 20261008]
输出：控制台收益表 + scripts/pulse_ablation_stats.json
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipi.pulse.loop import LivingLoop

MIN = 60.0

# ---------------------------------------------------------------- 事件流
def gen_events(days: int, mode: str, seed: int) -> list[tuple[int, int, str, dict]]:
    """与 pulse_p123_validation 完全同源的事件生成（保证可比）。"""
    rng = random.Random(seed)
    out: list[tuple[int, int, str, dict]] = []
    pending_arrive = None
    for d in range(1, days + 1):
        ev: list[tuple[int, str, dict]] = []
        if pending_arrive is not None:
            ev.append((pending_arrive, "owner_arrive", {}))
            pending_arrive = None
        if d <= 7 or rng.random() < 0.6:
            ev.append((rng.randint(2 * 60, 4 * 60), "night_noise", {}))
        p_dep = {"intimate": 0.85, "busy": 0.95, "random": 0.80}[mode]
        if rng.random() < p_dep:
            dep = rng.randint(7 * 60, 9 * 60)
            ev.append((dep, "owner_depart", {}))
            r = rng.random()
            if mode != "busy" and r < 0.10:
                ev.append((rng.randint(15 * 60 + 30, 16 * 60 + 30),
                           "owner_arrive", {}))
            elif r < 0.22:
                ev.append((rng.randint(21 * 60, 22 * 60 + 30),
                           "owner_arrive", {}))
            elif r < 0.34:
                pending_arrive = rng.randint(6 * 60, 8 * 60)
            else:
                ev.append((rng.randint(17 * 60, 20 * 60), "owner_arrive", {}))
        n_voice = rng.randint(0, 3) if mode != "busy" else \
            (1 if rng.random() < 0.15 else 0)
        for _ in range(n_voice):
            t = rng.randint(10 * 60, 21 * 60)
            if not any(k == "owner_arrive" and abs(k0 - t) < 60
                       for k0, k, _ in ev):
                ev.append((t, "user_voice", {"dur": 10}))
        if rng.random() < (0.40 if mode != "busy" else 0.10):
            ev.append((rng.randint(10 * 60, 20 * 60), "stranger", {}))
        if rng.random() < (0.30 if mode != "busy" else 0.10):
            ev.append((rng.randint(11 * 60, 21 * 60), "owner_sad", {}))
        if rng.random() < (0.5 if mode != "busy" else 0.3):
            ev.append((rng.randint(10 * 60, 21 * 60), "quiet_home", {"dur": 60}))
        if any(k == "owner_arrive" for _, k, _ in ev):
            p_ig = {"intimate": 0.10, "busy": 0.35, "random": 0.20}[mode]
            if rng.random() < p_ig:
                arr_t = next(k0 for k0, k, _ in ev if k == "owner_arrive")
                ev.append((arr_t + 5, "owner_ignored", {}))
        ev.sort(key=lambda x: x[0])
        for minute, kind, kw in ev:
            out.append((d, minute, kind, kw))
    return out


# ---------------------------------------------------------------- 器官禁用
def disable_organ(loop: LivingLoop, organ: str) -> None:
    """把指定器官 patch 为恒值/空操作（不动产品代码）。

    8→4 精简后 Rhythm/Affect 已无独立对象（降为派生），不再参与消融；
    其余器官与 full 对比。
    """
    if organ == "tension":
        # 驱力场冻结：waves 恒定 0.2，无涟漪无惯性
        def _update(dt, targets, sleeping=False):
            from pipi.pulse.organs.tension import TensionSnapshot
            loop.tension.values = {w: 0.2 for w in
                                   ("loneliness", "curiosity", "sleepiness",
                                    "security", "attachment")}
            return TensionSnapshot(
                dominant="loneliness", struggle=1.0, waves=dict(loop.tension.values))
        loop.tension.update = _update
        loop.tension.push = lambda wave, magnitude: None
    elif organ == "will":
        # 意愿不决策：意图恒 None（无切换、无用的事、打断挂起）
        loop.will.decide = lambda snap: None
        loop.will.interrupt = lambda: None
    elif organ == "bond":
        # 羁绊冻结：trust 恒初值、思念恒 0、重聚无增益
        def _longing(minutes, clinginess=5.0, trust=None):
            return 0.0
        loop.bond.longing = _longing
        loop.bond.interact = lambda delta=None: None
        loop.bond.reunion_response = lambda sep_min, clinginess=5.0: {
            "level": "brief", "mood": 0.0, "trust_gain": 0.0,
            "attach": 0.0, "text": "迎上去"}
        loop.bond.neglected = lambda: {"mood": 0.0, "attach": 0.0,
                                       "text": "凑过去蹭了蹭"}
        loop.bond.advance_days = lambda n=1.0: None
    elif organ == "river":
        # 生命流不整理：无日记叙事、self_depth 不增长（仍推进认主日历由 loop 侧驱动）
        loop.river.consolidate_night = lambda diary=None: ""
    elif organ == "seed":
        # 种子不漂移：五维恒基线（仍可读 profile）
        loop.seed.drift = lambda axis, direction, step=None: None


# ---------------------------------------------------------------- 仿真
def simulate(days: int, mode: str, seed: int,
             disabled: str | None = None) -> dict:
    loop = LivingLoop()
    if disabled is not None:
        disable_organ(loop, disabled)
    events = gen_events(days, mode, seed)
    by_day: dict[int, list] = {}
    for d, m, k, kw in events:
        by_day.setdefault(d, []).append((m, k, kw))

    startle_events: list[int] = []
    weather_counts: dict[str, int] = {}
    rate_extremes = [1e9, -1e9]
    diary_len_total = 0
    diary_days = 0
    for sim_min in range(days * 1440):
        day = sim_min // 1440 + 1
        hour = (sim_min % 1440) / 60.0
        for minute, kind, kw in by_day.get(day, []):
            if sim_min % 1440 == minute:
                loop.inject(kind, minute=sim_min % 1440, **kw)
                if loop._startle:
                    startle_events.append(sim_min)
        if sim_min % 1440 == 0 and loop.river.nights == day - 1:
            n = loop.consolidate_night()
            if n:
                diary_len_total += len(n)
                diary_days += 1
        tk = loop.tick(60.0, hour)
        r = tk["breath"]["rate_bpm"]
        rate_extremes[0] = min(rate_extremes[0], r)
        rate_extremes[1] = max(rate_extremes[1], r)
        weather_counts[tk["weather"]["label"]] = \
            weather_counts.get(tk["weather"]["label"], 0) + 1

    profile = loop.seed.profile()
    return {
        "stats": dict(loop.stats),
        "startle_total": len(startle_events),
        "late_startle": sum(1 for t in startle_events if t >= 7 * 1440),
        "weather_counts": weather_counts,
        "trust_final": round(loop.bond.trust, 2),
        "imprinted": loop.bond.imprinted,
        "interact_days": round(loop.bond.interact_days, 1),
        "rate_range": [round(rate_extremes[0], 1), round(rate_extremes[1], 1)],
        "diary_days": diary_days,
        "diary_avg_len": round(diary_len_total / diary_days, 0)
        if diary_days else 0,
        "self_depth": loop.river.self_depth,
        "seed_spread": round(max(profile.values()) - min(profile.values()), 3),
        "seed_profile": {k: round(v, 3) for k, v in profile.items()},
    }


# ---------------------------------------------------------------- 指标表
KEY_METRICS = [
    ("intent_changes", "意图切换"),
    ("useless", "无用的事"),
    ("missed_return", "晚归担忧"),
    ("early_reunion", "早回惊喜"),
    ("neglected", "冷淡重聚"),
    ("long_reunion", "久别重聚"),
]
DYN_METRICS = [
    ("startle_total", "受惊次数"),
    ("late_startle", "D8+受惊"),
    ("trust_final", "信任"),
    ("interact_days", "互动天数"),
    ("diary_avg_len", "日记均长"),
    ("self_depth", "自我深度"),
    ("seed_spread", "种子离散"),
]


def fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def main() -> None:
    ap = argparse.ArgumentParser(description="P3 器官消融验证")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=20261008)
    args = ap.parse_args()

    organs = ["full", "tension", "will", "bond", "river", "seed"]
    modes = ("random", "intimate", "busy")

    result: dict = {}
    for mode in modes:
        result[mode] = {}
        for org in organs:
            r = simulate(args.days, mode, args.seed,
                         None if org == "full" else org)
            result[mode][org] = r
            print(f"  [{mode:8s}] {org:12s} "
                  f"trust={r['trust_final']:>5} "
                  f"imp={1 if r['imprinted'] else 0} "
                  f"startle={r['startle_total']:>2} "
                  f"late={r['late_startle']:>2} "
                  f"chg={r['stats']['intent_changes']:>3} "
                  f"useless={r['stats']['useless']:>2} "
                  f"weather={len(r['weather_counts'])}档 "
                  f"diary={r['diary_avg_len']:>4.0f} "
                  f"depth={r['self_depth']:>2} "
                  f"seed={r['seed_spread']:.2f} "
                  f"rate={r['rate_range'][0]}-{r['rate_range'][1]}")

    # ---- 收益表（3 模式汇总：与 full 的差异）----
    print("\n===== 器官收益表（对 full 的差异）=====")
    print(f"{'器官':<14}{'行为差异(主要)':<40}{'判定'}")
    full = result["random"]
    for org in organs[1:]:
        diffs = []
        for k, zh in DYN_METRICS + KEY_METRICS:
            f = full["full"][k] if k in full["full"] else \
                full["full"]["stats"].get(k, 0)
            v = full[org][k] if k in full[org] else \
                full[org]["stats"].get(k, 0)
            if isinstance(f, (int, float)) and isinstance(v, (int, float)):
                if abs(f - v) > 1e-9:
                    diffs.append(f"{zh}:{fmt(f)}→{fmt(v)}")
        verdict = "独立价值存疑 → 可简化" if not diffs else "有机制影响"
        print(f"  {org:<12}{('; '.join(diffs[:5])):<38}{verdict}")

    (ROOT / "scripts" / "pulse_ablation_stats.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nJSON → scripts/pulse_ablation_stats.json")


if __name__ == "__main__":
    main()
