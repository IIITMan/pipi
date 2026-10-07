"""Mock 运动控制 —— 只记录/打印，不做物理动作。

软件阶段用它验证"什么时候该做什么动作"的决策逻辑是否正确。
部署到机器人时换成 WheeltecC30D（轮趣 C30D，详见 providers/motion/wheeltec.py）即可。
"""

from __future__ import annotations

import logging

from pipi.domain.motion import (
    GESTURE_SEQUENCES,
    Axis,
    Gesture,
    MotionCommand,
)

logger = logging.getLogger(__name__)


class MockMotion:
    """模拟运动控制。"""

    name = "mock"

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.history: list[tuple[str, float]] = []
        self._state = {a.value: 0.0 for a in Axis}

    def is_available(self) -> bool:
        return True

    def is_simulated(self) -> bool:
        return True

    def supported_axes(self) -> set[Axis]:
        return set(Axis)

    async def execute(self, cmd: MotionCommand) -> None:
        self._state[cmd.axis.value] = cmd.value
        self.history.append((cmd.axis.value, cmd.value))
        if self.verbose:
            logger.debug("[动作] %s → %.2f (%sms)", cmd.axis.value,
                         cmd.value, cmd.duration_ms or 0)

    async def execute_gesture(self, gesture: str | Gesture,
                              target: tuple[int, int] | None = None) -> None:
        """执行语义动作。"""
        if isinstance(gesture, str):
            try:
                gesture = Gesture(gesture)
            except ValueError:
                logger.debug("未知动作：%s", gesture)
                return

        if self.verbose:
            print(f"  [动作] {_gesture_cn(gesture)}")

        seq = GESTURE_SEQUENCES.get(gesture)
        if seq:
            for axis, value, dur in seq:
                await self.execute(MotionCommand(axis=axis, value=value,
                                                 duration_ms=dur))
        elif gesture == Gesture.LOOK_AT and target:
            # 云台转向目标（需要标定，这里只记录）
            self.history.append((f"look_at:{target}", 0.0))
            if self.verbose:
                print(f"  [云台] 转向 {target}")

    async def emergency_stop(self) -> None:
        for a in Axis:
            self._state[a.value] = 0.0
        if self.verbose:
            logger.info("[动作] 紧急停止")

    # ------------------------------------------------------------------
    # 自动回充（与 WheeltecC30D 同签名，保证可替换）
    # ------------------------------------------------------------------

    async def dock_velocity(self, vx: float, vy: float, wz: float,
                            distance_m: float | None = None) -> None:
        """模拟回充对接的一帧速度（带同样的限幅，便于策略层单测）。"""
        import math
        from pipi.providers.motion.wheeltec import (
            DOCK_MAX_VX, DOCK_MAX_VY, DOCK_MAX_WZ, DOCK_SLOW_ZONE_M,
        )
        scale = 1.0
        if distance_m is not None and distance_m < DOCK_SLOW_ZONE_M:
            scale = max(0.4, distance_m / DOCK_SLOW_ZONE_M)
        vx = max(-DOCK_MAX_VX, min(DOCK_MAX_VX, vx)) * scale
        vy = max(-DOCK_MAX_VY, min(DOCK_MAX_VY, vy)) * scale
        wz = max(-DOCK_MAX_WZ, min(DOCK_MAX_WZ, wz)) * scale
        self._mock_twist = (vx, vy, wz)
        self.history.append(("dock_vel", math.hypot(vx, vy)))
        if self.verbose:
            logger.debug("[回充] vx=%.3f vy=%.3f wz=%.3f", vx, vy, wz)

    async def dock_stop(self) -> None:
        self._mock_twist = (0.0, 0.0, 0.0)
        self.history.append(("dock_stop", 0.0))

    async def charge_pose(self) -> None:
        await self.execute_gesture(Gesture.CHARGE_POSE)

    async def get_state(self) -> dict[str, float]:
        state = dict(self._state)
        if hasattr(self, "_mock_twist"):
            vx, vy, wz = self._mock_twist
            state.update({"dock_vx": vx, "dock_vy": vy, "dock_wz": wz})
        return state

    async def close(self) -> None:
        pass


_GESTURE_CN = {
    Gesture.NOD: "点头",
    Gesture.SHAKE_HEAD: "摇头",
    Gesture.LOOK_AT: "看向说话人",
    Gesture.LEAN_IN: "探身",
    Gesture.TURN_AWAY: "扭头",
    Gesture.PERK_UP: "竖起身子",
    Gesture.SLEEP: "垂头",
    Gesture.HAPPY_WIGGLE: "开心晃动",
    Gesture.FOLLOW_OWNER: "跟随主人",
    Gesture.SIDE_STEP: "侧身滑开",
    Gesture.RETURN_TO_DOCK: "自动回充（归桩）",
    Gesture.DOCK_ALIGN: "回充微调对齐",
    Gesture.CHARGE_POSE: "充电中姿态",
}


def _gesture_cn(g: Gesture) -> str:
    return _GESTURE_CN.get(g, g.value)
