"""自动回充控制器自检 —— 直接运行：python3 tests/integration/test_dock_controller.py

不依赖 pytest fixture：与 test_proactive_vision.py 同风格，脚本独立可跑。
覆盖：三段式对齐收敛、超时重试、触点故障、有人靠近让路、低电话术。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pipi.domain.motion import DockState  # noqa: E402
from pipi.orchestration.dock import (  # noqa: E402
    DockConfig,
    DockController,
    DockObservation,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {extra}")


def test_trigger() -> None:
    print("\n[1] 触发条件")
    c = DockController(DockConfig(battery_low_pct=0.25, idle_minutes=20))
    check("高电量+刚交互 → 不触发",
          not c.should_start(0.9, 1.0))
    check("电量 24% → 触发",
          c.should_start(0.24, 1.0))
    check("电量 90% 但闲置 25min → 触发",
          c.should_start(0.90, 25.0))
    check("到点 23:30 → 触发",
          c.should_start(0.90, 1.0, now_hm="23:30"))
    c.status.charging = True
    check("充电中 → 不重复触发",
          not c.should_start(0.10, 99.0))


def test_speech() -> None:
    print("\n[2] 低电话术（陪伴感：先说一声再走）")
    c = DockController()
    s10 = c.low_battery_speech(0.08)
    s20 = c.low_battery_speech(0.18)
    s30 = c.low_battery_speech(0.24)
    check("10% 以内话术含电量与『马上』", "8%" in s10 and "马上" in s10)
    check("20% 以内话术含电量", "18%" in s20)
    check("24% 话术提及电量与补电意向",
          "24%" in s30 and "电" in s30 and ("补" in s30 or "充" in s30))


def test_align_converges() -> None:
    print("\n[3] 三段式对齐收敛（核心：vy 与 wz 正交，互不干扰）")
    c = DockController(DockConfig(vx_kp=0.8, vy_kp=1.2, wz_kp=2.0,
                                  lateral_deadband_mm=8.0,
                                  heading_deadband_deg=3.0,
                                  distance_deadband_mm=12.0))
    c.status.state = DockState.ALIGN

    # 车在桩右 50mm、航向偏 +8°、距桩 300mm
    lat, head, dist = 50.0, 8.0, 0.30
    steps = 0
    while steps < 200:
        out = c.step(DockObservation(distance_m=dist, lateral_mm=lat,
                                     heading_deg=head))
        if out.state is DockState.DOCKED:
            break
        # 理想执行器：速度×1秒 = 位移
        lat -= out.vy * 1000.0
        import math
        head -= math.degrees(out.wz)
        dist -= out.vx
        steps += 1

    check(f"150 拍内收敛（实际 {steps} 拍）", steps < 150)
    check("终止态为 DOCKED", c.status.state is DockState.DOCKED,
          f"实际 {c.status.state}")
    check(f"横向残差 <8mm（{abs(lat):.1f}）", abs(lat) < 8.0)
    check(f"航向残差 <3°（{abs(head):.2f}）", abs(head) < 3.0)


def test_align_wz_does_not_move_lateral() -> None:
    print("\n[4] 正交性验证：纯旋转不应改变横向位置")
    c = DockController()
    c.status.state = DockState.ALIGN
    out = c.step(DockObservation(distance_m=0.30, lateral_mm=0.0,
                                 heading_deg=10.0))
    check("航向偏差 → 只输出 wz", out.wz > 0 and abs(out.vy) < 1e-9)
    out2 = c.step(DockObservation(distance_m=0.30, lateral_mm=40.0,
                                  heading_deg=0.0))
    check("横向偏差 → 只输出 vy", abs(out2.vy) > 0 and abs(out2.wz) < 1e-9)


def test_align_holds_vx_when_misaligned() -> None:
    print("\n[5] 未对正时禁止前进（防歪着撞桩）")
    c = DockController()
    c.status.state = DockState.ALIGN
    out = c.step(DockObservation(distance_m=0.30, lateral_mm=60.0,
                                 heading_deg=0.0))
    check("横向偏 60mm → vx 锁定为 0", abs(out.vx) < 1e-9)
    out2 = c.step(DockObservation(distance_m=0.30, lateral_mm=0.0,
                                  heading_deg=12.0))
    check("航向偏 12° → vx 锁定为 0", abs(out2.vx) < 1e-9)


def test_align_timeout_retry() -> None:
    print("\n[6] 对齐超时 → 退 SEEK 冷却重试 → 超次数后 FAIL")
    c = DockController(DockConfig(timeout_s=25.0, max_retries=2))
    c.status.state = DockState.ALIGN

    def burst(ticks: int = 200):
        """跑一段，直到离开 ALIGN 或跑满 ticks。"""
        last = None
        for _ in range(ticks):
            last = c.step(DockObservation(distance_m=0.30, lateral_mm=100.0,
                                          heading_deg=20.0))
            if last.state is not DockState.ALIGN:
                return last
        return last

    out = burst()
    check("超时后离开 ALIGN（进 SEEK）", out.state is DockState.SEEK,
          f"实际 {out.state}")
    check("重试计数 =1", c.status.retries == 1)

    # 后续两次失败 → 累计 3 > max_retries=2 → FAIL
    for _ in range(2):
        c.status.state = DockState.ALIGN
        c._elapsed = 0.0
        c._retry_cooldown = 0.0
        out = burst()
    check("超过 max_retries → FAIL", out.state is DockState.FAIL,
          f"实际 {out.state}")


def test_human_abort() -> None:
    print("\n[7] 安全：有人靠近立即暂停")
    c = DockController(DockConfig(abort_on_human_near=True, human_near_m=0.8))
    c.status.state = DockState.ALIGN
    out = c.step(DockObservation(distance_m=0.30, lateral_mm=0.0,
                                 heading_deg=0.0, human_distance_m=0.5))
    check("人距 0.5m → stop=True", out.stop)
    check("状态不变", out.state is DockState.ALIGN)
    out2 = c.step(DockObservation(distance_m=0.30, lateral_mm=0.0,
                                  heading_deg=0.0, human_distance_m=2.0))
    check("人走远后恢复输出", not out2.stop)


def test_contact_and_fault() -> None:
    print("\n[8] 触点对接与故障判定")
    c = DockController(DockConfig(contact_full_s=3.0, no_current_fault_s=10.0))
    c.status.state = DockState.DOCKED
    # 触点闭合但无电流 → 10s 后 FAULT
    for _ in range(11):
        c.step(DockObservation(contact_closed=True, charge_current_a=0.0))
    check("无电流 10s → FAULT", c.status.state is DockState.FAULT,
          f"实际 {c.status.state}")
    check("错误信息已记录", "电流" in c.status.last_error)

    # 正常充电
    c2 = DockController(DockConfig(contact_full_s=3.0))
    c2.status.state = DockState.DOCKED
    for _ in range(4):
        c2.step(DockObservation(contact_closed=True, charge_current_a=3.2))
    check("有电流 3s → CHARGING", c2.status.state is DockState.CHARGING)
    check("charging 标志置位", c2.status.charging)
    check("电流已记录", abs(c2.status.charge_current_a - 3.2) < 1e-6)

    # 中途脱离
    c2.step(DockObservation(contact_closed=False))
    check("触点脱离 → 退回 SEEK", c2.status.state is DockState.SEEK)


def test_approach_transition() -> None:
    print("\n[9] 远程段 → 近程红外段的切换")
    c = DockController(DockConfig(near_enter_range_m=0.35))
    c.status.state = DockState.SEEK
    out = c.step(DockObservation(tag_visible=True))
    check("看到 tag → APPROACH", out.state is DockState.APPROACH)

    c.status.state = DockState.APPROACH
    out = c.step(DockObservation(distance_m=1.0))
    check("1.0m → 继续接近且输出 vx>0", out.state is DockState.APPROACH and out.vx > 0)

    out = c.step(DockObservation(distance_m=0.30))
    check("0.30m → 切 ALIGN", out.state is DockState.ALIGN)

    c.status.state = DockState.APPROACH
    out = c.step(DockObservation(distance_m=None))
    check("视觉丢失 → 退回 SEEK", out.state is DockState.SEEK)


def main() -> None:
    print("=" * 62)
    print("皮皮 · 自动回充控制器自检")
    print("=" * 62)
    test_trigger()
    test_speech()
    test_align_converges()
    test_align_wz_does_not_move_lateral()
    test_align_holds_vx_when_misaligned()
    test_align_timeout_retry()
    test_human_abort()
    test_contact_and_fault()
    test_approach_transition()
    print("\n" + "=" * 62)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
