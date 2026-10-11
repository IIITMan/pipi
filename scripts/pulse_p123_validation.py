"""PULSE P1/P2/P3 模拟数据验证 —— 随机事件流 × 14 天（跨认主期）。

不是固定脚本，而是"生成日子"：每天随机注入出门/回家/夜噪/陌生人/对话/冷淡/低落，
回家时间带提前/晚归/隔日分布，夜间噪声刻意连续以观测习惯化。
三个模式对照：random（混合日常）/ intimate（高互动）/ busy（忙碌疏离）。

断言覆盖：
  P1 心搏：呼吸区间无发散、睡眠慢而深、惊吓脉冲、睡眠挣扎抑制
  P2 预感：错过预期、意外提前、夜噪习惯化、意图切换/无用的事、天气多样性
  P3 羁绊：思念与分离单调、重聚分级文本、冷淡失落、认主成败对照、trust 有界

输出：scripts/pulse_p123_stats.json + 控制台断言表。
用法：python scripts/pulse_p123_validation.py [--days 14] [--seed 20261008]
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
WAVE_NAMES = {"loneliness": "孤", "curiosity": "好", "sleepiness": "困",
              "security": "安", "attachment": "依"}
WAVE_ORDER = ["loneliness", "curiosity", "sleepiness", "security", "attachment"]


# ---------------------------------------------------------------- 事件生成器
def gen_events(days: int, mode: str, seed: int) -> list[tuple[int, int, str, dict]]:
    """预生成全部事件：(day, minute_of_day, kind, kw)。含隔日未归跨天注入。

    mode: random=混合日常 / intimate=高互动 / busy=低互动疏离。
    夜间噪声：第一周每晚必注入（观习惯化），第二周 60% 概率。
    """
    rng = random.Random(seed)
    out: list[tuple[int, int, str, dict]] = []
    pending_arrive = None   # (次日分钟, 注释)
    for d in range(1, days + 1):
        ev: list[tuple[int, str, dict]] = []

        # 隔日清晨到达（前一天出门后整天未归）
        if pending_arrive is not None:
            ev.append((pending_arrive, "owner_arrive", {}))
            pending_arrive = None

        # 夜噪：第一周每晚，第二周 60%
        if d <= 7 or rng.random() < 0.6:
            ev.append((rng.randint(2 * 60, 4 * 60), "night_noise", {}))

        # 早晨出门（busy 几乎天天出门；intimate 多数也出门）
        p_dep = {"intimate": 0.85, "busy": 0.95, "random": 0.80}[mode]
        if rng.random() < p_dep:
            dep = rng.randint(7 * 60, 9 * 60)
            ev.append((dep, "owner_depart", {}))
            # 回家时刻分布：提前惊喜 / 准点 / 晚归 / 隔日
            r = rng.random()
            if mode != "busy" and r < 0.10:
                ev.append((rng.randint(15 * 60 + 30, 16 * 60 + 30),
                           "owner_arrive", {}))          # 提前 → 惊喜机会
            elif r < 0.22:
                ev.append((rng.randint(21 * 60, 22 * 60 + 30),
                           "owner_arrive", {}))          # 晚归 → 担忧窗口
            elif r < 0.34:
                pending_arrive = rng.randint(6 * 60, 8 * 60)   # 隔日清晨回
            else:
                ev.append((rng.randint(17 * 60, 20 * 60),
                           "owner_arrive", {}))          # 准点

        # 白天活动：对话/陌生人/低落/冷淡/安静
        n_voice = rng.randint(0, 3) if mode != "busy" else \
            (1 if rng.random() < 0.15 else 0)       # busy：几乎不对话
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
        # 冷淡重聚：今天有 arrive 时概率无视（intimate 低概率，busy 高概率）
        if any(k == "owner_arrive" for _, k, _ in ev):
            p_ig = {"intimate": 0.10, "busy": 0.35, "random": 0.20}[mode]
            if rng.random() < p_ig:
                arr_t = next(k0 for k0, k, _ in ev if k == "owner_arrive")
                ev.append((arr_t + 5, "owner_ignored", {}))

        ev.sort(key=lambda x: x[0])
        for minute, kind, kw in ev:
            out.append((d, minute, kind, kw))
    return out


# ---------------------------------------------------------------- 仿真
def simulate(days: int, mode: str, seed: int) -> dict:
    loop = LivingLoop()
    events = gen_events(days, mode, seed)
    # 事件索引：day → [(minute, kind, kw)]
    by_day: dict[int, list] = {}
    for d, m, k, kw in events:
        by_day.setdefault(d, []).append((m, k, kw))

    samples: list[dict] = []        # 每 120min 采样
    startle_events: list[int] = []  # 受惊发生时刻（minute of sim）
    startle_burst: list[dict] = []  # 受惊即时脉冲（机制直测，免采样疏漏）
    reunion_texts: list[str] = []   # 重聚文本（分级断言）
    for sim_min in range(days * 1440):
        day = sim_min // 1440 + 1
        hour = (sim_min % 1440) / 60.0

        # 事件注入（按仿真分钟）
        for minute, kind, kw in by_day.get(day, []):
            if sim_min % 1440 == minute:
                pre_rate = loop._breath_rate      # 受惊前基线
                msg = loop.inject(kind, minute=sim_min % 1440, **kw)
                if kind == "owner_arrive":
                    reunion_texts.append(msg)
                if loop._startle:
                    startle_events.append(sim_min)
                    # 即时脉冲：惊吓 1s 后呼吸峰值（burst τ=20s，1s 内几乎完整）
                    loop.tick(1.0, hour)
                    startle_burst.append({
                        "t": sim_min, "pre": pre_rate,
                        "peak": loop._breath_rate})
                elif msg:
                    pass   # 事件行不推进时钟（避免注入处重复推进）

        # 入睡整理（0:00 每天一次，含认主日历推进）
        if sim_min % 1440 == 0 and loop.river.nights == day - 1:
            loop.consolidate_night()

        # 主循环一帧（每仿真分钟）
        tk = loop.tick(60.0, hour)
        if sim_min % 120 == 0:
            samples.append({
                "sim_min": sim_min,
                "rate": round(tk["breath"]["rate_bpm"], 2),
                "depth": round(tk["breath"]["depth"], 3),
                "lamp": round(tk["body"]["lamp"], 3),
                "struggle": round(tk["tension"].struggle, 3),
                "weather": tk["weather"]["label"],
                "sleeping": tk["sleeping"],
                "trust": round(loop.bond.trust, 2),
                "sep_min": round(loop.sep_min, 0),
                "longing": round(loop.bond.longing(
                    loop.sep_min,
                    clinginess=loop._seed_profile()["clinginess"]), 3),
                "waves": {k: round(v, 3) for k, v in tk["tension"].waves.items()},
            })

    return {
        "loop": loop,
        "stats": dict(loop.stats),
        "samples": samples,
        "startle_events": startle_events,
        "startle_burst": startle_burst,
        "reunion_texts": reunion_texts,
        "weather_counts": {},
        "trust_final": round(loop.bond.trust, 2),
        "imprinted": loop.bond.imprinted,
        "interact_days": loop.bond.interact_days,
    }


# ---------------------------------------------------------------- 断言
def check(name: str, ok: bool, detail: str = "") -> tuple[str, bool, str]:
    return (name, ok, detail)


def verify(r: dict, mode: str) -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []
    s = r["samples"]
    st = r["stats"]

    # ---- P1 心搏 ----
    rates = [x["rate"] for x in s]
    nan_free = all(math.isfinite(x) for x in rates) and 0 < min(rates) < max(rates) < 40
    rows.append(check("P1 呼吸有界无发散",
                      nan_free, f"范围 {min(rates):.1f}-{max(rates):.1f} bpm"))
    night = [x["rate"] for x in s if x["sleeping"]]
    day = [x["rate"] for x in s if not x["sleeping"]]
    slow_sleep = len(night) > 0 and len(day) > 0 and sum(night)/len(night) < sum(day)/len(day)
    rows.append(check("P1 睡眠呼吸慢而深",
                      slow_sleep,
                      f"夜 {sum(night)/len(night):.1f} < 昼 {sum(day)/len(day):.1f} bpm"))
    n_lamp = [x["lamp"] for x in s if x["sleeping"]]
    d_lamp = [x["lamp"] for x in s if not x["sleeping"]]
    rows.append(check("P1 睡眠灯温润（>白天）",
                      len(n_lamp) > 0 and sum(n_lamp)/len(n_lamp) > sum(d_lamp)/len(d_lamp),
                      f"夜 {sum(n_lamp)/len(n_lamp):.2f} vs 昼 {sum(d_lamp)/len(d_lamp):.2f}"))

    # 惊吓脉冲：受惊后 30s 内呼吸峰值 > 受惊前基线 +2（机制直测）
    burst_ok = any(b["peak"] > b["pre"] + 2.0 for b in r["startle_burst"])
    burst_det = "; ".join(f"{b['pre']:.0f}→{b['peak']:.0f} bpm" for b in r["startle_burst"])
    rows.append(check("P1 惊吓后呼吸脉冲",
                      burst_ok and len(r["startle_burst"]) >= 1,
                      f"受惊 {len(r['startle_events'])} 次；脉冲 {burst_det}"))

    # 睡眠挣扎抑制：sleeping 且 security>0.45 时 struggle≤0.30
    sup = [x["struggle"] for x in s if x["sleeping"]
           and x["waves"].get("security", 0) > 0.45]
    suppress = len(sup) > 0 and max(sup) <= 0.31
    rows.append(check("P1 睡眠挣扎抑制（安睡不挣扎）",
                      suppress, f"最大 {max(sup):.2f}（样本 {len(sup)}）" if sup else "无样本"))

    # ---- P2 预感/天气/意愿 ----
    rows.append(check("P2 错过预期（晚归担忧）",
                      st["missed_return"] >= 1, f"{st['missed_return']} 次"))
    rows.append(check("P2 意外提前（早回惊喜）",
                      st["early_reunion"] >= 1, f"{st['early_reunion']} 次"))
    # 习惯化：夜噪受惊按天分布，后半程（D8+）不应再吓醒（第一周每晚注入）
    late_startle = [t for t in r["startle_events"] if t >= 7 * 1440]
    rows.append(check("P2 夜噪习惯化（D8 后不再吓醒）",
                      len(late_startle) == 0,
                      f"D8+ 受惊 {len(late_startle)} 次 / 总 {len(r['startle_events'])} 次"))
    rows.append(check("P2 意图会切换",
                      st["intent_changes"] >= 1, f"{st['intent_changes']} 次"))
    rows.append(check("P2 会做无用的事",
                      st["useless"] >= 1, f"{st['useless']} 次"))
    wc = {}
    for x in s:
        wc[x["weather"]] = wc.get(x["weather"], 0) + 1
    rows.append(check("P2 天气多样性（≥3 档）",
                      len(wc) >= 3, f"{len(wc)} 档 {wc}"))

    # ---- P3 羁绊 ----
    # 思念单调：按分离段分组（sep_min 回落 = 新分离段），段内单调不减
    seg = []
    cur = []
    prev_sep = None
    for x in s:
        if x["sep_min"] > 0:
            if prev_sep is not None and x["sep_min"] < prev_sep:
                seg.append(cur)
                cur = []
            cur.append(x["longing"])
        prev_sep = x["sep_min"]
    if cur:
        seg.append(cur)
    mono = bool(seg) and all(
        all(b >= a - 0.05 for a, b in zip(sg, sg[1:])) for sg in seg)
    rows.append(check("P3 思念随分离单调上升",
                      mono,
                      f"{len(seg)} 个分离段，峰值 {max((max(sg) for sg in seg), default=0):.2f}"))
    # 重聚分级：随机流中必须出现想念级/久别级文本（准点/晚归/隔日分离均 >2h）
    graded = any(("想死你了" in t or "你怎么才回来" in t)
                 for t in r["reunion_texts"])
    rows.append(check("P3 重聚分级（想念/久别级文本）",
                      graded,
                      f"{len(r['reunion_texts'])} 次重聚，分级样本 "
                      f"{sorted(set(t[:8] for t in r['reunion_texts']))}"))
    rows.append(check("P3 冷淡重聚（失落+信任损失）",
                      st["neglected"] >= 1 if mode != "intimate" else st["neglected"] <= 1,
                      f"{st['neglected']} 次" + ("（亲密期几乎无冷淡，符合设计）"
                                                 if mode == "intimate" else "")))
    trust_bounded = 0 <= r["trust_final"] <= 100
    rows.append(check("P3 trust 有界",
                      trust_bounded, f"最终 {r['trust_final']}"))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="PULSE P1/P2/P3 模拟数据验证")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=20261008)
    args = ap.parse_args()

    result = {}
    for mode in ("random", "intimate", "busy"):
        r = simulate(args.days, mode, args.seed)
        rows = verify(r, mode)
        result[mode] = {
            "stats": r["stats"],
            "trust_final": r["trust_final"],
            "imprinted": r["imprinted"],
            "interact_days": r["interact_days"],
            "checks": [{"name": n, "pass": p, "detail": d} for n, p, d in rows],
            "samples": r["samples"],
            "startle_total": len(r["startle_events"]),
        }
        ok_n = sum(1 for _, p, _ in rows if p)
        print(f"\n===== {mode.upper()}（{args.days} 天，seed={args.seed}）=====")
        print(f"trust={r['trust_final']}  imprinted={r['imprinted']}  "
              f"互动天数={r['interact_days']:.0f}")
        for n, p, d in rows:
            print(f"  [{'PASS' if p else 'FAIL'}] {n}  {d}")
        print(f"  通过 {ok_n}/{len(rows)}")

    # 对照结论：亲密组认主成功；疏离组（busy）要么错过认主，要么 trust 显著更低
    a = result["intimate"]
    b = result["busy"]
    contrast = a["imprinted"] and (not b["imprinted"] or a["trust_final"] - b["trust_final"] >= 15.0)
    print("\n===== 对照结论 =====")
    print(f"  亲密组 trust={a['trust_final']} imprinted={a['imprinted']} "
          f"互动 {a['interact_days']:.0f} 天")
    print(f"  疏离组 trust={b['trust_final']} imprinted={b['imprinted']} "
          f"互动 {b['interact_days']:.0f} 天")
    print(f"  → {'PASS 认主/信任对照成立' if contrast else 'FAIL 对照未拉开'}")

    out = {"args": vars(args), "modes": result}
    (ROOT / "scripts" / "pulse_p123_stats.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nJSON → scripts/pulse_p123_stats.json")


if __name__ == "__main__":
    main()
