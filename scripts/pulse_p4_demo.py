"""PULSE P4 独立演示 —— 生命流成文 + 种子漂移（成长性）。

21 天两组对照：
  · 多刺激组：每晚夜噪（前 10 天）+ 每 2 天陌生人 + 正常互动
  · 平静组  ：无夜噪无陌生人，正常互动 + 偶尔晚归/提前回
每晚入睡时：River 按当天真实经历成文（日记）→ 种子按经历漂移（×塑性）→ 成长句。

用法：
    python scripts/pulse_p4_demo.py              # 默认 21 天两组对照
    python scripts/pulse_p4_demo.py --days 21 --log p4.log
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipi.pulse.loop import LivingLoop

AXES = ("boldness", "curiosity", "clinginess", "sensitivity", "reactivity")
AXIS_ZH = {"boldness": "胆识", "curiosity": "好奇", "clinginess": "依恋",
           "sensitivity": "敏感", "reactivity": "应激"}


def gen_events(days: int, mode: str, seed: int) -> dict[int, list]:
    """按天生成事件（确定性）。mode: 'stim'（多刺激）/ 'calm'（平静）。"""
    import random
    rng = random.Random(seed)
    out: dict[int, list] = {}
    for d in range(1, days + 1):
        ev: list = []
        # 夜噪：刺激组前 10 天每晚 + 后 11 天 60%；平静组无
        if mode == "stim":
            if d <= 10 or rng.random() < 0.6:
                ev.append((rng.randint(2 * 60, 4 * 60), "night_noise", {}))
        # 陌生人：刺激组每 2 天一次；平静组无
        if mode == "stim" and d % 2 == 0:
            ev.append((rng.randint(10 * 60, 20 * 60), "stranger", {}))
        # 出门/回家（两组都有，回家带提前/晚归/隔日分布）
        if rng.random() < 0.9:
            dep = rng.randint(7 * 60, 9 * 60)
            ev.append((dep, "owner_depart", {}))
            r = rng.random()
            if r < 0.10:
                ev.append((rng.randint(15 * 60 + 30, 16 * 60 + 30),
                           "owner_arrive", {}))
            elif r < 0.20:
                ev.append((rng.randint(21 * 60, 22 * 60 + 30),
                           "owner_arrive", {}))
            elif r < 0.28:
                pass   # 隔日清晨回（简化：当天不回）
            else:
                ev.append((rng.randint(17 * 60, 20 * 60), "owner_arrive", {}))
        # 互动：两组相同（对话/低落/安静/冷淡）
        for _ in range(rng.randint(1, 3)):
            ev.append((rng.randint(10 * 60, 21 * 60),
                       "user_voice", {"dur": 10}))
        if rng.random() < 0.3:
            ev.append((rng.randint(11 * 60, 21 * 60), "owner_sad", {}))
        if rng.random() < 0.5:
            ev.append((rng.randint(10 * 60, 21 * 60), "quiet_home", {"dur": 60}))
        if any(k == "owner_arrive" for _, k, _ in ev) and rng.random() < 0.15:
            arr_t = next(k0 for k0, k, _ in ev if k == "owner_arrive")
            ev.append((arr_t + 5, "owner_ignored", {}))
        ev.sort(key=lambda x: x[0])
        out[d] = ev
    return out


def simulate(days: int, mode: str, seed: int) -> dict:
    loop = LivingLoop()
    events = gen_events(days, mode, seed)
    seed_trace: list[dict] = []     # 每 3 天采样
    diaries: list[str] = []
    for sim_min in range(days * 1440):
        day = sim_min // 1440 + 1
        hour = (sim_min % 1440) / 60.0
        for minute, kind, kw in events.get(day, []):
            if sim_min % 1440 == minute:
                loop.inject(kind, minute=sim_min % 1440, **kw)
        if sim_min % 1440 == 0 and loop.river.nights == day - 1:
            n = loop.consolidate_night()
            diaries.append((day, n))
        loop.tick(60.0, hour)
        if sim_min % 4320 == 0:      # 每 3 天
            seed_trace.append({"day": day, **loop._seed_profile()})
    return {
        "loop": loop, "diaries": diaries, "seed_trace": seed_trace,
        "stats": dict(loop.stats),
        "seed_final": loop._seed_profile(),
        "self_depth": loop.river.self_depth, "nights": loop.river.nights,
        "trust": round(loop.bond.trust, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="PULSE P4 独立演示")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--seed", type=int, default=20261009)
    ap.add_argument("--log", type=str, default="")
    args = ap.parse_args()

    log_fh = open(ROOT / "scripts" / (args.log or "pulse_p4_demo.log"),
                  "w", encoding="utf-8") if args.log else None

    def say(line: str) -> None:
        print(line)
        if log_fh:
            log_fh.write(line + "\n")

    results = {}
    for mode, title in (("stim", "多刺激组（夜噪×陌生人）"),
                        ("calm", "平静组（无惊吓，安稳陪伴）")):
        r = simulate(args.days, mode, args.seed)
        results[mode] = r
        loop = r["loop"]
        say("=" * 64)
        say(f"P4 · {title}（{args.days} 天，seed={args.seed}）")
        say("=" * 64)
        # 日记样例：第 1、3、14、21 天
        for day, text in r["diaries"]:
            if day in (1, 2, 7, 14, args.days):
                short = text.replace("\n", " ")
                say(f"D{day:>2}  {short}")
        # 种子演变
        say("  种子：")
        for tr in r["seed_trace"]:
            line = f"    D{tr['day']:>2}  " + "  ".join(
                f"{AXIS_ZH[a]}={tr[a]:.2f}" for a in AXES)
            say(line)
        say(f"  终值  " + "  ".join(
            f"{AXIS_ZH[a]}={r['seed_final'][a]:.2f}" for a in AXES))
        say(f"  self_depth={r['self_depth']}  nights={r['nights']}  "
            f"trust={r['trust']}  受惊={r['stats']['startle']}")

    # ---- 断言 ----
    stim, calm = results["stim"], results["calm"]
    checks = []
    sf, cf = stim["seed_final"], calm["seed_final"]
    checks.append(("种子全部有界[2,8]",
                    all(2 <= sf[a] <= 8 for a in AXES)
                    and all(2 <= cf[a] <= 8 for a in AXES),
                    f"stim max={max(sf.values()):.2f} min={min(sf.values()):.2f}"))
    checks.append(("多刺激组敏感度 > 平静组（惊吓使人敏感）",
                    sf["sensitivity"] > cf["sensitivity"],
                    f"{sf['sensitivity']:.2f} vs {cf['sensitivity']:.2f}"))
    checks.append(("多刺激组应激 > 平静组",
                    sf["reactivity"] > cf["reactivity"],
                    f"{sf['reactivity']:.2f} vs {cf['reactivity']:.2f}"))
    checks.append(("日记含真实经历（非计数模板）",
                    any(("吓醒" in t or "陌生人" in t or "想死你了" in t
                         or "等你" in t or "陪你" in t or "晴天" in t)
                        for _, t in stim["diaries"]),
                    f"{len(stim['diaries'])} 篇日记"))
    checks.append(("self_depth 随天数递增",
                    stim["self_depth"] == args.days + 1,
                    f"self_depth={stim['self_depth']}（期望 {args.days + 1}）"))
    checks.append(("日记措辞有变体（非逐日雷同）",
                    len({t.split("。")[0] for _, t in calm["diaries"]}) >= 3
                    and len({p for _, t in calm["diaries"]
                             for p in t.split("。")[1:2]}) >= 5,
                    f"首句 {len({t.split('。')[0] for _, t in calm['diaries']})} 种"
                    f" / 次句 {len({p for _, t in calm['diaries'] for p in t.split('。')[1:2]})} 种"))

    say("\n" + "=" * 64)
    say("P4 断言")
    say("=" * 64)
    for name, ok, det in checks:
        say(f"  [{'PASS' if ok else 'FAIL'}] {name}  {det}")
    say(f"  通过 {sum(1 for _, ok, _ in checks if ok)}/{len(checks)}")
    say("（纯本地动力学，零 LLM：日记=模板×真实经历×确定性变体，种子=经历×塑性×cap。）")
    if log_fh:
        log_fh.close()


if __name__ == "__main__":
    main()
