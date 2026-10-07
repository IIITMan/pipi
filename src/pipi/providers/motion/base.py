"""运动控制 Provider 接口 + 语义动作库。

关键抽象：编排层只说"点头""看向说话人"，不关心舵机角度。
换硬件（舵机型号变了/加了底盘）只改 Provider，业务代码零改动。
"""

from __future__ import annotations

from typing import Protocol

from pipi.domain.motion import Axis, Gesture, MotionCommand


class MotionProvider(Protocol):
    """运动控制接口。"""

    name: str

    def is_available(self) -> bool: ...

    def is_simulated(self) -> bool: ...

    def supported_axes(self) -> set[Axis]: ...

    async def execute(self, cmd: MotionCommand) -> None: ...

    async def execute_gesture(self, gesture: str | Gesture,
                              target: tuple[int, int] | None = None) -> None: ...

    async def emergency_stop(self) -> None: ...

    async def get_state(self) -> dict[str, float]: ...

    async def close(self) -> None: ...
