#!/usr/bin/env python3
"""遗忘曲线演示 —— 证明"稀疏记忆"的不对称性。

核心主张：皮皮忘记闲聊，但记得喜好和身份。
这个脚本用同一次对话产生的记忆，横向对比 7 天/30 天后的显著性。
"""

import asyncio
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipi.app import build_app
from pipi.infra.config import Config
from pipi.infra.clock import FakeClock


async def main() -> None:
    cfg = Config.default()
    clock = FakeClock(time.time(), "Asia/Shanghai")
    app = build_app(cfg, sim=True, clock=clock)
    uid = "owner"

    # 规则抽取路径能识别"有句式信号"的记忆
    lines = [
        "我叫阿哲",                      # 身份 → pinned
        "我住在杭州，一个人住",           # 身份 → pinned
        "我喜欢喝美式",                  # 喜好 → 长半衰期
        "我答应你，周末一定早点睡",       # 约定 → pinned
    ]
    for t in lines:
        await app.orchestrator.handle_turn(uid, t)
        await asyncio.sleep(0.25)
    await asyncio.sleep(0.4)

    # 闲聊类记忆由 LLM 抽取路径产生（规则路径刻意不抽，见 memory_manager 注释）。
    # 这里直接注入两条，演示它们的快速衰减。
    from pipi.domain.memory import MemoryItem, MemoryType
    now = clock.now()
    for content in ("他今天说天气不错，下楼晒了会儿太阳",
                    "他刚才看了个挺好笑的视频"):
        app.repos.memory.insert(MemoryItem(
            id=f"demo_chitchat_{abs(hash(content)) % 10**8}",
            user_id=uid, type=MemoryType.CHITCHAT, content=content,
            importance=0.3, created_at=now, updated_at=now,
            last_access_at=now,
        ))

    items = app.repos.memory.list_live(uid)

    def sal_at(m, days: float) -> float:
        s0 = m.salience(now)
        hl = m.half_life_hours()
        if hl == float("inf"):
            return s0
        return s0 * math.exp(-math.log(2) / hl * (days * 24))

    print(f"{'类型 / 内容':40s} {'初始':>7s} {'7天':>7s} {'30天':>7s} {'半衰期':>8s}")
    print("-" * 78)
    for m in sorted(items, key=lambda x: (x.type.value, -x.salience(now))):
        hl = m.half_life_hours()
        hls = "永不" if hl == float("inf") else f"{hl:.0f}h"
        label = f"{m.type.value} {m.content[:26]}"
        flag = "📌" if m.pinned else "  "
        print(f"{flag}{label:38s} {m.salience(now):7.3f} "
              f"{sal_at(m, 7):7.3f} {sal_at(m, 30):7.3f} {hls:>8s}")

    print()
    print("结论：")
    print("  · 身份 / 约定（pinned）→ 显著性恒为 1.0，永不衰减")
    print("  · 喜好 → 半衰期约 40 天，一个月后仍然记得")
    print("  · 闲聊 → 半衰期约 18 小时，7 天后基本归零")
    print("    （这就是'像人'的关键：人会自然忘掉寒暄，只留重要的）")

    # 真正跑一次 30 天后的衰减巡检，看淘汰结果
    clock.advance(days=30)
    rep = await app.memory.decay(uid, clock.now())
    print()
    print(f"30 天后跑一次衰减巡检：{rep.summary()}")
    survivors = app.repos.memory.list_live(uid)
    print(f"存活记忆 {len(survivors)} 条：")
    for m in survivors:
        print(f"  - [{m.type.value}] {m.content[:32]}  "
              f"(sal={m.salience(clock.now()):.3f})")

    dropped = [m for m in items if m.id not in {s.id for s in survivors}]
    if dropped:
        print(f"被遗忘 {len(dropped)} 条：")
        for m in dropped:
            print(f"  ✗ [{m.type.value}] {m.content[:32]}")

    app.close()


if __name__ == "__main__":
    asyncio.run(main())
