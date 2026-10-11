"""PULSE P1 独立演示 —— 不接入现有系统，仿真数据驱动，实时日志。

用法：
    python scripts/pulse_p1_demo.py              # 默认 2 天，每仿真分钟 0.02s
    python scripts/pulse_p1_demo.py --days 1 --accel 0.05 --step-min 5
    python scripts/pulse_p1_demo.py --log demo.log   # 同时写日志文件

每行 = 一帧"生命日志"：呼吸波形（24 字符滚动）· 五潮汐 · 挣扎 · 身体表达。
事件行以 >> 开头高亮；入睡时打印"今日整理"。
"""

from __future__ import annotations

import argparse
import math
import sys
import time
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

# 场景（仿真数据：视觉/对话/音频事件），day 从 1 起
SCENARIO = {
    1: [(7 * 60 + 30, "owner_depart", {}),
        (19 * 60 + 0, "owner_arrive", {}),
        (19 * 60 + 10, "user_voice", {"dur": 10}),
        (21 * 60 + 30, "user_voice", {"dur": 10})],
    2: [(3 * 60 + 5, "night_noise", {}),
        (7 * 60 + 30, "owner_depart", {}),
        (14 * 60 + 0, "stranger", {"dur": 2}),
        (19 * 60 + 0, "owner_arrive", {}),
        (19 * 60 + 10, "user_voice", {"dur": 10}),
        (20 * 60 + 45, "owner_sad", {}),
        (21 * 60 + 30, "user_voice", {"dur": 10})],
}


def breath_row(breath, cols: int = 24, span_s: float = 60.0) -> str:
    """当前呼吸相位往后推 span_s 的波形（每行不同 → 滚动感）。"""
    chars = "▁▂▃▄▅▆▇"
    out = []
    for i in range(cols):
        ph = (breath.phase + i * (span_s / cols) * breath.rate_bpm / 60.0) % 1.0
        v = max(-1.0, min(1.0, breath.depth * math.sin(2 * math.pi * ph)))
        out.append(chars[int((v + 1) / 2 * 6.999) % 7])
    return "".join(out)


def fmt_line(sim_min: int, tk: dict) -> str:
    hm = f"{sim_min // 60:02d}:{sim_min % 60:02d}"
    b = breath_row(tk["breath"])
    s = tk["tension"]
    wv = " ".join(f"{WAVE_NAMES[k]}{s.waves[k]:.2f}" for k in WAVE_ORDER)
    body = tk["body"]
    act = f" | {body['act']}" if body["act"] else ""
    return (f"{hm} {b}  {wv} | 挣扎{s.struggle:.2f} | "
            f"呼吸{tk['breath'].rate_bpm:5.1f} 灯{body['lamp']:.2f} "
            f"眼{body['eye']} {body['pose']}{act}")


def fmt_event(sim_min: int, kind: str, msg: str, loop: LivingLoop) -> str:
    hm = f"{sim_min // 60:02d}:{sim_min % 60:02d}"
    sec = loop.tension.snapshot().waves["security"]
    return f">> {hm} [{kind}] {msg}  （安全 {sec:.2f}）"


def main() -> None:
    ap = argparse.ArgumentParser(description="PULSE P1 独立演示")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--accel", type=float, default=0.02,
                    help="每仿真分钟的真实秒数")
    ap.add_argument("--step-min", type=int, default=10,
                    help="清醒时日志行间隔（仿真分钟）")
    ap.add_argument("--log", type=str, default="",
                    help="额外写入日志文件路径")
    args = ap.parse_args()

    loop = LivingLoop()
    log_fh = open(ROOT / "scripts" / (args.log or "pulse_p1_demo.log"),
                  "w", encoding="utf-8") if args.log else None

    def say(line: str) -> None:
        print(line)
        if log_fh:
            log_fh.write(line + "\n")

    total_min = args.days * 1440
    rates: list[tuple[bool, float]] = []
    for sim_min in range(total_min):
        day = sim_min // 1440 + 1
        hour = (sim_min % 1440) / 60.0

        # ---- 事件注入（仿真数据）----
        for minute, kind, kw in SCENARIO.get(day, []):
            if sim_min % 1440 == minute:
                msg = loop.inject(kind, **kw)
                if msg:
                    say(fmt_event(sim_min, kind, msg, loop))
                    # 惊吓后立即补一帧"即时反应"（呼吸急促喘息，τ≈20s 衰减）
                    if loop._startle:
                        tk = loop.tick(0.5, hour)
                        say("  ↳ " + fmt_line(sim_min, tk))

        # ---- 入睡整理（0:00，每天一次）----
        if sim_min % 1440 == 0 and loop.river.nights == day - 1:
            n = loop.consolidate_night()
            say(f".. {sim_min // 60:02d}:00 睡着了…… 今日整理：{n}")
            # 整理后强制进入睡眠状态（0:00 已深睡）
            loop.sleeping = True

        # ---- 状态行（睡眠 60min 一跳，清醒按 --step-min）----
        step = 60 if loop.sleeping_at(hour) else args.step_min
        if sim_min % step == 0:
            tk = loop.tick(60.0, hour)
            rates.append((loop.sleeping_at(hour), tk["breath"].rate_bpm))
            say(fmt_line(sim_min, tk))

        time.sleep(step * args.accel)

    if log_fh:
        log_fh.close()

    # ---- 汇总 ----
    night = [r for s, r in rates if s]
    day = [r for s, r in rates if not s]
    st = loop.stats
    print("=" * 64)
    print("P1 DEMO SUMMARY")
    print("=" * 64)
    print(f"受惊           {st['startle']} 次（陌生人 + 深夜噪声，因果触发）")
    print(f"好奇分档       {st['curious']} 次")
    print(f"意图切换       {st['intent_changes']} 次（会改主意）")
    print(f"无用的事       {st['useless']} 次（发呆/哼歌/伸懒腰）")
    print(f"重聚迎候       {st['greet']} 次")
    print(f"睡眠整理       {loop.river.nights} 次  self_depth={loop.river.self_depth}")
    print(f"羁绊 trust     {loop.bond.trust:.1f}（认主关键期 ×3 成长中）")
    print(f"呼吸 夜/昼     {sum(night)/len(night):.1f} / {sum(day)/len(day):.1f} bpm")
    print(f"个性           {loop._seed_profile()}")
    print("=" * 64)
    print("（这是纯本地动力学，零 LLM；下一步 body 接真硬件表达。）")


if __name__ == "__main__":
    main()
