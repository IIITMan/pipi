"""RK3588 / RK3576 板卡真实驱动（预留）。

硬件到位后在这里实现：
  - USB UVC 摄像头采集（OpenCV / V4L2）
  - ALSA 麦克风阵列（含 VAD、可选 DOA 声源定位）
  - ALSA 扬声器播放

替换方式：修改 configs/default.yaml 中对应 provider 名称即可，
业务代码无需改动 —— 这就是硬件抽象层的价值。

注意：运动控制**不在这一层**。头部舵机 / 灯带 / 麦轮底盘全部由
轮趣 C30D 单板下位机通过 ROS2 话题驱动，见 providers/motion/wheeltec.py。

选型与接线方案见仓库根目录的 HARDWARE.md。

实现注意：
  1. is_available() 必须真检测设备节点（/dev/video0），不能返回 True
     —— 降级链靠它判断。
  2. 所有阻塞 IO（OpenCV cap.read()、ALSA read）必须用
     asyncio.to_thread() 包装，否则会卡死事件循环、导致主动引擎失效。
  3. WS2812 灯带与舵机 PWM 一律交给 C30D，不要在 Linux 用户态直接驱动。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__: list[str] = []

# TODO(硬件阶段): 实现 USBCameraDriver（USB UVC 免驱，OpenCV 直读）
# TODO(硬件阶段): 实现 AlsaMicDriver（麦克风阵列，含 VAD）
# TODO(硬件阶段): 实现 AlsaSpeakerDriver（aplay / sounddevice）
# 运动控制见 providers/motion/wheeltec.py（ROS2 → C30D），无需串口桥
