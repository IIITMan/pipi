"""自动回充编排器 —— 决定"什么时候去充、怎么对进去、什么时候算成功"。

对应 `CHASSIS.md` §12「自动回充与充电桩」。

设计要点（为什么是麦轮的主场）：

  差速底盘只有 (vx, wz) 两个自由度，要把车体精确"倒"进桩里，
  必须同时处理"离多远"和"歪多少"，两者耦合 —— 想转正就会偏离，
  想靠近就会撞歪。工程上要么规划 S 形曲线，要么反复
  "前进-修正-后退"，慢且丑。

  麦轮有 (vx, vy, wz) **三个正交自由度**，可以把对接解耦成
  三个独立的单变量闭环：

      ① 缩距    vx ← kp_x · 距离偏差      （沿桩法线靠近）
      ② 调心偏  vy ← kp_y · 横向偏差      （纯平移，不动航向）
      ③ 调角偏  wz ← kp_z · 航向偏差      （纯旋转，不动位置）

  三段互不干扰 → 控制律从"2 输入耦合"降到三个 SISO，各配一个
  P 控制器 + 死区即可收敛，**不需要 MPC / 轨迹规划**。
  而且这三路都是 `/cmd_vel` 自带字段，**C30D 固件一行不用改**。

分层：
    DockController（本模块）= 策略 + 状态机，纯逻辑，可单测
    MotionProvider.dock_velocity() = 哑执行器（带限幅）
    感知层 = 提供红外/视觉观测量

  这样切分的好处：喂一组假观测就能完整测出状态机行为，
  不需要真车、不需要 ROS2、不需要摄像头。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pipi.domain.motion import DockState, DockStatus

logger = logging.getLogger(__name__)


@dataclass
class DockConfig:
    """回充参数（由 configs/default.yaml 的 charging 段注入）。"""

    # 触发
    battery_low_pct: float = 0.25
    idle_minutes: float = 20.0
    scheduled_time: str = "23:30"
    respect_quiet_hours: bool = True

    # 引导
    far_max_range_m: float = 3.0
    near_enter_range_m: float = 0.35
    tag_id: int = 0

    # 三段式控制器
    vx_kp: float = 0.80
    vy_kp: float = 1.20
    wz_kp: float = 2.00
    lateral_deadband_mm: float = 8.0
    heading_deadband_deg: float = 3.0
    distance_deadband_mm: float = 12.0
    timeout_s: float = 25.0
    max_retries: int = 3

    # 桩端电气
    contact_full_s: float = 3.0
    no_current_fault_s: float = 10.0

    # 安全
    abort_on_human_near: bool = True
    human_near_m: float = 0.8


@dataclass
class DockObservation:
    """一帧感知观测（由感知层填充，控制器只读）。"""

    distance_m: float | None = None       # 到桩距离；None = 未看到桩
    lateral_mm: float = 0.0               # 横向中心偏差（红外解算，+ 表车在桩右侧）
    heading_deg: float = 0.0              # 航向偏差（+ 表车头偏向左侧）
    tag_visible: bool = False             # 远程视觉是否看到桩
    contact_closed: bool = False          # 触点是否已闭合
    charge_current_a: float = 0.0         # 桩端回传充电电流
    human_distance_m: float | None = None  # 最近的人有多远


@dataclass
class DockOutput:
    """一帧控制输出。"""

    vx: float = 0.0
    vy: float = 0.0
    wz: float = 0.0
    state: DockState = DockState.IDLE
    stop: bool = False      # True 表示"这一拍不要动"
    say: str = ""           # 需要皮皮说的话（低电提醒等）


class DockController:
    """自动回充状态机 + 三段式 P 控制器。纯逻辑，无 IO。"""

    def __init__(self, cfg: DockConfig | None = None):
        self.cfg = cfg or DockConfig()
        self.status = DockStatus()
        self._elapsed = 0.0          # 当前阶段已耗时（秒）
        self._contact_timer = 0.0    # 触点闭合持续时间
        self._no_current_timer = 0.0
        self._retry_cooldown = 0.0   # 失败后的重试冷却（见 _fail 注释）

    # ------------------------------------------------------------------

    def should_start(self, battery_pct: float, idle_minutes: float,
                     now_hm: str = "", quiet: bool = False) -> bool:
        """该不该发起回充？三条件取或，并遵守静默期。"""
        if self.status.state not in (DockState.IDLE, DockState.CHARGING):
            return False
        if self.status.charging:
            return False
        if battery_pct <= self.cfg.battery_low_pct:
            return True
        if idle_minutes >= self.cfg.idle_minutes:
            return True
        if now_hm and now_hm == self.cfg.scheduled_time:
            return True
        return False

    def low_battery_speech(self, battery_pct: float) -> str:
        """低电提醒话术 —— 陪伴机器人的核心是"先说一声再去"，
        而不是默默消失。用户感知里，"默默走开"等于"坏了"。"""
        pct = int(round(battery_pct * 100))
        if pct <= 10:
            return f"喂，我快没电了（{pct}%），我先回去充一会儿，马上回来找你。"
        if pct <= 20:
            return f"我电量只剩 {pct}% 了，去充个电，别趁我不在偷吃零食啊。"
        return f"电量 {pct}% 了，我顺路去补个电，很快。"

    # ------------------------------------------------------------------

    def step(self, obs: DockObservation) -> DockOutput:
        """推进一拍状态机。感知层每拍调一次（建议 10~20 Hz）。"""
        cfg = self.cfg
        st = self.status
        s = st.state

        # —— 全局安全：有人靠近就停（陪伴机器人撞人比充不上电严重得多）
        if (cfg.abort_on_human_near and obs.human_distance_m is not None
                and obs.human_distance_m < cfg.human_near_m
                and s in (DockState.SEEK, DockState.APPROACH, DockState.ALIGN)):
            logger.info("回充暂停：有人距 %.2fm", obs.human_distance_m)
            return DockOutput(state=s, stop=True)

        if s == DockState.IDLE:
            return DockOutput(state=s, stop=True)

        if s == DockState.SEEK:
            # 重试冷却期内：原地静默，等感知刷新（防"用旧观测立刻复现同一失败"）。
            # 踩过的坑：失败后直接回 SEEK，而观测里的 distance 还是旧的，
            # 于是 SEEK→APPROACH→ALIGN 一拍走完，repeats 永远累加不到上限。
            if self._retry_cooldown > 0:
                self._retry_cooldown -= 1.0
                return DockOutput(state=s, stop=True)
            # 远程引导：视觉看到 tag → 转 APPROACH；看不到 → 靠里程计摸（这里先保守等待）
            if obs.tag_visible or obs.distance_m is not None:
                st.state = DockState.APPROACH
                self._elapsed = 0.0
                return DockOutput(state=st.state)
            self._elapsed += 1.0
            if self._elapsed > cfg.timeout_s:
                return self._fail("远程找不到桩")
            return DockOutput(state=s, stop=True)

        if s == DockState.APPROACH:
            d = obs.distance_m
            if d is None:
                # 视觉丢失 → 退回 SEEK 重新找
                st.state = DockState.SEEK
                self._elapsed = 0.0
                return DockOutput(state=st.state, stop=True)
            if d <= cfg.near_enter_range_m:
                # 进入红外区 → 转 ALIGN，切换引导源
                st.state = DockState.ALIGN
                self._elapsed = 0.0
                logger.info("进入红外引导区（%.2fm）", d)
                return DockOutput(state=st.state, stop=True)
            # 缩距：只有 vx，不用 vy/wz（远程段不纠偏，速度优先）
            vx = min(cfg.vx_kp * d, 0.15)
            self._elapsed += 1.0
            if self._elapsed > cfg.timeout_s:
                return self._fail("接近超时")
            return DockOutput(vx=vx, state=s)

        if s == DockState.ALIGN:
            return self._align(obs)

        if s in (DockState.DOCKED, DockState.CHARGING):
            return self._charging(obs)

        if s in (DockState.FAIL, DockState.FAULT):
            return DockOutput(state=s, stop=True)

        return DockOutput(state=s, stop=True)

    # ------------------------------------------------------------------

    def _align(self, obs: DockObservation) -> DockOutput:
        """三段式对接的核心：三个自由度各自闭环，互不干扰。

        这一段是麦轮底盘相对差速底盘最明显的优势所在。

        增益设计要点（踩过坑）：
          P 控制在**死区附近必须降增益**，否则会 overshoot 到死区另一侧、
          来回振荡形成极限环（第一版 `wz_kp=2.0` 配 8° 偏差一步就转过 16°，
          直接冲到 −8°，对称振荡永不收敛）。
          这里的做法是"线性软着陆"：
              |e| < 死区×3  →  增益按 |e|/(死区×3) 线性压到 1/3 以下
              |e| ≥ 死区×3  →  全增益
          等价于一个"近场柔化"的 P 控制器，不需要引入 I/D 项就能收敛。
        """
        cfg = self.cfg
        st = self.status
        self._elapsed += 1.0

        st.lateral_error_mm = obs.lateral_mm
        st.heading_error_deg = obs.heading_deg
        st.distance_mm = (obs.distance_m or 0.0) * 1000

        heading_ok = abs(obs.heading_deg) <= cfg.heading_deadband_deg
        lateral_ok = abs(obs.lateral_mm) <= cfg.lateral_deadband_mm

        def soft_gain(err: float, deadband: float) -> float:
            """死区附近线性压增益，防极限环。返回 0~1 的系数。"""
            if deadband <= 0:
                return 1.0
            r = abs(err) / (deadband * 3.0)
            return min(1.0, max(0.15, r))

        # ① 缩距（vx）：只在"已经对正"的前提下才前进，避免歪着撞桩
        vx = 0.0
        if (obs.distance_m is not None
                and obs.distance_m * 1000 > cfg.distance_deadband_mm
                and heading_ok and lateral_ok):
            vx = min(cfg.vx_kp * obs.distance_m, 0.15)

        # ② 调心偏（vy）：横向偏差 mm → m；+ 表车在桩右侧 → 往左移
        vy = 0.0
        if abs(obs.lateral_mm) > cfg.lateral_deadband_mm:
            vy = (cfg.vy_kp * soft_gain(obs.lateral_mm,
                                        cfg.lateral_deadband_mm)
                  * (obs.lateral_mm / 1000.0))
            vy = max(-0.06, min(0.06, vy))      # 与 provider 限幅一致

        # ③ 调角偏（wz）：纯旋转
        wz = 0.0
        if abs(obs.heading_deg) > cfg.heading_deadband_deg:
            import math
            wz = (cfg.wz_kp * soft_gain(obs.heading_deg,
                                        cfg.heading_deadband_deg)
                  * math.radians(obs.heading_deg))
            wz = max(-0.35, min(0.35, wz))      # 与 provider 限幅一致

        # 距桩很近时**降速爬行**（而不是锁死）。
        # 踩过的坑：早先写成"< 60mm 直接 vx=0"，但到位死区只有 12mm，
        # 于是车停在 60mm 处永远不满足到位条件 —— 收敛死角。
        # 正确做法是"近场减速"，保留一点前进量把最后几十毫米走完。
        if obs.distance_m is not None and obs.distance_m < 0.06:
            vx = min(vx, 0.02)

        # 到位判定：距离、横向、航向三者全进死区 → 转 DOCKED
        if (obs.distance_m is not None
                and obs.distance_m * 1000 <= cfg.distance_deadband_mm
                and lateral_ok and heading_ok):
            st.state = DockState.DOCKED
            self._elapsed = 0.0
            self._contact_timer = 0.0
            logger.info("对齐完成：横向 %.1fmm 航向 %.1f° → 触点对接",
                        obs.lateral_mm, obs.heading_deg)
            return DockOutput(state=st.state, stop=True)

        if self._elapsed > cfg.timeout_s:
            return self._fail("对齐超时")

        return DockOutput(vx=vx, vy=vy, wz=wz, state=st.state)

    # ------------------------------------------------------------------

    def _charging(self, obs: DockObservation) -> DockOutput:
        """触点闭合 → 确认电流 → CHARGING；无电流 → FAULT。"""
        cfg = self.cfg
        st = self.status
        st.charge_current_a = obs.charge_current_a

        if not obs.contact_closed:
            # 被拔了/滑脱 → 退回去重试
            st.charging = False
            st.state = DockState.SEEK
            self._elapsed = 0.0
            self._contact_timer = self._no_current_timer = 0.0
            logger.warning("充电中断：触点脱离")
            return DockOutput(state=st.state, stop=True)

        self._contact_timer += 1.0
        if obs.charge_current_a < 0.05:
            self._no_current_timer += 1.0
            if self._no_current_timer > cfg.no_current_fault_s:
                st.last_error = "触点闭合但无充电电流"
                st.state = DockState.FAULT
                st.charging = False
                logger.error("回充故障：触点闭合但无电流，检查触点氧化/桩端供电")
                return DockOutput(state=st.state, stop=True)
        else:
            self._no_current_timer = 0.0

        if self._contact_timer >= cfg.contact_full_s:
            st.charging = True
            st.state = DockState.CHARGING
        return DockOutput(state=st.state, stop=True)

    # ------------------------------------------------------------------

    def _fail(self, reason: str) -> DockOutput:
        st = self.status
        st.retries += 1
        st.last_error = reason
        if st.retries <= self.cfg.max_retries:
            st.state = DockState.SEEK          # 退回重新找桩
            self._elapsed = 0.0
            # 进 SEEK 后先冷却若干拍。原因见 step() 里 SEEK 分支的注释：
            # 失败往往是因为"观测本身有问题"（车歪在桩前、红外读数卡住），
            # 若立刻用同一份旧观测重走一遍，只会一拍之内复现同样的失败，
            # retries 也就永远涨不到 max_retries，最终卡在 FAIL/ALIGN 之间抖动。
            # 冷却给了感知层刷新时间，也让车有机会退出几何奇异位置。
            self._retry_cooldown = 15.0
            logger.warning("回充失败（第 %d 次）：%s → 冷却 %.0f 拍后重试",
                           st.retries, reason, self._retry_cooldown)
        else:
            self._retry_cooldown = 0.0
            st.state = DockState.FAIL
            logger.error("回充放弃：连续 %d 次失败，最后一次原因：%s",
                         st.retries, reason)
        return DockOutput(state=st.state, stop=True)

    # ------------------------------------------------------------------

    def reset(self) -> None:
        self.status = DockStatus()
        self._elapsed = self._contact_timer = self._no_current_timer = 0.0

    def mark_charging_complete(self) -> None:
        """电量充满 → 回到 IDLE，等待下一次触发。"""
        self.status.charging = False
        self.status.state = DockState.IDLE
        self.status.retries = 0
        self._contact_timer = self._no_current_timer = 0.0
