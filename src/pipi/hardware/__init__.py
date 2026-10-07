"""硬件抽象层。

当前全部走模拟实现（sim/），RK3588/RK3576 真实驱动在 rockchip/ 预留。
"""

from pipi.hardware.base import CameraDriver, MCUBridge, MicDriver, SpeakerDriver

__all__ = ["CameraDriver", "MicDriver", "SpeakerDriver", "MCUBridge"]
