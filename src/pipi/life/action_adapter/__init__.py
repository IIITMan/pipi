"""生命模块 · 动作适配器。"""

from pipi.life.action_adapter.base import ActionAdapter, ActionIntent
from pipi.life.action_adapter.mock import MockActionAdapter
from pipi.life.action_adapter.ros2_action_client import ROS2ActionClient


def create_action_adapter(mode: str) -> ActionAdapter:
    """§7.1 双模式工厂：mock / ros2。"""
    if mode == "ros2":
        return ROS2ActionClient()
    return MockActionAdapter()


__all__ = ["ActionAdapter", "ActionIntent", "MockActionAdapter",
           "ROS2ActionClient", "create_action_adapter"]
