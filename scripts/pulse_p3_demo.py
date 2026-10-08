"""PULSE P3 独立演示 —— 羁绊完整化（思念曲线 / 认主关键期 / 重聚落差）。

用法：
    python scripts/pulse_p3_demo.py                     # 默认 4 天仿真
    python scripts/pulse_p3_demo.py --imprint-test      # 快速认主对照（不跑分钟仿真）
    python scripts/pulse_p3_demo.py --log p3.log

P3 相对 P2 新增：
  · 思念曲线：分离时长 → 思念强度（先缓后急 × clinginess × trust），
    行内以"思念 XX%"显示；孤独目标由思念驱动
  · 重聚落差分级：短离(<2h)轻快 / 想念级(2-12h)"想死你了" / 久别级(≥12h)"你怎么才回来"，
    久别重聚额外获得信任奖励
  · 冷淡重聚：主人回来却不理（owner_ignored）→ 失落 + 信任损失
  · 认主关键期：领养 14 天内共同经历 ×3 信任成长；每晚 advance_days 推进，
    按"有互动的天数"判定认主成功（≥5 天）或错过；连续 2 天零互动 → 疏离折损
  · --imprint-test：两组 14 天对照（高频互动 vs 前 3 天后荒置），打印逐日 trust 与认主事件
"""

from __future__ import annotations

import argparse
import math
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

# 4 天场景：
# D1 上午出门 6.5h → 想念级重聚；下午再出门 2.5h → 想念级；
# D2 出门 24h+ 不归（思念冲顶）→ D3 清晨久别重聚（你怎么才回来）；
# D3 安静陪伴日；D4 短离重聚后主人无视（冷淡失落）+ 安慰 + 陪伴
SCENARIO = {
    1: [(7 * 60 + 30, "owner_depart", {}),
        (14 * 60 + 0, "owner_arrive", {}),          # 分离 6.5h → 想念级
        (16 * 60 + 0, "owner_depart", {}),
        (18 * 60 + 30, "owner_arrive", {}),         # 分离 2.5h → 想念级
        (20 * 60 + 0, "user_voice", {"dur": 10}),
        (21 * 60 + 30, "user_voice", {"dur": 10})],
    2: [(7 * 60 + 30, "owner_depart", {})],          # 整日不归 → 思念冲顶 + 错过预期
    3: [(7 * 60 + 40, "owner_arrive", {}),          # 分离 24h+ → 久别级
        (10 * 60 + 0, "quiet_home", {"dur": 60}),
        (12 * 60 + 0, "user_voice", {"dur": 10}),
        (15 * 60 + 0, "user_voice", {"dur": 10}),
        (21 * 60 + 0, "user_voice", {"dur": 10})],
    4: [(7 * 60 + 30, "owner_depart", {}),
        (10 * 60 + 0, "owner_arrive", {}),          # 分离 2.5h → 想念级
        (10 * 60 + 5, "owner_ignored", {}),         # 回来了却不理 → 冷淡失落
        (14 * 60 + 0, "owner_sad", {}),             # 主人低落 → 挨过去安慰
        (18 * 60 + 0, "user_voice", {"dur": 10}),
        (20 * 60 + 0, "user_voice", {"dur": 10})],
}


def breath_row(breath, cols: int = 24, span_s: float = 60.0) -> str:
    chars = "▁▂▃▄▅▆▇"
    out = []
    for i in range(cols):
        ph = (breath.phase + i * (span_s / cols) * breath.rate_bpm / 60.0) % 1.0
        v = max(-1.0, min(1.0, breath.depth * math.sin(2 * math.pi * ph)))
        out.append(chars[int((v + 1) / 2 * 6.999) % 7])
    return "".join(out)


def fmt_line(sim_min: int, tk: dict, loop: LivingLoop) -> str:
    hm = f"{sim_min // 60:02d}:{sim_min % 60:02d}"
    b = breath_row(tk["breath"])
    s = tk["tension"]
    wv = " ".join(f"{WAVE_NAMES[k]}{s.waves[k]:.2f}" for k in WAVE_ORDER)
    body = tk["body"]
    act = f" | {body['act']}" if body["act"] else ""
    it = tk["intent"]
    why = f"（{it.reason}）" if it is not None and it.reason else ""
    # P3：思念强度（分离中才显示）
    long_s = ""
    if not loop.owner_present:
        lg = loop.bond.longing(loop.sep_min,
                               clinginess=loop.seed.profile()["clinginess"])
        long_s = f" 思念{lg * 100:3.0f}%"
    return (f"{hm} {b}  {wv}{long_s} | 挣扎{s.struggle:.2f} | 天气{tk['weather'].label} | "
            f"呼吸{tk['breath'].rate_bpm:5.1f} 灯{body['lamp']:.2f} "
            f"眼{body['eye']} {body['pose']}{act}{why}")


def fmt_event(sim_min: int, kind: str, msg: str, loop: LivingLoop) -> str:
    hm = f"{sim_min // 60:02d}:{sim_min % 60:02d}"
    sec = loop.tension.snapshot().waves["security"]
    return f">> {hm} [{kind}] {msg}  （安全 {sec:.2f}）"


def run_simulation(args) -> None:
    loop = LivingLoop()
    log_fh = open(ROOT / "scripts" / (args.log or "pulse_p3_demo.log"),
                  "w", encoding="utf-8") if args.log else None

    def say(line: str) -> None:
        print(line)
        if log_fh:
            log_fh.write(line + "\n")

    total_min = args.days * 1440
    rates: list[tuple[bool, float]] = []
    weather_counts: dict[str, int] = {}
    max_longing = 0.0
    for sim_min in range(total_min):
        day = sim_min // 1440 + 1
        hour = (sim_min % 1440) / 60.0

        # ---- 事件注入（仿真数据，按仿真分钟）----
        for minute, kind, kw in SCENARIO.get(day, []):
            if sim_min % 1440 == minute:
                msg = loop.inject(kind, minute=sim_min % 1440, **kw)
                if msg:
                    say(fmt_event(sim_min, kind, msg, loop))
                    if loop._startle:
                        tk = loop.tick(0.5, hour)   # 惊吓即时帧（呼吸脉冲 tau=20s）
                        say("  ↳ " + fmt_line(sim_min, tk, loop))

        # ---- 入睡整理（0:00，每天一次，含认主日历推进）----
        if sim_min % 1440 == 0 and loop.river.nights == day - 1:
            n = loop.consolidate_night()
            say(f".. {sim_min // 60:02d}:00 {n}")

        # ---- 主循环一帧：每仿真分钟 tick 一次（dt=60s，时钟真实推进），
        #      输出按 step-min 稀疏化（睡眠 60min 一跳）----
        sleeping = loop.sleeping_at(hour)
        tk = loop.tick(60.0, hour)
        rates.append((sleeping, tk["breath"].rate_bpm))
        weather_counts[tk["weather"].label] = weather_counts.get(
            tk["weather"].label, 0) + 1
        if not loop.owner_present:
            max_longing = max(max_longing, loop.bond.longing(
                loop.sep_min, clinginess=loop.seed.profile()["clinginess"]))
        step = 60 if sleeping else args.step_min
        if sim_min % step == 0:
            say(fmt_line(sim_min, tk, loop))

    # ---- 汇总 ----
    night = [r for s, r in rates if s]
    day_r = [r for s, r in rates if not s]
    st = loop.stats
    days_left, interact_days = loop.bond.imprint_progress()
    block = "=" * 64 + "\nP3 DEMO SUMMARY\n" + "=" * 64 + "\n"
    block += f"思念峰值       {max_longing * 100:.0f}%（分离中最高）\n"
    block += f"重聚分级       久别 {st['long_reunion']} 次 / 迎候 {st['greet']} 次（含想念级）\n"
    block += f"冷淡重聚       {st['neglected']} 次（失落 + 信任 -{loop.bond.cfg['neglect_trust_loss']}）\n"
    block += f"受惊           {st['startle']} 次\n"
    block += f"意图切换       {st['intent_changes']} 次\n"
    block += f"无用的事       {st['useless']} 次\n"
    block += f"天气分布       {weather_counts}\n"
    block += f"睡眠整理       {loop.river.nights} 次  self_depth={loop.river.self_depth}\n"
    block += f"羁绊 trust     {loop.bond.trust:.1f}（认主期 ×{loop.bond.cfg['imprint_boost']:.0f} 成长中）\n"
    block += f"认主进度       剩余 {days_left:.1f} 天 / 已互动 {interact_days:.0f} 天"
    block += f"（≥{loop.bond.cfg['imprint_min_interact_days']:.0f} 天认主成功）\n"
    block += f"呼吸 夜/昼     {sum(night) / len(night):.1f} / {sum(day_r) / len(day_r):.1f} bpm\n"
    block += f"个性           {loop.seed.profile()}\n"
    block += "=" * 64 + "\n"
    block += "（纯本地动力学，零 LLM；P4 接生命流与种子漂移成文。）\n"
    if log_fh:
        log_fh.write("\n" + block)
        log_fh.close()
    print(block)


def run_imprint_test() -> None:
    """14 天认主对照：A=每天互动 ×2（认主成功）；B=前 3 天互动后荒置（错过）。"""
    print("=" * 64)
    print("IMPRINT TEST —— 14 天认主关键期对照")
    print("=" * 64)

    def show(bond, interact_day: callable) -> None:
        bond.set_days_since_adoption(0.2)
        print(f"day0  领养 0.2 天  trust={bond.trust:.1f}")
        for d in range(1, 15):
            interact_day(d)
            ev = bond.advance_days(1.0)
            line = f"day{d:>2}  trust={bond.trust:5.1f}  互动天数={bond.interact_days:.0f}"
            if ev:
                line += "   ◆ " + ev["text"]
            print(line)
        print(f"      结束 trust={bond.trust:.1f}  imprinted={bond.imprinted}")

    from pipi.pulse.organs.bond import Bond

    print("\n— 对照组 A：每天互动 2 次（模拟亲近的领养）—")
    bond = Bond()
    show(bond, lambda d: (bond.interact(), bond.interact()))

    print("\n— 对照组 B：前 3 天互动，之后 11 天荒置（模拟忙/冷淡）—")
    bond = Bond()
    show(bond, lambda d: (bond.interact(), bond.interact()) if d <= 3 else None)

    print("\n（认主成败只看关键期内'有互动的天数'；连续零互动每日疏离折损。）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="PULSE P3 独立演示")
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--accel", type=float, default=0.02)
    ap.add_argument("--step-min", type=int, default=10)
    ap.add_argument("--log", type=str, default="")
    ap.add_argument("--imprint-test", action="store_true",
                    help="快速认主对照（不跑分钟仿真）")
    args = ap.parse_args()
    if args.imprint_test:
        run_imprint_test()
    else:
        run_simulation(args)
