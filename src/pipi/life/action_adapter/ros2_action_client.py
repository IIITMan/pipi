"""生命模块 · ROS2 Action 客户端 —— 阶段 2 接入（占位）。

目标协议（§7.2）：
  Action 名称：/robot/perform_intent_action
  请求：ActionIntent JSON；反馈：running/finished/canceled/failed 回传生命模块
  （动作完成 mood+，被打断 alert+，长时间运动 fatigue+）

接入步骤（阶段 2/3）：
  1. 目标机安装 ROS2 + 本包；rclpy 仅存在于 ROS2 环境
  2. 实现 perform()：下发 ActionIntent 并注册结果回调
  3. 实现 cancel_all()：cancel_action + 状态回传
  4. configs/default.yaml → life.action_backend: "ros2"
"""

from __future__ import annotations

import logging

from pipi.life.action_adapter.base import ActionAdapter, ActionIntent

logger = logging.getLogger(__name__)


class ROS2ActionClient(ActionAdapter):
    """ROS2 Action 客户端（阶段 2 实装，当前为占位）。"""

    name = "ros2"

    def perform(self, intent: ActionIntent) -> None:
        logger.warning("[ros2] 尚未接入（阶段 2），动作被忽略：%s", intent.action_type)

    def cancel_all(self) -> None:
        logger.warning("[ros2] 尚未接入（阶段 2），无动作可取消")
