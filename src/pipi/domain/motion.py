"""运动控制领域模型 —— 为将来部署到实体机器人（舵机/底盘/云台）预留。

当前是软件阶段，MotionProvider 用 mock 实现，
但接口和语义已经对齐真实硬件：主控（N100/N5015/RK3588）+ 轮趣 C30D 单板下位机，
头部舵机 / 表情灯带 / 麦克纳姆轮底盘全部由 C30D 驱动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Axis(str, Enum):
    """可控制的自由度。"""

    HEAD_PAN = "head_pan"          # 头部左右转（云台）
    HEAD_TILT = "head_tilt"        # 头部上下抬（云台）
    BASE_FORWARD = "base_forward"  # 底盘前进后退
    BASE_STRAFE = "base_strafe"    # 底盘横移（麦克纳姆轮专属自由度）
    BASE_ROTATE = "base_rotate"    # 底盘原地旋转
    LED = "led"                    # 灯效/表情灯
    SPEAKER_VOLUME = "speaker_volume"


class DockState(str, Enum):
    """自动回充（归桩）状态机。

    状态迁移：
        IDLE ──电量低/定时──▶ SEEK ──看到桩──▶ APPROACH
                                              │
                        ┌─────────────────────┘
                        ▼
                     ALIGN ──触点闭合──▶ CHARGING ──充满──▶ IDLE
                        │                  │
                    超时/丢失          被拔/离开
                        ▼                  ▼
                      FAIL ──重试──▶ SEEK            IDLE
    """

    IDLE = "idle"            # 未在回充流程中
    SEEK = "seek"            # 远程引导：找桩（视觉识别 AprilTag/QR）
    APPROACH = "approach"    # 接近：走直线缩短距离，保持航向
    ALIGN = "align"          # 近程对齐：红外引导，调中心偏差 + 角度偏差
    DOCKED = "docked"        # 触点已闭合，等待桩端确认
    CHARGING = "charging"    # 正在充电
    FAIL = "fail"            # 本次回充失败（超时/丢失目标）
    FAULT = "fault"          # 硬件异常（充电无电流、触点短路）


@dataclass
class DockStatus:
    """回充过程的实时状态（由 DockController 发布，UI/日志消费）。"""

    state: DockState = DockState.IDLE
    battery_pct: float = 1.0          # 0..1
    charging: bool = False
    charge_current_a: float = 0.0     # 桩端回传的充电电流
    lateral_error_mm: float = 0.0     # 横向中心偏差（红外解算）
    heading_error_deg: float = 0.0    # 航向偏差（红外/视觉解算）
    distance_mm: float = 0.0          # 到桩距离
    retries: int = 0
    last_error: str = ""


@dataclass
class MotionCommand:
    """一条底层运动指令。"""

    axis: Axis
    value: float                     # 归一化 −1..1，或绝对角度（由 provider 解释）
    duration_ms: int | None = None   # 动作时长，None = 立即到位
    blocking: bool = False           # 是否阻塞等待完成
    speed: float = 1.0               # 速度倍率


class Gesture(str, Enum):
    """高层语义动作 —— 编排层只说"点头"，不关心舵机角度。

    这层抽象的意义：换硬件时只改 MotionProvider，
    业务代码（什么时候点头）完全不动。
    """

    NOD = "nod"                    # 点头（认同/应答）
    SHAKE_HEAD = "shake_head"      # 摇头（否定/傲娇）
    LOOK_AT = "look_at"            # 云台转向说话人（需视觉坐标）
    LEAN_IN = "lean_in"            # 探身（粘人/感兴趣）
    TURN_AWAY = "turn_away"        # 扭头（生气/吃醋）
    PERK_UP = "perk_up"            # 竖起（听到唤醒词/被唤醒）
    SLEEP = "sleep"                # 垂头（休眠）
    HAPPY_WIGGLE = "happy_wiggle"  # 开心晃动
    FOLLOW_OWNER = "follow_owner"  # 底盘跟随主人
    SIDE_STEP = "side_step"        # 侧身滑开（傲娇/吃醋时的平移拉开距离）
    # —— 自动回充（详见 CHASSIS.md §12）
    RETURN_TO_DOCK = "return_to_dock"  # 整段归桩流程（由 DockController 驱动，
                                       # 不是固定序列，provider 只需接管）
    DOCK_ALIGN = "dock_align"          # 近程微调对齐（红外引导的三段闭环）
    CHARGE_POSE = "charge_pose"        # 已对接：垂头 + 呼吸灯（"充电中"表情）


# 语义动作 → 底层指令序列（由 GestureLibrary 消费）
GESTURE_SEQUENCES: dict[Gesture, list[tuple[Axis, float, int]]] = {
    Gesture.NOD: [
        (Axis.HEAD_TILT, -0.3, 220),
        (Axis.HEAD_TILT, 0.15, 220),
        (Axis.HEAD_TILT, 0.0, 180),
    ],
    Gesture.SHAKE_HEAD: [
        (Axis.HEAD_PAN, -0.35, 260),
        (Axis.HEAD_PAN, 0.35, 380),
        (Axis.HEAD_PAN, 0.0, 220),
    ],
    Gesture.PERK_UP: [
        (Axis.HEAD_TILT, 0.35, 180),
        (Axis.HEAD_TILT, 0.1, 200),
    ],
    Gesture.TURN_AWAY: [
        (Axis.HEAD_PAN, 0.6, 400),
        (Axis.HEAD_TILT, -0.15, 300),
    ],
    Gesture.LEAN_IN: [
        (Axis.BASE_FORWARD, 0.3, 500),
    ],
    Gesture.SLEEP: [
        (Axis.HEAD_TILT, -0.5, 900),
        (Axis.LED, 0.1, 500),
    ],
    Gesture.HAPPY_WIGGLE: [
        (Axis.HEAD_PAN, -0.25, 160),
        (Axis.HEAD_PAN, 0.25, 320),
        (Axis.HEAD_PAN, -0.15, 260),
        (Axis.HEAD_PAN, 0.0, 160),
    ],
    # 麦克纳姆轮专属：不开底盘时（无 C30D），
    # GestureLibrary 应跳过 BASE_* 步骤而不是报错。
    Gesture.SIDE_STEP: [
        (Axis.BASE_STRAFE, 0.4, 600),
        (Axis.BASE_STRAFE, 0.0, 200),
    ],
    # —— 自动回充：只有"对接成功后的姿态"是固定序列，
    #    真正的归桩运动由 DockController 逐拍发 Twist，不走这里。
    Gesture.CHARGE_POSE: [
        (Axis.HEAD_TILT, -0.35, 900),   # 垂头"眯一会儿"
        (Axis.LED, 0.25, 600),          # 低亮度呼吸灯
        (Axis.BASE_FORWARD, 0.0, 100),  # 确保底盘停住
    ],
}


@dataclass
class GestureRequest:
    """一次语义动作请求。"""

    gesture: Gesture
    target_bbox: tuple[int, int] | None = None   # LOOK_AT 用：目标像素坐标
    intensity: float = 1.0
    reason: str = ""                              # 调试：为什么做这个动作
    extra: dict = field(default_factory=dict)
