#!/usr/bin/env python3
"""种子数据 —— 一键造出一段"和主人相处了三个月"的记忆现场。

用途：
  - 演示遗忘曲线、好感度成长、主动搭话 时不用手动聊几百轮
  - 验收测试的可复现基线

用法：
    python3 scripts/seed_demo_data.py            # 写入默认库
    python3 scripts/seed_demo_data.py --clean    # 先清空再写
    python3 scripts/seed_demo_data.py --days 90  # 模拟相处 90 天
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipi.app import build_app
from pipi.domain.memory import MemoryItem, MemoryType
from pipi.infra import Config
from pipi.infra.clock import FakeClock

# 一段铺好的"用户档案 + 对话史"——覆盖四类记忆
SCRIPT: list[tuple[str, str]] = [
    ("我叫阿哲，你可以叫我哲哥", "阿哲"),
    ("我在杭州上班，一个人住", ""),
    ("我养了只猫叫布丁", ""),
    ("我超爱喝美式，一天三杯那种", ""),
    ("我特别讨厌开会，一开就是两小时", ""),
    ("下周三是我生日", ""),
    ("我最近在学做菜，昨天炒糊了", ""),
    ("今天被领导说了，有点烦", ""),
    ("布丁今天又把我的杯子推下桌了", ""),
    ("我答应你，周末一定早点睡", ""),
    ("我其实有点怕黑", ""),
    ("我最近喜欢听陈奕迅", ""),
    ("对了，我下周要去上海出差三天", ""),
    ("你比我家布丁可爱多了", ""),
]

# 闲聊类（这些应该在几天后基本被淡忘）
CHITCHAT: list[str] = [
    "今天天气不错啊",
    "刚吃完饭，有点撑",
    "楼下新开了家奶茶店",
    "刚才看了个挺好笑的视频",
    "今天地铁好挤",
    "外面下雨了",
    "好困啊",
    "晚上吃点什么呢",
]


async def seed(uid: str, days: int, clean: bool) -> None:
    cfg = Config.default()
    clock = FakeClock(__import__("time").time(), "Asia/Shanghai")
    clock.advance(days=-days)                    # 让"相遇"发生在 days 天前
    app = build_app(cfg, sim=True, clock=clock)

    if clean:
        n = await app.memory_store.forget_all(uid)
        print(f"  已清空 {n} 条旧记忆")

    app.repos.user.ensure(uid, clock.now())
    total_written = 0

    # ---- 相识阶段：密集对话 ----
    for i, (text, _) in enumerate(SCRIPT):
        clock.advance(hours=random.uniform(4, 20))
        res = await app.orchestrator.handle_turn(uid, text)
        await asyncio.sleep(0.12)               # 等异步落库
        total_written += len(res.memories_written)
    print(f"  相识阶段：{len(SCRIPT)} 轮对话，写入 {total_written} 条记忆")

    # ---- 日常阶段：大量闲聊（会自然衰减） ----
    for d in range(days):
        for _ in range(random.randint(1, 3)):
            clock.advance(hours=random.uniform(2, 10))
            await app.orchestrator.handle_turn(uid, random.choice(CHITCHAT))
        await asyncio.sleep(0.02)
    print(f"  日常阶段：{days} 天，共 {app.repos.turn.count(uid)} 轮用户发言")

    # ---- 时间快进：让闲聊自然淡去 ----
    clock.advance(days=14)
    now = clock.now()
    rep = await app.memory.decay(uid, now)
    app.repos.decay.save(uid, rep, now)
    print(f"  {rep.summary()}")

    # ---- 结果概览 ----
    items = app.repos.memory.list_live(uid)
    by_type: dict[str, list[MemoryItem]] = {}
    for m in items:
        by_type.setdefault(m.type.value, []).append(m)

    print(f"\n  最终记忆：{len(items)} 条")
    for t, ms in sorted(by_type.items()):
        avg = sum(m.salience(now) for m in ms) / len(ms)
        hl = ms[0].half_life_hours()
        hl_s = "永不" if hl == float("inf") else f"{hl:.0f}h"
        print(f"    {t:12s} {len(ms):3d} 条  平均显著性 {avg:.3f}  半衰期 {hl_s}")

    profile = app.orchestrator.reload_profile(uid)
    state = app.orchestrator.get_state(uid)
    print(f"\n  档案：称呼={profile.call_name or profile.nickname or '—'}"
          f"  喜欢={profile.likes}  讨厌={profile.dislikes}"
          f"  情敌={profile.rivals}")
    print(f"  情绪：{state.mood.value}  好感度 {state.affection:.0f}"
          f"（{state.affection_level}）")

    app.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="皮皮演示数据种子")
    ap.add_argument("--user", default="owner")
    ap.add_argument("--days", type=int, default=60, help="模拟相处天数")
    ap.add_argument("--clean", action="store_true", help="先清空已有数据")
    args = ap.parse_args()

    print(f"正在为 {args.user} 生成 {args.days} 天的相处记录…")
    asyncio.run(seed(args.user, args.days, args.clean))
    print("\n完成。启动查看：python3 run.py --web --sim")


if __name__ == "__main__":
    main()
