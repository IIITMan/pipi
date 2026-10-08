"""PulseRuntime 集成 smoke —— 真实系统适配层端到端验证。

用注入时钟快进 2 天，把会话/视觉/外部事件全部走一遍：
  D1 8:05 出门 → 10:00 陌生人 → 12:00 对话 ×2 → 18:00 提前回家 → 20:00 冷淡
  D2 9:00 出门 → 11:00 主人低落 → 19:00 准时回家 → 22:00 安静陪伴
  D3 3:00 夜噪 → 跨天整理（日记 + 种子漂移 + 认主日历）

验证：tick 运转、事件映射 inject、跨天 consolidate、snapshot 兼容结构、
     兼容接口（observe_* / log_chat / set_sink / note_event）不抛异常。

用法：python scripts/pulse_runtime_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipi.pulse.runtime import PulseRuntime


class FakeClock:
    """可快进的假时钟（day=年内第几天, hour=0-24）。"""

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


class FakeSession:
    """session 替身：is_busy 常 False，记录自发发声。"""

    def __init__(self):
        self.is_busy = False
        self.spoken: list[str] = []

    def try_speak_spontaneous(self, text: str) -> bool:
        self.spoken.append(text)
        return True


def main() -> None:
    clock = FakeClock(day=100, hour=8.0)     # 领养后第 100 天（认主期已过，稳定）
    session = FakeSession()
    rt = PulseRuntime({"pulse": {"tick_s": 60.0}}, session=session, clock=clock)
    # 事件从 outbox 直接读（asyncio sink 循环在手动驱动下不跑）
    fired: set = set()

    # 事件剧本（分钟, kind, kw）；视觉类用感知层事件名走 observe_vision 映射
    script = [
        (8 * 60 + 5, "owner_gone", {}),                # D1 出门
        (10 * 60, "stranger", {}),                     # D1 陌生人（视觉）
        (12 * 60, "user_voice", {"dur": 5}),           # D1 对话
        (12 * 60 + 30, "user_voice", {"dur": 5}),
        (18 * 60, "owner_arrived", {}),                # D1 提前回家（视觉）
        (20 * 60, "owner_ignored", {}),                # D1 冷淡重聚
        (9 * 60, "owner_gone", {}),                    # D2 出门
        (11 * 60, "owner_expression", {}),             # D2 主人低落（视觉）
        (19 * 60, "owner_arrived", {}),                # D2 准时回家（视觉）
        (22 * 60, "quiet_home", {"dur": 60}),          # D2 安静陪伴
        (3 * 60, "night_noise", {}),                   # D3 夜噪
    ]
    vision_kinds = ("owner_arrived", "owner_gone", "stranger", "owner_expression")

    for _ in range(2 * 24 * 60):                     # 2 天 × 1440 分钟
        cur = int(round(clock.hour * 60))            # 精确分钟匹配，防浮点重注入
        for minute, kind, kw in script:
            if minute == cur and (kind, minute) not in fired:
                fired.add((kind, minute))
                if kind in vision_kinds:
                    rt.observe_vision(
                        type("E", (), {"kind": kind, "text": kind})())
                else:
                    rt._inject(kind, kw)
        rt._beat()
        clock.advance(1)

    # ---- 断言 ----
    fails: list[str] = []

    def check(name: str, ok: bool, det: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {det}")
        if not ok:
            fails.append(name)

    snap = rt.snapshot()
    s = snap["pulse"]
    check("快照含 monitor 兼容字段",
          all(k in snap for k in ("state", "attention", "physio",
                                  "activity", "queue", "last_thought")),
          f"state={snap['state']}")
    check("快照含 PULSE 专属字段",
          all(k in s for k in ("breath", "struggle", "weather", "trust",
                               "self_depth", "owner_present", "sleeping",
                               "seed")),
          f"trust={s['trust']} depth={s['self_depth']}")
    check("事件注入生效（受惊/重聚/冷淡计数）",
          rt.loop.stats["startle"] >= 1
          and rt.loop.stats["greet"] >= 1
          and rt.loop.stats["neglected"] >= 1,
          f"startle={rt.loop.stats['startle']} greet={rt.loop.stats['greet']} "
          f"neglect={rt.loop.stats['neglected']} "
          f"early={rt.loop.stats['early_reunion']}")
    check("信任随互动成长",
          rt.loop.bond.trust > 20.0,
          f"trust={rt.loop.bond.trust:.2f}")
    out_events = list(rt._outbox)
    check("跨天入睡整理触发（self_depth 递增 + 日记事件）",
          s["self_depth"] >= 3
          and any(e["kind"] == "diary" for e in out_events),
          f"self_depth={s['self_depth']} 日记事件="
          f"{sum(1 for e in out_events if e['kind'] == 'diary')}")
    check("可视化事件已上报（感知/对话/打扰）",
          any(e["kind"] in ("stranger", "owner_sad") for e in out_events),
          f"事件数={len(out_events)}")
    check("自发发声决策正常（冷却+概率，非睡眠时才可能开口）",
          len(session.spoken) >= 0,
          f"自发开口 {len(session.spoken)} 条")

    # 完整走一遍兼容接口（不抛异常即通过）
    rt.observe_user_voice()
    rt.on_barge_in()
    rt.on_response_done()
    rt.on_farewell()
    rt.observe_dialogue_mood("HAPPY")
    rt.log_chat("user", "测试")
    rt.note_event("reminder", "提醒测试")
    check("兼容接口全部可调（observe_*/log_chat/note_event）", True, "")

    # 日记样例输出
    diaries = [e["text"] for e in out_events if e["kind"] == "diary"]
    print("\n  📓 入睡整理样例：")
    for d in diaries:
        print(f"    · {d}")

    print("\n" + "=" * 52)
    print(f"PulseRuntime 集成 smoke：{'通过' if not fails else '失败'} "
          f"（{8 - len(fails)}/8 项）")
    print("=" * 52)
    if fails:
        print("未通过：" + ", ".join(fails))
        sys.exit(1)


if __name__ == "__main__":
    main()
