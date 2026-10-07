"""模拟摄像头（单独模块，便于 import）。"""

from pipi.hardware.sim import SimCamera, SimMCUBridge, SimMic, SimSpeaker

__all__ = ["SimCamera", "SimMic", "SimSpeaker", "SimMCUBridge"]
