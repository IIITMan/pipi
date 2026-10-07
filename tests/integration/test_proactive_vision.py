"""阶段 5+6 验收：主动引擎 × 视觉联动 × 运动预留。

验证四件事：
  1. 视觉→语言翻译（不把 JSON 塞给模型）
  2. "主人回家"触发主动打招呼，且话术吃到衣着/表情上下文
  3. 频率上限 / 冷却期 / 夜间静默 / 被忽视降频 全部生效
  4. 虚拟时钟快进验证 idle / morning / night_sleep 触发器
  5. MockMotion 语义动作正确 + 轮趣 C30D 单板（ROS2 话题）驱动头部/灯/麦轮

运行：
    python3 tests/integration/test_proactive_vision.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pipi.app import build_app
from pipi.domain.emotion import EmotionState
from pipi.domain.motion import Axis, MotionCommand
from pipi.infra.clock import FakeClock
from pipi.infra.config import Config
from pipi.providers.motion.mock import MockMotion
from pipi.providers.vision.mock import (
    make_absence_observation,
    make_arrival_observation,
)

TZ = "Asia/Shanghai"
PASS, FAIL = "✓", "✗"
_results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    _results.append((bool(cond), label))
    mark = PASS if cond else FAIL
    line = f"  {mark} {label}"
    if detail:
        line += f"  — {detail}"
    print(line)


def section(title: str) -> None:
    print(f"\n{'─' * 64}\n{title}\n{'─' * 64}")


def make_app(tmp: Path, start: datetime, persona: str = "tsundere"):
    cfg = Config.default()
    cfg.set("memory.db_path", str(tmp / "probe.db"))
    cfg.set("persona.active", persona)
    clock = FakeClock(start.timestamp(), TZ)
    app = build_app(cfg, sim=True, clock=clock)
    return app, clock


# ---------------------------------------------------------------------------
# 1. 视觉 → 语言
# ---------------------------------------------------------------------------

def test_vision_to_language() -> None:
    section("1. 视觉 → 语言翻译（避免 AI 味的关键）")
    obs = make_arrival_observation(clothing_color="藏青色", expression="happy")
    hint = obs.to_hint("阿哲")
    print(f"  注入模型的话：{hint}")
    check(hint is not None and "刚回来" in hint, "识别到'刚回家'")
    check("藏青色" in hint, "衣着颜色被自然语言化")
    check("高兴" in hint, "表情被自然语言化")
    check("json" not in hint.lower() and "{" not in hint, "没有把原始 JSON 塞进去")


# ---------------------------------------------------------------------------
# 2. 回家打招呼闭环
# ---------------------------------------------------------------------------

async def test_return_home(app, clock) -> None:
    section("2. 回家打招呼闭环（视觉 → 主动引擎 → 生成）")
    uid = "owner"
    profile = app.orchestrator.get_profile(uid)
    state = app.orchestrator.get_state(uid)

    # 先让用户不在家（避免 owner_newly_arrived 误判）
    obs = make_arrival_observation(clothing_color="灰色", expression="happy")

    decision = app.proactive_engine.evaluate(
        uid, profile=profile, state=state,
        owner_present=True, owner_newly_arrived=True,
    )
    check(decision.should_fire, "回家触发主动搭话",
          f"trigger={decision.trigger}")
    check(decision.trigger == "return_home", "触发器是 return_home")
    check("回到家" in decision.event or "回来" in decision.event,
          "事件描述含'回家'", decision.event)

    result = await app.orchestrator.handle_proactive(uid, decision.event, vision=obs)
    print(f"  皮皮说：{result.text}")
    check(bool(result.bubbles), "生成了非空回复")

    app.proactive_engine.record_fired(uid, decision.trigger, result.text)

    # 立即再来一次 —— 应被冷却期拦住
    d2 = app.proactive_engine.evaluate(
        uid, profile=profile, state=state,
        owner_present=True, owner_newly_arrived=True,
    )
    check(not d2.should_fire, "冷却期内第二次回家不重复开口",
          d2.suppressed_by)
    return result


# ---------------------------------------------------------------------------
# 3. 频率 / 静默 / 降频
# ---------------------------------------------------------------------------

def test_limits(app, clock) -> None:
    section("3. 频率上限 · 夜间静默 · 被忽视降频")
    uid = "owner"
    profile = app.orchestrator.get_profile(uid)
    state = app.orchestrator.get_state(uid)
    eng = app.proactive_engine

    # --- 每日上限 ---
    eng.repo.log(uid, "return_home", "x", clock.now())
    d = eng.evaluate(uid, profile=profile, state=state,
                     owner_present=True, owner_newly_arrived=True)
    print(f"  今日已用 1/{eng.daily_limit}，上限：{eng.daily_limit}")
    check(eng.daily_limit >= 1, f"人格给出每日上限 {eng.daily_limit} 次")

    # 塞满到上限
    for i in range(eng.daily_limit):
        eng.repo.log(uid, "random", f"filler{i}", clock.now())
    d = eng.evaluate(uid, profile=profile, state=state,
                     owner_present=True, owner_newly_arrived=True)
    check(not d.should_fire, "达到每日上限后不再开口", d.suppressed_by)
    check("上限" in d.suppressed_by, "压制理由说明是上限")

    # --- 夜间静默 ---
    app2, clock2 = make_app_like(app)
    clock2.jump_to_hour(2)
    d = app2.proactive_engine.evaluate(
        uid, profile=profile, state=state,
        owner_present=True, owner_newly_arrived=True)
    check(not d.should_fire, "凌晨 2 点处于静默期", d.suppressed_by)
    check("静默" in d.suppressed_by, "压制理由是静默期")

    # --- 被忽视降频 ---
    app3, clock3 = make_app_like(app)
    for _ in range(3):
        app3.proactive_engine.note_ignored()
    check(app3.proactive_engine.effective_daily_limit() == 0,
          "连续被忽视 3 次 → 自动静默（0 次/天）")
    d = app3.proactive_engine.evaluate(
        uid, profile=profile, state=state,
        owner_present=True, owner_newly_arrived=True)
    check(not d.should_fire, "被忽视后不再主动开口", d.suppressed_by)

    app4, _ = make_app_like(app)
    app4.proactive_engine.note_ignored()
    app4.proactive_engine.note_ignored()
    check(app4.proactive_engine.effective_daily_limit()
          < app4.proactive_engine.daily_limit,
          "被忽视 2 次 → 上限减半")
    app4.proactive_engine.note_user_response(0.0)
    check(app4.proactive_engine.effective_daily_limit()
          == app4.proactive_engine.daily_limit,
          "用户回应后 → 上限恢复")


def make_app_like(app):
    """复制一份同等配置的 app（独立 DB，避免互相污染）。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    cfg = Config.default()
    cfg.set("memory.db_path", str(tmp / "probe.db"))
    cfg.set("persona.active", app.persona.id)
    clock = FakeClock(app.clock.now(), TZ)
    return build_app(cfg, sim=True, clock=clock), clock


# ---------------------------------------------------------------------------
# 4. 虚拟时钟快进 —— 各触发器
# ---------------------------------------------------------------------------

async def test_triggers_by_clock(tmp: Path) -> None:
    section("4. 虚拟时钟快进 —— idle / morning / night_sleep / 重要日子")

    # --- idle：主人太久没理 ---
    start = datetime(2025, 6, 10, 15, 0, tzinfo=None)
    app, clock = make_app(tmp / "idle", start)
    uid = "owner"
    profile = app.orchestrator.get_profile(uid)
    state = app.orchestrator.get_state(uid)
    state.last_interaction = clock.now()
    clock.advance(hours=5)                      # 快进 5 小时
    state.companion_need = 0.0
    d = app.proactive_engine.evaluate(
        uid, profile=profile, state=state, owner_present=False)
    print(f"  快进 5 小时后：should_fire={d.should_fire} trigger={d.trigger}"
          f" 理由={d.suppressed_by or d.reason}")
    check(d.should_fire and d.trigger == "idle", "闲置 5 小时 → 想他/小委屈")

    # --- morning：早间问候 ---
    app2, clock2 = make_app(tmp / "morning", datetime(2025, 6, 11, 8, 0))
    p2 = app2.orchestrator.get_profile(uid)
    s2 = app2.orchestrator.get_state(uid)
    s2.last_interaction = clock2.now() - 60     # 刚聊过，排除 idle
    d = app2.proactive_engine.evaluate(uid, profile=p2, state=s2)
    print(f"  早上 8 点：should_fire={d.should_fire} trigger={d.trigger}")
    check(d.should_fire and d.trigger == "morning", "早间问候触发")

    # --- night_sleep：夜间催睡 ---
    app3, clock3 = make_app(tmp / "night", datetime(2025, 6, 11, 22, 30))
    p3 = app3.orchestrator.get_profile(uid)
    s3 = app3.orchestrator.get_state(uid)
    s3.last_interaction = clock3.now() - 60
    d = app3.proactive_engine.evaluate(uid, profile=p3, state=s3)
    print(f"  22:30：should_fire={d.should_fire} trigger={d.trigger}")
    check(d.should_fire and d.trigger == "night_sleep", "夜间催睡触发")

    # --- important_date：明天生日 ---
    tomorrow = (datetime(2025, 6, 11) + timedelta(days=1)).strftime("%Y-%m-%d")
    app4, clock4 = make_app(tmp / "bday", datetime(2025, 6, 11, 20, 0))
    p4 = app4.orchestrator.get_profile(uid)
    p4.important_dates = {"生日": tomorrow}
    s4 = app4.orchestrator.get_state(uid)
    s4.last_interaction = clock4.now() - 60
    d = app4.proactive_engine.evaluate(uid, profile=p4, state=s4)
    print(f"  明天生日：should_fire={d.should_fire} trigger={d.trigger}"
          f" event={d.event}")
    check(d.should_fire and d.trigger == "important_date", "重要日子提前一天提醒")

    # --- concern_followup：他情绪不好 ---
    app5, clock5 = make_app(tmp / "concern", datetime(2025, 6, 12, 20, 0))
    p5 = app5.orchestrator.get_profile(uid)
    s5 = app5.orchestrator.get_state(uid)
    s5.last_interaction = clock5.now() - 60
    s5.companion_need = 0.7
    d = app5.proactive_engine.evaluate(uid, profile=p5, state=s5)
    print(f"  情绪低落：should_fire={d.should_fire} trigger={d.trigger}")
    check(d.should_fire and d.trigger == "concern_followup", "情绪低落跟进关心")

    # --- 优先级：回家 > 闲置 ---
    app6, clock6 = make_app(tmp / "prio", datetime(2025, 6, 13, 20, 0))
    p6 = app6.orchestrator.get_profile(uid)
    s6 = app6.orchestrator.get_state(uid)
    s6.last_interaction = clock6.now() - 10 * 3600      # 10 小时没理
    d = app6.proactive_engine.evaluate(
        uid, profile=p6, state=s6, owner_present=True, owner_newly_arrived=True)
    print(f"  同时满足 idle 和回家：trigger={d.trigger}")
    check(d.trigger == "return_home", "优先级正确：回家(90) 压过 闲置(50)")


# ---------------------------------------------------------------------------
# 5. 运动层
# ---------------------------------------------------------------------------

async def test_motion(tmp: Path) -> None:
    section("5. 运动控制 —— Mock 语义动作 + 轮趣 C30D 单板驱动")

    # --- MockMotion 打印语义动作 ---
    mot = MockMotion(verbose=True)
    check(mot.is_available(), "MockMotion 可用")
    check(mot.is_simulated(), "MockMotion 标记为模拟")
    await mot.execute_gesture("nod")
    await mot.execute_gesture("happy_wiggle")
    check(len(mot.history) > 0, "动作被执行并记录",
          f"history={len(mot.history)} 条")
    state = await mot.get_state()
    check(isinstance(state, dict) and len(state) > 0, "能查询到轴状态")
    await mot.emergency_stop()
    check(all(v == 0.0 for v in (await mot.get_state()).values()),
          "紧急停止后所有轴归零")

    # --- 轮趣 C30D 单板（ROS2 + micro-ROS，dry-run 无需 ROS2 环境） ---
    from pipi.providers.motion.wheeltec import WheeltecC30D, TwistCmd, HeadCmd
    wt = WheeltecC30D(dry_run=True)
    check(wt.is_available(), "C30D dry-run 模式下可用（无需 rclpy）")
    check(wt.is_simulated(), "C30D 标记为模拟")
    axes = wt.supported_axes()
    check(axes == {Axis.HEAD_PAN, Axis.HEAD_TILT, Axis.LED,
                   Axis.BASE_FORWARD, Axis.BASE_STRAFE, Axis.BASE_ROTATE},
          "C30D 单板承接全部六个自由度（头部 3 + 底盘 3）",
          f"axes={sorted(a.value for a in axes)}")

    # 底盘：速度语义 → /cmd_vel Twist
    wt.published.clear()
    await wt.execute(MotionCommand(axis=Axis.BASE_STRAFE, value=0.5,
                                   duration_ms=600))
    check(len(wt.published) > 0, "横移指令 → /cmd_vel Twist 发布")
    if wt.published:
        t = wt.published[0]
        check(isinstance(t, TwistCmd), "发布的是 TwistCmd（与 rclpy 解耦）")
        check(abs(t.vy) > 0.05 and abs(t.vx) < 1e-9 and abs(t.wz) < 1e-9,
              "横移 → vy 分量非零，vx/wz 保持零",
              f"vy={t.vy:.2f} vx={t.vx:.2f} wz={t.wz:.2f}")

    # 头部：角度语义 → /pipi/head Float32MultiArray
    wt.published_head.clear()
    await wt.execute(MotionCommand(axis=Axis.HEAD_TILT, value=-0.5))
    check(len(wt.published_head) > 0, "点头指令 → /pipi/head 头部话题发布")
    if wt.published_head:
        h = wt.published_head[0]
        check(isinstance(h, HeadCmd), "发布的是 HeadCmd（与 rclpy 解耦）")
        check(h.tilt is not None and h.tilt < 0 and h.pan is None,
              "俯仰 → tilt 为负角度，pan 保持不变（None）",
              f"tilt={h.tilt} pan={h.pan}")

    # 头部是位置语义：发完即停，不设看门狗
    await wt.execute(MotionCommand(axis=Axis.HEAD_PAN,
                                   value=1.0, duration_ms=300))
    st = await wt.get_state()
    check(abs(st["head_pan"] - 120.0) < 1e-6,
          "HEAD_PAN 满档 → 角度语义 +120°（不是速度）", f"pan={st['head_pan']}")

    state = await wt.get_state()
    check("base_strafe" in state, "状态里可查询横移分量")
    check("head_pan" in state and "head_tilt" in state and "led" in state,
          "状态里同时可查询头部三路")

    await wt.emergency_stop()
    last = wt.published[-1]
    check(abs(last.vx) < 1e-9 and abs(last.vy) < 1e-9 and abs(last.wz) < 1e-9,
          "紧急停止 → 发布全零 Twist")
    check(abs((await wt.get_state())["base_strafe"]) < 1e-9, "紧急停止后速度归零")
    check(wt.published_head[-1].led == 0.0, "紧急停止 → 灯带熄灭")
    check(wt.published_head[-1].pan is None,
          "紧急停止不改变头部角度（突然垂头会砸到人）")

    wt.published.clear()
    await wt.execute_gesture("side_step")
    check(any(abs(t.vy) > 0.05 for t in wt.published),
          "SIDE_STEP 手势 → 底盘横移 Twist 序列")

    wt.published_head.clear()
    await wt.execute_gesture("nod")
    check(any(h.tilt is not None for h in wt.published_head),
          "NOD 手势 → 头部 tilt 序列（同一块板子消化）")

    wt.published_head.clear()
    await wt.execute_gesture("look_at", target=(560, 240))
    check(any(h.pan is not None and h.pan > 0 for h in wt.published_head),
          "LOOK_AT 右侧目标 → 头部 pan 为正（转向说话人）",
          f"pan={wt.published_head[0].pan if wt.published_head else None}")
    await wt.close()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

async def main() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="pipi-proactive-"))
    print(f"临时工作目录：{tmp}")

    test_vision_to_language()

    app, clock = make_app(tmp / "home", datetime(2025, 6, 10, 19, 30))
    await test_return_home(app, clock)
    test_limits(app, clock)

    await test_triggers_by_clock(tmp)
    await test_motion(tmp)

    total = len(_results)
    ok = sum(1 for c, _ in _results if c)
    section(f"结果：{ok}/{total} 通过")
    if ok < total:
        print("  失败项：")
        for c, label in _results:
            if not c:
                print(f"    {FAIL} {label}")
        return 1
    print(f"  {PASS} 阶段 5+6 验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
