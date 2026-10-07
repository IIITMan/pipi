"""Mock 视觉 —— 无摄像头时的兜底。

两种用法：
  1. 脚本化：按预设序列输出观测（用于 --sim 演示"主人回家"触发）
  2. 占位：返回"主人一直在"的静态观测
"""

from __future__ import annotations

import time
from typing import Any, Callable

from pipi.domain.vision import (
    BBox,
    ClothingObservation,
    ExpressionObservation,
    FaceObservation,
    VisionObservation,
)


class MockVision:
    """脚本驱动的假视觉。"""

    name = "mock"

    def __init__(self, script: list[VisionObservation] | None = None):
        self._script = list(script or [])
        self._idx = 0
        self._started = False

    def is_available(self) -> bool:
        return True

    def capabilities(self) -> set[str]:
        return {"detect", "recognize", "expression", "clothing"}

    def feed(self, obs: VisionObservation) -> None:
        self._script.append(obs)

    async def start(self) -> None:
        self._started = True

    async def capture(self) -> VisionObservation | None:
        if self._idx < len(self._script):
            obs = self._script[self._idx]
            self._idx += 1
            return obs
        return self._default_obs()

    async def aclose(self) -> None:
        self._started = False

    @staticmethod
    def _default_obs() -> VisionObservation:
        return VisionObservation(
            timestamp=time.time(),
            person_count=1,
            owner_present=True,
            faces=[FaceObservation(
                bbox=BBox(100, 80, 120, 120),
                person_id="owner", is_owner=True, identity_confidence=0.9,
            )],
            expressions={"owner": ExpressionObservation("neutral", 0.6)},
            clothing={"owner": ClothingObservation(
                color_names=["灰色"], color_ratios=[0.7],
            )},
        )


def make_arrival_observation(clothing_color: str = "灰色",
                             expression: str = "happy") -> VisionObservation:
    """构造一个"主人刚回家"的观测 —— 用于模拟主动打招呼。"""
    return VisionObservation(
        timestamp=time.time(),
        person_count=1,
        owner_present=True,
        owner_newly_arrived=True,
        faces=[FaceObservation(
            bbox=BBox(100, 80, 120, 120),
            person_id="owner", is_owner=True, identity_confidence=0.92,
        )],
        expressions={"owner": ExpressionObservation(expression, 0.8)},
        clothing={"owner": ClothingObservation(
            color_names=[clothing_color], color_ratios=[0.75],
        )},
        raw={"synthetic": True},
    )


def make_absence_observation() -> VisionObservation:
    """构造一个"屋里没人"的观测。"""
    return VisionObservation(
        timestamp=time.time(), person_count=0,
        owner_present=False, owner_just_left=True,
    )


def create_mock_vision() -> MockVision:
    return MockVision()


__all__ = [
    "MockVision", "create_mock_vision",
    "make_arrival_observation", "make_absence_observation",
]
