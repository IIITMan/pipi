#!/usr/bin/env python3
"""成长系统验收（GROWTH_DESIGN §8 六条 + 里程碑逐项）。

用法：python tests/integration/test_growth.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pipi.app import build_app
from pipi.infra import Config

PASS = 0


def ok(cond, msg):
    global PASS
    assert cond, f"FAIL: {msg}"
    PASS += 1
    print(f"  ✓ {msg}")


async def main():
    app = build_app(Config.default(), sim=True)
    g = app.orchestrator.growth
    ok(g is not None, "sim 装配下 GrowthEngine 存在")
    t0 = app.clock.now()

    # ---- 单向性（§8.6）：内部异常不影响调用方 ----
    def boom(*a, **k):
        raise RuntimeError("boom")
    orig_load = g.load
    g.load = boom
    try:
        await g.on_turn("owner", "x")
        ok(True, "故障注入：on_turn 异常被吞（单向性）")
    except Exception:
        ok(False, "故障注入：异常泄漏！")
    finally:
        g.load = orig_load

    # 分轨：两个独立用户（一次性 ID，避免重跑与幂等约束打架）
    stamp = int(time.time() * 1000)
    uid_a, uid_b = f"growth_test_a{stamp}", f"growth_test_b{stamp}"
    app.repos.user.ensure(uid_a, t0)
    app.repos.user.ensure(uid_b, t0)

    # ---- M1 幂等 + 落表（§8.3）----
    ok(await g.record_milestone(uid_a, "M1", t0), "M1 首次写入成功")
    ok(not await g.record_milestone(uid_a, "M1", t0 + 1), "M1 重复触发不重复写")
    gp_a = g.load(uid_a)
    ok("M1" in gp_a.milestones, "里程碑进入档案")

    # ---- 统计 + M6 晚安计数 ----
    day = 86400.0
    for i in range(3):
        await g.on_turn(uid_a, "晚安", None, now=t0 + i * day)
    gp_a = g.load(uid_a)
    ok(gp_a.turns() == 3 and gp_a.active_days() == 3, "轮次/活跃天数累计")
    ok(gp_a.stats.get("night_wells") == 3, "晚安计数累计")

    # ---- M2：第一个喜好（on_turn 自动触发：likes 非空）----
    profile_b = app.repos.profile.get(uid_b)
    profile_b.likes = ["美式"]
    app.repos.profile.save(profile_b, t0)
    await g.on_turn(uid_b, "随便聊点啥", None, now=t0)
    gp_b = g.load(uid_b)
    ok("M2" in gp_b.milestones, "M2 自动触发（likes 非空 → 记里程碑）")

    # ---- M4：相遇满 30 天（业务时间门槛）----
    await g.on_turn(uid_b, "嗯", None, now=t0 + 29 * day)
    gp_b = g.load(uid_b)
    ok("M4" not in gp_b.milestones, "M4 相遇 29 天：不触发")
    await g.on_turn(uid_b, "嗯", None, now=t0 + 31 * day)
    gp_b = g.load(uid_b)
    ok("M4" in gp_b.milestones, "M4 相遇 31 天：自动触发")

    # ---- 阶段机（§8.1）：天数不足不推进 ----
    await g.on_turn(uid_b, "嗯", None, now=t0)
    gp_b = g.load(uid_b)
    ok(gp_b.stage == 1, "第 1 天不可能是 S2+（硬性天数门槛）")

    # ---- 可塑性（§8.2）：S1 变化量 > S4（单调性）----
    from pipi.orchestration.growth import GrowthEngine

    class FakeState:
        class mood:
            value = "开心"
        intensity = 0.8

    cur = g._curve()
    d_s1 = 0.08 * 0.8 * cur[0] * 1   # S1 曲线值
    d_s4 = 0.08 * 0.8 * cur[3] * 1   # S4 曲线值
    ok(d_s1 > d_s4, f"可塑性单调性：S1 步长({d_s1:.3f}) > S4({d_s4:.3f})")

    # ---- 分轨（§8.5）：两个 user_id 互不污染 ----
    await g.on_turn(uid_a, "晚安", None, now=t0 + 3 * day)
    gp_a2, gp_b2 = g.load(uid_a), g.load(uid_b)
    ok(gp_a2.stats.get("night_wells", 0) != gp_b2.stats.get("night_wells", 0),
       "分轨隔离：两用户的统计互不影响")

    # ---- growth_log 可回看 ----
    log = g.repos.growth.list_log(uid_a)
    ok(len(log) >= 1, "演化事件写入 growth_log 可回看")

    print(f"\n────────────────────────────────────────────\n结果：{PASS} 项断言全部通过\n")


if __name__ == "__main__":
    asyncio.run(main())
