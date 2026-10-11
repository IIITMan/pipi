"""PULSE P2 独立演示 —— 预感完整（错过预期/意外提前/习惯化）+ 天气 + 意图理由。

用法：
    python scripts/pulse_p2_demo.py                # 默认 3 天
    python scripts/pulse_p2_demo.py --days 3 --accel 0.05 --step-min 10
    python scripts/pulse_p2_demo.py --log p2.log

P2 相对 P1 新增：
  · 错过预期：主人该回未回（19:30-20:30 窗口）→ 担忧（孤独涨/安全跌/心情转雨）
  · 意外提前：早于预期 >30min 回来 → 惊喜脉冲（呼吸一振 + 底色转晴）
  · 习惯化：同一夜间噪声连三晚 → 第一晚吓醒、第三晚只"翻个身"
  · 天气标签：底色+波澜 → 晴/多云/阴/小雨/阵雨/雷雨
  · 意图理由：每帧显示"为什么此刻想做这个"（Will 完整）
  · 惊吓打断：受惊时挂起当前意图，多次打断后放弃（"刚才想做啥来着，又忘了"）
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

# 3 天场景：D1 主人晚归（错失预期→担忧）；D2 夜噪(吓醒)+陌生人+提前回(惊喜)；
# D3 同一夜噪（习惯化→不吓醒）+ 安静陪伴
SCENARIO = {
    1: [(7 * 60 + 30, "owner_depart", {}),
        (19 * 60 + 55, "owner_arrive", {}),          # 晚 ~1h → 19:30 触发担忧
        (20 * 60 + 10, "user_voice", {"dur": 10}),
        (21 * 60 + 30, "user_voice", {"dur": 10})],
    2: [(3 * 60 + 5, "night_noise", {}),             # 第一晚夜噪 → 吓醒
        (7 * 60 + 30, "owner_depart", {}),
        (14 * 60 + 0, "stranger", {"dur": 2}),
        (18 * 60 + 30, "owner_arrive", {}),          # 早 30min → 惊喜
        (19 * 60 + 10, "user_voice", {"dur": 10}),
        (20 * 60 + 45, "owner_sad", {}),
        (21 * 60 + 30, "user_voice", {"dur": 10})],
    3: [(3 * 60 + 5, "night_noise", {}),             # 第二晚同一噪声 → 习惯化
        (7 * 60 + 30, "owner_depart", {}),
        (10 * 60 + 0, "quiet_home", {"dur": 60}),
        (19 * 60 + 0, "owner_arrive", {}),
        (19 * 60 + 10, "user_voice", {"dur": 10})],
}


def breath_row(breath, cols: int = 24, span_s: float = 60.0) -> str:
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
    it = tk["intent"]
    why = f"（{it.reason}）" if it is not None and it.reason else ""
    return (f"{hm} {b}  {wv} | 挣扎{s.struggle:.2f} | 天气{tk['weather'].label} | "
            f"呼吸{tk['breath'].rate_bpm:5.1f} 灯{body['lamp']:.2f} "
            f"眼{body['eye']} {body['pose']}{act}{why}")


def fmt_event(sim_min: int, kind: str, msg: str, loop: LivingLoop) -> str:
    hm = f"{sim_min // 60:02d}:{sim_min % 60:02d}"
    sec = loop.tension.snapshot().waves["security"]
    return f">> {hm} [{kind}] {msg}  （安全 {sec:.2f}）"


def main() -> None:
    ap = argparse.ArgumentParser(description="PULSE P2 独立演示")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--accel", type=float, default=0.02)
    ap.add_argument("--step-min", type=int, default=10)
    ap.add_argument("--log", type=str, default="")
    args = ap.parse_args()

    loop = LivingLoop()
    log_fh = open(ROOT / "scripts" / (args.log or "pulse_p2_demo.log"),
                  "w", encoding="utf-8") if args.log else None

    def say(line: str) -> None:
        print(line)
        if log_fh:
            log_fh.write(line + "\n")

    total_min = args.days * 1440
    rates: list[tuple[bool, float]] = []
    weather_counts: dict[str, int] = {}
    for sim_min in range(total_min):
        day = sim_min // 1440 + 1
        hour = (sim_min % 1440) / 60.0

        # ---- 事件注入（仿真数据）----
        for minute, kind, kw in SCENARIO.get(day, []):
            if sim_min % 1440 == minute:
                msg = loop.inject(kind, minute=sim_min % 1440, **kw)
                if msg:
                    say(fmt_event(sim_min, kind, msg, loop))
                    if loop._startle:
                        tk = loop.tick(0.5, hour)
                        say("  ↳ " + fmt_line(sim_min, tk))

        # ---- 入睡整理（0:00，每天一次）----
        if sim_min % 1440 == 0 and loop.river.nights == day - 1:
            n = loop.consolidate_night()
            say(f".. {sim_min // 60:02d}:00 睡着了…… 今日整理：{n}")
            loop.sleeping = True

        # ---- 状态行（睡眠 60min 一跳，清醒按 --step-min）----
        step = 60 if loop.sleeping_at(hour) else args.step_min
        if sim_min % step == 0:
            tk = loop.tick(60.0, hour)
            rates.append((loop.sleeping_at(hour), tk["breath"].rate_bpm))
            weather_counts[tk["weather"].label] = \
                weather_counts.get(tk["weather"].label, 0) + 1
            say(fmt_line(sim_min, tk))
            # 错过预期：事件行（担忧）
            if tk["missed"]:
                say(fmt_event(sim_min, "owner_late",
                              "天都黑了……主人怎么还不回来", loop))

        time.sleep(step * args.accel)

    # ---- 汇总 ----
    night = [r for s, r in rates if s]
    day = [r for s, r in rates if not s]
    st = loop.stats
    block = "=" * 64 + "\nP2 DEMO SUMMARY\n" + "=" * 64 + "\n"
    block += f"受惊           {st['startle']} 次（D2 夜噪 + 陌生人；D3 夜噪已习惯化）\n"
    block += f"错过预期       {st['missed_return']} 次（主人晚归 → 担忧）\n"
    block += f"意外提前       {st['early_reunion']} 次（早回 → 惊喜）\n"
    block += f"意图切换       {st['intent_changes']} 次（会改主意）\n"
    block += f"无用的事       {st['useless']} 次\n"
    block += f"重聚迎候       {st['greet']} 次\n"
    block += f"天气分布       {weather_counts}\n"
    block += f"睡眠整理       {loop.river.nights} 次  self_depth={loop.river.self_depth}\n"
    block += f"羁绊 trust     {loop.bond.trust:.1f}\n"
    block += f"呼吸 夜/昼     {sum(night)/len(night):.1f} / {sum(day)/len(day):.1f} bpm\n"
    block += f"个性           {loop._seed_profile()}\n"
    block += "=" * 64 + "\n"
    block += "（纯本地动力学，零 LLM；P3 接羁绊完整，P4 接生命流与种子。）\n"
    if log_fh:
        log_fh.write("\n" + block)
        log_fh.close()
    print(block)


if __name__ == "__main__":
    main()
