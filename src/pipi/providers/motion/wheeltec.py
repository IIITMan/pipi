"""轮趣 C30D —— 全机唯一的运动控制下位机。

C30D 是一块 STM32F407VET6 控制板（12V 输入，板载 5V/5A 输出、多路舵机
接口、4 路编码器电机接口、USB/CAN 通信）。皮皮把**头部舵机、表情灯带、
麦克纳姆轮底盘**全部交给它，不再需要第二块 MCU —— 一块板子管全机，
少一条 UART 线、少一个需要烧固件的设备、少一处时序抖动来源。

数据流（部署链路）：

  ┌ 皮皮主程序（本模块）────────────────────────────────────┐
  │                                                          │
  │  头部/灯（角度语义）                                      │
  │    → 发布 std_msgs/Float32MultiArray 到 /pipi/head       │
  │    → micro-ROS agent → C30D（自写 micro-ROS app）         │
  │    → 板载 50Hz 舵机 PWM + WS2812                          │
  │                                                          │
  │  底盘（速度语义）                                         │
  │    → 发布 geometry_msgs/Twist 到 /cmd_vel                 │
  │    → micro-ROS agent → C30D                               │
  │    → 板载麦轮运动学解算 + 编码器 PID → 4× 编码器电机       │
  │                                                          │
  │  回程：C30D → /odom（nav_msgs/Odometry）→ agent → 本模块   │
  └──────────────────────────────────────────────────────────┘

为什么走 ROS2 而不是直接打串口：
  · C30D 官方生态就是 ROS/ROS2 下位机，micro-ROS 固件里已实现麦轮解算、
    PID、看门狗；上位机只表达"想要的速度/角度"。
  · ROS2 DDS 自带发现与 QoS，后续加雷达（SLAM）、手柄遥控、导航栈
    都只是在话题图里加节点，皮皮代码不用动。
  · 串口私有协议会被固件升级打破；话题接口是稳定契约。

为什么不再需要第二块 MCU（原 ESP32-S3 方案已移除）：
  · 舵机 PWM、WS2812 时序、电机 PID 全部由 C30D 的 MCU 产生，
    Linux 用户态的调度抖动传不下去，实时性要求同样被满足。
  · 单板方案少一路供电、少一处固件版本对不齐的风险。

rclpy 依赖是可选的：
  · 装了 ROS2（Humble/Jazzy）→ 真实驱动
  · 没装 → is_available() 返回 False，降级链自动回落 Mock
  · dry_run=True → 全程模拟（单测/演示），发布过的指令有记录可查

对应轴（全机六个自由度都在这一个 provider 里）：
  HEAD_PAN / HEAD_TILT / LED → /pipi/head
  BASE_FORWARD → Twist.linear.x
  BASE_STRAFE  → Twist.linear.y（麦轮专属：横向平移）
  BASE_ROTATE  → Twist.angular.z

自动回充（详见 CHASSIS.md §12）：
  麦轮底盘的回充是一份"礼物"—— 差速底盘只有 (vx, wz) 两个自由度，
  要把车体精确对进桩里必须做"S 形倒车 + 反复修正"；麦轮有 (vx, vy, wz)
  三个正交自由度，可以把对接**解耦成三个独立的单变量闭环**：

      ① 缩距    → linear.x     （沿桩法线靠近）
      ② 调心偏  → linear.y     （纯横向平移，不动航向）
      ③ 调角偏  → angular.z    （原地旋转，不动位置）

  三段互不耦合，每段都能单独调 P 增益、单独设死区，控制律从
  "2 输入 3 输出耦合"降到三个 SISO。**C30D 固件一行都不用改**，
  因为这三路本来就是 /cmd_vel 自带的标准字段。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field

from pipi.domain.motion import Axis, Gesture, GESTURE_SEQUENCES, MotionCommand

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 关节索引
# 头部话题的数组布局，固件侧必须与此一致（详见 HARDWARE.md §9 映射表）
HEAD_JOINTS: tuple[str, ...] = ("head_pan", "head_tilt", "led")
JOINT_INDEX: dict[str, int] = {name: i for i, name in enumerate(HEAD_JOINTS)}

# ---------------------------------------------------------------- 物理上限
MAX_VX_MPS = 0.5     # 最大前后速度 m/s
MAX_VY_MPS = 0.4     # 最大横移速度 m/s（麦轮横移通常略慢）
MAX_WZ_RAD = 1.5     # 最大角速度 rad/s
WATCHDOG_S = 0.5     # 与固件看门狗一致的无指令停车时限

# 头部是绝对位置语义：发了指令就停在那个位置，不会"一直转过去"，
# 所以头部不需要软件看门狗，只需固件侧的失联保护。
HEAD_PAN_DEG = 120.0  # 左右舵机行程 ±120°（与 configs/default.yaml 一致）
HEAD_TILT_DEG = 45.0  # 俯仰舵机行程 ±45°
HEAD_PWM_HZ = 50      # 舵机 PWM 频率（固件侧使用）

# ------------------------------------------------------- 回充对接的物理限幅
# 对接阶段必须"慢"：轮速越高，麦轮辊子的打滑越不可控，且红外的刷新率
# 只有几十 Hz，快了会过冲振荡。这三个值是回充专用的**硬上限**，
# 与正常行驶的 MAX_* 分开，避免"回充时忘了降速"。
DOCK_MAX_VX = 0.15      # m/s，缩距段上限
DOCK_MAX_VY = 0.06      # m/s，调心偏段上限（横移更慢，几乎无过冲）
DOCK_MAX_WZ = 0.35      # rad/s，调角偏段上限
DOCK_SLOW_ZONE_M = 0.25  # 距桩 250mm 内再降速到 40%（"最后一米"降速）
DOCK_ALIGN_TIMEOUT_S = 25.0   # 对齐段超时（超过就判 FAIL 重试）
DOCK_TOUCH_SETTLE_S = 1.2     # 触点闭合后的稳定等待（防抖）


@dataclass
class TwistCmd:
    """与 rclpy 解耦的速度指令（便于单测与记录）。"""

    vx: float = 0.0    # m/s，+ 前进
    vy: float = 0.0    # m/s，+ 左移
    wz: float = 0.0    # rad/s，+ 逆时针


@dataclass
class HeadCmd:
    """与 rclpy 解耦的头部指令（角度语义）。

    pan/tilt 单位为度；led 为 0..1 亮度。任一为 None 表示"这一路不变"。
    """

    pan: float | None = None
    tilt: float | None = None
    led: float | None = None

    def is_empty(self) -> bool:
        return self.pan is None and self.tilt is None and self.led is None


@dataclass
class _State:
    """一路的当前状态。"""

    pan_deg: float = 0.0
    tilt_deg: float = 0.0
    led: float = 0.0
    forward: float = 0.0
    strafe: float = 0.0
    rotate: float = 0.0
    odom: dict[str, float] = field(default_factory=dict)


class WheeltecC30D:
    """轮趣 C30D：一块板子同时驱动头部舵机/灯带与麦轮底盘。"""

    name = "wheeltec_c30d"

    def __init__(self, topic_cmd: str = "/cmd_vel",
                 topic_head: str = "/pipi/head",
                 topic_odom: str = "/odom",
                 node_name: str = "pipi_base",
                 max_pan_deg: float = HEAD_PAN_DEG,
                 max_tilt_deg: float = HEAD_TILT_DEG,
                 dry_run: bool = False):
        self.topic_cmd = topic_cmd
        self.topic_head = topic_head
        self.topic_odom = topic_odom
        self.node_name = node_name
        self.max_pan_deg = max_pan_deg
        self.max_tilt_deg = max_tilt_deg
        self.dry_run = dry_run
        self._lock = asyncio.Lock()
        self._state = _State()
        self._zero_task: asyncio.Task | None = None
        # ROS2 运行时（lazy 初始化）
        self._node = None
        self._pub_cmd = None
        self._pub_head = None
        self._spin_thread: threading.Thread | None = None
        # dry-run 记录：发布过的指令序列（测试可断言）
        self.published: list[TwistCmd] = []
        self.published_head: list[HeadCmd] = []

    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        if self.dry_run:
            return True
        try:
            import rclpy  # noqa: F401
        except ImportError:
            logger.debug("未检测到 ROS2（rclpy），C30D 不可用，走 Mock")
            return False
        return True

    def is_simulated(self) -> bool:
        return self.dry_run

    def supported_axes(self) -> set[Axis]:
        """单板方案：全机六个自由度都由 C30D 承接。"""
        return {
            Axis.HEAD_PAN, Axis.HEAD_TILT, Axis.LED,
            Axis.BASE_FORWARD, Axis.BASE_STRAFE, Axis.BASE_ROTATE,
        }

    # ------------------------------------------------------------------
    # ROS2 运行时

    def _ensure_node(self) -> None:
        """lazy 创建 ROS2 节点 + 两个 publisher + odom 订阅 + spin 线程。"""
        if self._node is not None:
            return
        import rclpy
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry
        from std_msgs.msg import Float32MultiArray

        if not rclpy.ok():
            rclpy.init(args=None)
        self._node = rclpy.create_node(self.node_name)
        self._pub_cmd = self._node.create_publisher(Twist, self.topic_cmd, 10)
        self._pub_head = self._node.create_publisher(
            Float32MultiArray, self.topic_head, 10)
        self._node.create_subscription(Odometry, self.topic_odom,
                                       self._on_odom, 10)

        def _spin() -> None:
            try:
                rclpy.spin(self._node)
            except Exception:
                pass

        self._spin_thread = threading.Thread(target=_spin, daemon=True,
                                             name="pipi-c30d-spin")
        self._spin_thread.start()
        logger.info("C30D ROS2 节点已启动：cmd=%s head=%s odom=%s",
                    self.topic_cmd, self.topic_head, self.topic_odom)

    def _on_odom(self, msg) -> None:
        """里程计回调（spin 线程里执行，只更新内存，无阻塞）。"""
        p = msg.pose.pose.position
        t = msg.twist.twist
        self._state.odom = {
            "odom_x": p.x, "odom_y": p.y,
            "odom_v_lin": t.linear.x, "odom_v_ang": t.angular.z,
        }

    # ------------------------------------------------------------------
    # 发布

    def _publish_twist(self, twist: TwistCmd) -> None:
        """发布底盘速度。dry-run 只记录，真实模式转 geometry_msgs.Twist。"""
        self.published.append(twist)
        if self.dry_run:
            logger.debug("[C30D dry-run] %s vx=%.2f vy=%.2f wz=%.2f",
                         self.topic_cmd, twist.vx, twist.vy, twist.wz)
            return
        from geometry_msgs.msg import Twist
        self._ensure_node()
        msg = Twist()
        msg.linear.x = twist.vx
        msg.linear.y = twist.vy
        msg.angular.z = twist.wz
        self._pub_cmd.publish(msg)

    def _publish_head(self, head: HeadCmd) -> None:
        """发布头部关节角。dry-run 只记录，真实模式转 Float32MultiArray。"""
        self.published_head.append(head)
        if self.dry_run:
            logger.debug("[C30D dry-run] %s pan=%s tilt=%s led=%s",
                         self.topic_head, head.pan, head.tilt, head.led)
            return
        from std_msgs.msg import Float32MultiArray
        self._ensure_node()
        msg = Float32MultiArray()
        msg.data = [
            float(head.pan if head.pan is not None else self._state.pan_deg),
            float(head.tilt if head.tilt is not None else self._state.tilt_deg),
            float(head.led if head.led is not None else self._state.led),
        ]
        self._pub_head.publish(msg)

    def _current_twist(self) -> TwistCmd:
        s = self._state
        return TwistCmd(vx=s.forward * MAX_VX_MPS,
                        vy=s.strafe * MAX_VY_MPS,
                        wz=s.rotate * MAX_WZ_RAD)

    # ------------------------------------------------------------------

    async def execute(self, cmd: MotionCommand) -> None:
        """按轴分发。底盘是速度档（−1..1），头部是归一化角度（−1..1）。"""
        if cmd.axis in (Axis.HEAD_PAN, Axis.HEAD_TILT, Axis.LED):
            await self._execute_head(cmd)
            return
        await self._execute_base(cmd)

    async def _execute_base(self, cmd: MotionCommand) -> None:
        key = {Axis.BASE_FORWARD: "forward",
               Axis.BASE_STRAFE: "strafe",
               Axis.BASE_ROTATE: "rotate"}.get(cmd.axis)
        if key is None:
            return
        setattr(self._state, key, max(-1.0, min(1.0, cmd.value)))

        async with self._lock:
            await asyncio.to_thread(self._publish_twist, self._current_twist())

        # 底盘是速度语义：不复位会一直开走 → 看门狗到期自动停车
        if cmd.duration_ms:
            self._schedule_zero()

    async def _execute_head(self, cmd: MotionCommand) -> None:
        v = max(-1.0, min(1.0, cmd.value))
        head = HeadCmd()
        if cmd.axis is Axis.HEAD_PAN:
            self._state.pan_deg = v * self.max_pan_deg
            head.pan = self._state.pan_deg
        elif cmd.axis is Axis.HEAD_TILT:
            self._state.tilt_deg = v * self.max_tilt_deg
            head.tilt = self._state.tilt_deg
        else:  # Axis.LED
            self._state.led = max(0.0, v)
            head.led = self._state.led
        async with self._lock:
            await asyncio.to_thread(self._publish_head, head)

    def _schedule_zero(self) -> None:
        if self._zero_task and not self._zero_task.done():
            self._zero_task.cancel()
        self._zero_task = asyncio.create_task(self._zero_after_watchdog())

    async def _zero_after_watchdog(self) -> None:
        await asyncio.sleep(WATCHDOG_S)
        s = self._state
        if s.forward or s.strafe or s.rotate:
            s.forward = s.strafe = s.rotate = 0.0
            async with self._lock:
                await asyncio.to_thread(self._publish_twist, self._current_twist())
            logger.debug("C30D 看门狗：底盘速度归零")

    async def execute_gesture(self, gesture: str | Gesture,
                              target: tuple[int, int] | None = None) -> None:
        """逐条执行语义动作序列 —— 单板方案下整条序列都是自己消化。"""
        if isinstance(gesture, str):
            try:
                gesture = Gesture(gesture)
            except ValueError:
                return

        if gesture == Gesture.LOOK_AT and target:
            await self.look_at(target)
            return

        # 回充是"闭环控制流程"而非"固定动作序列"——它需要感知反馈，
        # provider 无权自行推进（否则会变成盲开）。这里只打印一条警告，
        # 真正的推进由 DockController 调 dock_velocity() 逐拍驱动。
        if gesture in (Gesture.RETURN_TO_DOCK, Gesture.DOCK_ALIGN):
            logger.warning(
                "%s 是闭环流程，需由 DockController 驱动 dock_velocity()，"
                "execute_gesture 不代为执行", gesture.value)
            return

        for axis, value, dur in GESTURE_SEQUENCES.get(gesture, []):
            await self.execute(MotionCommand(axis=axis, value=value,
                                             duration_ms=dur if dur else None))

    async def look_at(self, target: tuple[int, int]) -> None:
        """像素坐标 → 云台角度。留出标定系数，实车用 /pipi/head 微调。

        约定：图像宽度中点为"正前方"，越靠右越往右转。
        水平视场角按 60° 估算（1080P 无畸变 UVC + 常见镜头），
        实车可用 `scripts/` 下的小工具标定后覆盖此系数。
        """
        import math
        fov_h_deg = 60.0
        # 目标像素 → 归一化偏移（−1..1）；这里只拿 x 分量驱动 pan
        x_norm = max(-1.0, min(1.0, (target[0] - 320.0) / 320.0))
        pan_deg = x_norm * fov_h_deg * 0.5
        pan_norm = max(-1.0, min(1.0, pan_deg / max(1e-6, self.max_pan_deg)))
        await self._execute_head(MotionCommand(axis=Axis.HEAD_PAN,
                                               value=pan_norm))
        # y 分量同理驱动 tilt（图像 y 向下为正 → 取反）
        y_norm = max(-1.0, min(1.0, (320.0 - target[1]) / 320.0))
        tilt_norm = y_norm * math.sin(math.radians(fov_h_deg * 0.25))
        await self._execute_head(MotionCommand(axis=Axis.HEAD_TILT,
                                               value=tilt_norm))

    # ------------------------------------------------------------------
    # 自动回充：麦轮三段式对接（详见 CHASSIS.md §12）
    #
    # 为什么这是麦轮的主场：
    #   差速底盘只有 (vx, wz)，把车体对进桩要同时管"离多远"和"歪多少"，
    #   两者耦合 —— 想转正就会偏离，想靠近就会撞歪。于是工程上要么
    #   倒 S 形曲线（轨迹规划复杂），要么反复"前进-修正-后退"。
    #   麦轮多一个 vy，可以 ①缩距 ②纯平移调心偏 ③纯旋转调角偏，
    #   三个动作**互不干扰**，各自一个 P 控制器 + 死区就收敛。
    #
    # 本方法只做"发一帧速度"，不做闭环 —— 闭环在 DockController 里，
    # 因为它需要红外/视觉的观测量，那是感知层的事。这样切分的好处：
    # provider 保持"哑执行器"角色，回充策略可以单独测试（喂假观测）。
    # ------------------------------------------------------------------

    async def dock_velocity(self, vx: float, vy: float, wz: float,
                            distance_m: float | None = None) -> None:
        """回充专用限速发布。所有回充运动必须走这里，不要直接 execute()。

        Args:
            vx: 期望前后速度（+ 朝桩前进），会被 DOCK_MAX_VX 夹住
            vy: 期望横移速度（+ 左移），会被 DOCK_MAX_VY 夹住
            wz: 期望角速度（+ 逆时针），会被 DOCK_MAX_WZ 夹住
            distance_m: 距桩距离；传入则启用"最后一米降速"（更稳）
        """
        scale = 1.0
        if distance_m is not None and distance_m < DOCK_SLOW_ZONE_M:
            # 进慢区线性降到 40%，避免撞桩
            scale = max(0.4, distance_m / DOCK_SLOW_ZONE_M)

        vx = max(-DOCK_MAX_VX, min(DOCK_MAX_VX, vx)) * scale
        vy = max(-DOCK_MAX_VY, min(DOCK_MAX_VY, vy)) * scale
        wz = max(-DOCK_MAX_WZ, min(DOCK_MAX_WZ, wz)) * scale

        s = self._state
        s.forward = vx / MAX_VX_MPS
        s.strafe = vy / MAX_VY_MPS
        s.rotate = wz / MAX_WZ_RAD
        async with self._lock:
            await asyncio.to_thread(self._publish_twist,
                                    TwistCmd(vx=vx, vy=vy, wz=wz))

    async def dock_stop(self) -> None:
        """对接停车：直接发全零（不等看门狗，回充要停得干脆）。"""
        s = self._state
        s.forward = s.strafe = s.rotate = 0.0
        async with self._lock:
            await asyncio.to_thread(self._publish_twist, TwistCmd())

    async def charge_pose(self) -> None:
        """已对接：垂头 + 低亮呼吸灯的"充电中"姿态。"""
        await self.execute_gesture(Gesture.CHARGE_POSE)

    async def emergency_stop(self) -> None:
        """急停：底盘归零 + 灯灭。头部保持当前角度（突然垂头会砸到人）。"""
        s = self._state
        s.forward = s.strafe = s.rotate = 0.0
        s.led = 0.0
        try:
            async with self._lock:
                await asyncio.to_thread(self._publish_twist, TwistCmd())
                await asyncio.to_thread(self._publish_head, HeadCmd(led=0.0))
        except Exception as exc:
            logger.error("C30D 紧急停止失败：%s", exc)

    async def get_state(self) -> dict[str, float]:
        s = self._state
        state = {
            "head_pan": s.pan_deg, "head_tilt": s.tilt_deg, "led": s.led,
            "base_forward": s.forward, "base_strafe": s.strafe,
            "base_rotate": s.rotate,
        }
        state.update(s.odom)
        return state

    async def close(self) -> None:
        await self.emergency_stop()
        if self._zero_task and not self._zero_task.done():
            self._zero_task.cancel()
        if self._node is not None:
            try:
                self._node.destroy_node()
                import rclpy
                if rclpy.ok():
                    rclpy.shutdown()
            except Exception:
                pass
            self._node = None
            self._pub_cmd = None
            self._pub_head = None
