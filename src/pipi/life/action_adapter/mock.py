"""生命模块 · Mock 动作适配器 —— PC 原型调试用（§7.1）。"""

from __future__ import annotations

import logging

from pipi.life.action_adapter.base import ActionAdapter, ActionIntent

logger = logging.getLogger(__name__)


class MockActionAdapter(ActionAdapter):
    """仅打印动作日志，不调用 ROS。"""

    name = "mock"

    def perform(self, intent: ActionIntent) -> None:
        logger.info("[mock-action] %s (priority=%s, %.1fs, source=%s)",
                    intent.action_type, intent.priority,
                    intent.duration, intent.source)
        # 语义说明：PC 调试时直观看到机器人本该做什么（ROS2 阶段按同名动作执行）
        _SEMANTIC = {
            "patrol_route": "沿房间边缘巡视一圈（前进→转弯→观察→返回）",
            "look_around": "原地转头张望",
            "go_home": "返回充电桩附近待命",
            "slow_forward": "低速直行",
            "wander_turn": "随机转向漫游",
        }
        if intent.action_type in _SEMANTIC:
            logger.info("[mock-action]   ↳ %s", _SEMANTIC[intent.action_type])
        if intent.action_type == "freeze":
            logger.info("[mock-action] 全部运动停止，仅保留最低亮度呼吸灯")

    def cancel_all(self) -> None:
        logger.info("[mock-action] 取消所有动作")
