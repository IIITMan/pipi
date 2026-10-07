"""硬件抽象层 —— 摄像头/麦克风/扬声器/C30D 桥。

当前全模拟，但接口已对齐真实架构：
  主控 N100/N5015/RK3588（Linux）—— USB 摄像头、麦克风阵列、扬声器、跑 Python
  轮趣 C30D（单板下位机）—— 头部舵机、WS2812 灯带、麦克纳姆轮电机 + 编码器，
                          经 ROS2 话题通信（见 providers/motion/wheeltec.py）
"""

from __future__ import annotations

from typing import Any, Protocol


class CameraDriver(Protocol):
    """摄像头驱动。"""

    def is_available(self) -> bool: ...
    async def open(self) -> None: ...
    async def read(self) -> Any: ...
    async def close(self) -> None: ...


class MicDriver(Protocol):
    """麦克风驱动。"""

    def is_available(self) -> bool: ...
    async def start(self) -> None: ...
    async def read_chunk(self) -> bytes: ...
    async def stop(self) -> None: ...


class SpeakerDriver(Protocol):
    """扬声器驱动。"""

    def is_available(self) -> bool: ...
    async def play(self, audio: bytes, sample_rate: int) -> None: ...
    async def stop(self) -> None: ...


class MCUBridge(Protocol):
    """下位机（C30D）通信桥。

    实机走 ROS2 话题，不需要串口桥：
      底盘 → geometry_msgs/Twist           发到 /cmd_vel
      头部 → std_msgs/Float32MultiArray    发到 /pipi/head
      回传 → nav_msgs/Odometry             订阅 /odom

    这个 Protocol 只保留给"需要同步问答"的场合（如固件版本查询、
    标定参数读写）；常规运动指令请直接用 pipi.providers.motion.wheeltec。
    """

    def is_available(self) -> bool: ...
    async def send(self, payload: dict) -> dict: ...
    async def query_state(self) -> dict: ...
    async def close(self) -> None: ...
