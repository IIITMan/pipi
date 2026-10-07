"""生命模块 · 动作适配器抽象基类（§7.1 双模式）。

高层意图 → 机器人动作，解耦硬件：
  - MockActionAdapter：PC 原型调试，仅打印动作日志
  - ROS2ActionClient：阶段 2 接入 ROS2 Action Server

所有动作默认 interruptible=True，支持随时取消（§7.2）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ActionIntent:
    """§7.2 动作通信协议（生命模块 → ROS2 Action Server）。"""

    action_type: str                    # breath_light / nod / wander_turn / freeze ...
    priority: int = 2                   # 应激5 > 交互3~4 > 漫游1~2
    duration: float = 1.0               # 预估持续秒
    params: dict = field(default_factory=dict)
    interruptible: bool = True
    source: str = "thought"             # thought/human/instinct
    action_id: str = ""


class ActionAdapter(ABC):
    """动作抽象基类：下发高层意图，硬件差异封装在实现里。"""

    name: str = "base"

    @abstractmethod
    def perform(self, intent: ActionIntent) -> None:
        """下发动作（非阻塞，内部自行异步/排队）。"""

    @abstractmethod
    def cancel_all(self) -> None:
        """Barge-in：取消当前所有动作（§7.4 规则 3）。"""
