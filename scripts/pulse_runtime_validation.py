"""PULSE 接入层模拟数据验证 —— PulseRuntime（真实适配层）× 随机事件流 × 14 天。

与 P123 用同一套"生成日子"事件流（random/intimate/busy 三组对照），但验证对象
从"心脏本身"换成"接入真实系统后的适配层"：

  链路层：视觉事件映射（observe_vision）、会话注入、提醒 note_event → 全部生效
  表达层：自发发声接通 session（>0 且克制）、监控快照结构完整、monitor 兼容
  成长层：跨天整理按真实日历触发（日记 + self_depth + 种子有界）、认主对照成立
  心脏回归：呼吸有界无发散（接入层不改动力学）

输出：scripts/pulse_runtime_stats.json + 控制台断言表。
用法：python scripts/pulse_runtime_validation.py [--days 14] [--seed 20261008]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipi.pulse.runtime import PulseRuntime

# ---------------------------------------------------------------- 时钟替身
class FakeClock:
    """可快进时钟（day=第几天 1..N, hour=0-24）。"""

    def __init__(self, day: int = 1, hour: float = 8.0):
        self.day = day
        self.hour = hour

    def now_hour(self) -> float:
        return self.hour

    def now_minute(self) -> int:
        return int((self.hour % 1.0) * 60)

    def now_day(self) -> int:
        return self.day

    def advance(self, minutes: float) -> None:
        self.hour += minutes / 60.0
        while self.hour >= 24.0:
            self.hour -= 24.0
            self.day += 1

    def mono(self) -> float:
        """单调时钟：按仿真时间推进（发声冷却/人声节流在仿真中生效）。"""
        return self.day * 86400.0 + self.hour * 3600.0


class FakeSession:
    """session 替身：is_busy 常 False，记录自发发声。"""

    def __init__(self):
        self.is_busy = False
        self.spoken: list[str] = []

    def try_speak_spontaneous(self, text: str) -> bool:
        self.spoken.append(text)
        return True


# ---------------------------------------------------------------- 事件生成器
# 与 pulse_p123_validation.py 同构（random/intimate/busy 三组）
def gen_events(days: int, mode: str, seed: int) -> list[tuple[int, int, str, dict]]:
    import random
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
        n_voice = {"intimate": rng.randint(2, 4),          # 高互动：几乎天天聊
                   "random": rng.randint(0, 3),
                   "busy": (1 if rng.random() < 0.15 else 0)}[mode]
        for _ in range(n_voice):
            t = rng.randint(10 * 60, 21 * 60)
            if not any(k == "owner_arrive" and abs(k0 - t) < 60
                       for k0, k, _ in ev):
                ev.append((t, "user_voice", {"dur": 10}))
        if rng.random() < (0.40 if mode != "busy" else 0.10):
            ev.append((rng.randint(10 * 60, 20 * 60), "stranger", {}))
        if rng.random() < {"intimate": 0.40, "busy": 0.10,
                           "random": 0.30}[mode]:
            ev.append((rng.randint(11 * 60, 21 * 60), "owner_sad", {}))
        if rng.random() < {"intimate": 0.60, "busy": 0.30,
                           "random": 0.50}[mode]:
            ev.append((rng.randint(10 * 60, 21 * 60), "quiet_home", {"dur": 60}))
        if any(k == "owner_arrive" for _, k, _ in ev):
            p_ig = {"intimate": 0.10, "busy": 0.35, "random": 0.20}[mode]
            if rng.random() < p_ig:
                arr_t = next(k0 for k0, k, _ in ev if k == "owner_arrive")
                ev.append((arr_t + 5, "owner_ignored", {}))
        # 提醒：每天 0-2 次（外部模块 → note_event → 日记留痕）
        for _ in range(rng.randint(0, 2)):
            ev.append((rng.randint(8 * 60, 22 * 60),
                       "reminder", {"text": "提醒主人带钥匙"}))
        ev.sort(key=lambda x: x[0])
        for minute, kind, kw in ev:
            out.append((d, minute, kind, kw))
    return out


# ---------------------------------------------------------------- 仿真（走真实适配层）
def simulate(days: int, mode: str, seed: int) -> dict:
    clock = FakeClock(day=1, hour=8.0)
    session = FakeSession()
    rt = PulseRuntime({"pulse": {"tick_s": 60.0,
                                 "vocal_cooldown_s": 90.0,
                                 "vocal_prob": 0.008}},
                      session=session, clock=clock, mono=clock.mono)
    events = gen_events(days, mode, seed)
    by_day: dict[int, list] = {}
    for d, m, k, kw in events:
        by_day.setdefault(d, []).append((m, k, kw))

    VISION = {"owner_arrive": "owner_arrived", "owner_depart": "owner_gone",
              "stranger": "stranger", "owner_sad": "owner_expression"}
    samples: list[dict] = []
    diary_texts: list[str] = []
    prev_day = clock.day
    for _ in range(days * 1440):
        cur = int(round(clock.hour * 60))
        for minute, kind, kw in by_day.get(clock.day, []):
            if minute == cur:
                if kind == "reminder":
                    rt.note_event("reminder", kw["text"])
                elif kind in VISION:
                    rt.observe_vision(
                        type("E", (), {"kind": VISION[kind], "text": kind})())
                elif kind == "user_voice":
                    rt._inject(kind, kw)     # 仿真跳过 1s 防抖（硬件语义）
                else:
                    rt._inject(kind, kw)
        rt._beat()
        clock.advance(1)
        # 跨天：从 outbox 独立收集当天日记（避免被其他事件挤出）
        if clock.day != prev_day:
            for e in reversed(list(rt._outbox)):
                if e["kind"] == "diary":
                    diary_texts.append(e["text"])
                    break
            prev_day = clock.day
        if int(clock.hour) % 8 == 0 and abs(clock.hour % 1.0) < 0.02:
            samples.append(rt.snapshot())
    diary_texts.reverse()
    return {
        "loop": rt.loop, "rt": rt,
        "stats": dict(rt.loop.stats),
        "diary_texts": diary_texts,
        "samples": samples,
        "spoken": session.spoken,
        "outbox": list(rt._outbox),
        "trust_final": round(rt.loop.bond.trust, 2),
        "imprinted": rt.loop.bond.imprinted,
        "interact_days": rt.loop.bond.interact_days,
        "self_depth": rt.loop.river.self_depth,
        "seed_final": rt.loop.seed.profile(),
    }


# ---------------------------------------------------------------- 断言
def verify(r: dict, mode: str, days: int) -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []
    st = r["stats"]

    def chk(name, ok, det=""):
        rows.append((name, ok, det))

    # ---- 链路层：视觉/会话/提醒全部生效 ----
    chk("链路 视觉映射生效（陌生人/到家/低落）",
        st["startle"] >= 1 and
        (st["greet"] + st["early_reunion"] + st["long_reunion"]) >= 1,
        f"startle={st['startle']} 重聚(g+e+l)="
        f"{st['greet'] + st['early_reunion'] + st['long_reunion']}")
    chk("链路 对话注入生效（互动计数）",
        r["loop"].bond.interact_days >= 1,
        f"互动 {r['interact_days']:.0f} 天")
    chk("链路 提醒 note_event 写入日记",
        any("提醒" in t for t in r["diary_texts"]),
        f"{sum(1 for t in r['diary_texts'] if '提醒' in t)} 天日记含提醒")

    # ---- 表达层：自发发声接通且克制 ----
    chk("表达 自发发声接通 session",
        len(r["spoken"]) > 0, f"{len(r['spoken'])} 条")
    chk("表达 自发发声克制（≤300/14天）",
        len(r["spoken"]) <= 300,
        f"{len(r['spoken'])} 条（冷却90s+概率0.008）")

    # ---- 监控层：快照结构完整、字段有界 ----
    snap_ok = len(r["samples"]) > 0 and all(
        all(k in s for k in ("state", "attention", "physio", "activity",
                             "queue", "last_thought", "pulse"))
        for s in r["samples"])
    chk("监控 快照含兼容+PULSE 字段",
        snap_ok, f"{len(r['samples'])} 次采样")
    wlabels = {s["pulse"]["weather"]["label"] for s in r["samples"]}
    chk("监控 天气为六档之一",
        wlabels <= {"晴", "多云", "阴", "小雨", "阵雨", "雷雨"},
        f"{len(wlabels)} 档 {sorted(wlabels)}")
    seed_ok = all(2 <= v <= 8 for v in r["seed_final"].values())
    chk("监控 种子有界[2,8]",
        seed_ok,
        "  ".join(f"{k[:3]}={v:.2f}" for k, v in r["seed_final"].items()))
    rates = [s["pulse"]["breath"]["rate"] for s in r["samples"]]
    chk("心脏 呼吸有界无发散（接入层不改动力学）",
        all(math.isfinite(x) and 0 < x < 40 for x in rates),
        f"范围 {min(rates):.1f}-{max(rates):.1f} bpm")

    # ---- 成长层：跨天整理 + 认主 ----
    # 从 8:00 起跑 days 天会跨 days 次天（day2..day(days+1) 0:00）
    chk("成长 每日跨天整理触发",
        r["self_depth"] >= days and len(r["diary_texts"]) >= days - 2,
        f"self_depth={r['self_depth']} 日记 {len(r['diary_texts'])} 篇")
    chk("成长 trust 有界",
        0 <= r["trust_final"] <= 100, f"最终 {r['trust_final']}")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="PULSE 接入层模拟数据验证")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=20261008)
    args = ap.parse_args()

    result = {}
    total_ok = total = 0
    seed_off = {"random": 0, "intimate": 7, "busy": 13}
    for mode in ("random", "intimate", "busy"):
        r = simulate(args.days, mode, args.seed + seed_off[mode])
        rows = verify(r, mode, args.days)
        result[mode] = {
            "stats": r["stats"],
            "trust_final": r["trust_final"],
            "imprinted": r["imprinted"],
            "interact_days": r["interact_days"],
            "self_depth": r["self_depth"],
            "seed_final": r["seed_final"],
            "spoken": len(r["spoken"]),
            "diary_sample": r["diary_texts"][-1] if r["diary_texts"] else "",
            "checks": [{"name": n, "pass": p, "detail": d} for n, p, d in rows],
        }
        ok_n = sum(1 for _, p, _ in rows if p)
        total_ok += ok_n
        total += len(rows)
        print(f"\n===== {mode.upper()}（{args.days} 天，seed={args.seed}）=====")
        print(f"trust={r['trust_final']}  imprinted={r['imprinted']}  "
              f"互动={r['interact_days']:.0f}天  自发发声={len(r['spoken'])} 条  "
              f"self_depth={r['self_depth']}")
        for n, p, d in rows:
            print(f"  [{'PASS' if p else 'FAIL'}] {n}  {d}")
        print(f"  通过 {ok_n}/{len(rows)}")
        if r["diary_texts"]:
            print(f"  📓 最后一天日记：{r['diary_texts'][-1][:120]}")

    # 认主对照：亲密组认主成功；疏离组错过或信任显著更低
    a = result["intimate"]
    b = result["busy"]
    contrast = a["imprinted"] and (not b["imprinted"]
                                   or a["trust_final"] - b["trust_final"] >= 15.0)
    print("\n===== 对照结论（接入层）=====")
    print(f"  亲密组 trust={a['trust_final']} imprinted={a['imprinted']} "
          f"互动 {a['interact_days']:.0f} 天")
    print(f"  疏离组 trust={b['trust_final']} imprinted={b['imprinted']} "
          f"互动 {b['interact_days']:.0f} 天")
    print(f"  → {'PASS 认主/信任对照成立' if contrast else 'FAIL 对照未拉开'}")
    print(f"\n  总断言 {total_ok}/{total} 通过")
    if contrast:
        total_ok += 1
    total += 1
    print(f"  （含对照）{total_ok}/{total} 通过")

    out = {"args": vars(args), "modes": result,
           "contrast": {"intimate": a, "busy": b, "pass": contrast}}
    (ROOT / "scripts" / "pulse_runtime_stats.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nJSON → scripts/pulse_runtime_stats.json")
    if total_ok < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
