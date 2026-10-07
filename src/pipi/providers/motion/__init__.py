"""运动控制 Provider。

单板方案：全机只有一块下位机 —— 轮趣 C30D，
头部舵机 / 表情灯带 / 麦克纳姆轮底盘都由它驱动
（详见 `pipi.providers.motion.wheeltec` 的模块文档）。
"""

from __future__ import annotations

import logging
from typing import Any

from pipi.providers.motion.base import MotionProvider
from pipi.providers.motion.mock import MockMotion

logger = logging.getLogger(__name__)


def _make_wheeltec(cfg: Any) -> "MotionProvider | None":
    from pipi.providers.motion.wheeltec import WheeltecC30D
    sub = cfg.get("motion.wheeltec", {}) or {}
    m = WheeltecC30D(
        topic_cmd=sub.get("topic_cmd", "/cmd_vel"),
        topic_head=sub.get("topic_head", "/pipi/head"),
        topic_odom=sub.get("topic_odom", "/odom"),
        node_name=sub.get("node_name", "pipi_base"),
        max_pan_deg=float(cfg.get("motion.max_head_pan_deg", 120) or 120),
        max_tilt_deg=float(sub.get("max_head_tilt_deg", 45) or 45),
    )
    return m if m.is_available() else None


def create_motion(cfg: Any) -> MotionProvider | None:
    """按配置创建运动控制。

    provider 取值：
      mock      纯日志模拟（软件阶段默认）
      wheeltec  轮趣 C30D 实机（ROS2 话题，全机六个自由度）
    """
    provider = cfg.get("motion.provider", "mock") if hasattr(cfg, "get") else "mock"
    mock = lambda: MockMotion(verbose=bool(cfg.get("motion.verbose", False)))  # noqa: E731

    if provider == "wheeltec":
        try:
            if (m := _make_wheeltec(cfg)) is not None:
                return m
            logger.info("C30D 不可用（未装 ROS2 或节点异常），退到 MockMotion")
        except Exception as exc:
            logger.info("C30D 初始化失败：%s", exc)
        return mock()

    if provider == "mock":
        return mock()

    return None


def create_mock_motion() -> MockMotion:
    return MockMotion()


__all__ = [
    "MotionProvider", "MockMotion", "create_motion", "create_mock_motion",
]
