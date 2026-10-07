"""事件定义 —— 模块间通过事件总线解耦。

设计原则：感知是持续背景流（高频、可能被丢弃），
对话是事件驱动流（低频、必须处理）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pipi.domain.audio import Transcript
from pipi.domain.vision import VisionObservation


@dataclass
class SystemEvent:
    """所有系统事件的基类。"""

    timestamp: float = field(default_factory=time.time)
    source: str = ""

    @property
    def name(self) -> str:
        return type(self).__name__


@dataclass
class UserUtteranceEvent(SystemEvent):
    """用户说话了（来自 ASR 或 CLI）。"""

    text: str = ""
    transcript: Transcript | None = None
    source: str = "asr"


@dataclass
class OwnerArrivedEvent(SystemEvent):
    """主人回来了（视觉检测到人出现）。"""

    observation: VisionObservation | None = None
    source: str = "vision"


@dataclass
class OwnerLeftEvent(SystemEvent):
    """主人离开了。"""

    observation: VisionObservation | None = None
    source: str = "vision"


@dataclass
class ProactiveTriggerEvent(SystemEvent):
    """主动搭话触发器 —— 由 Scheduler 或 ProactiveEngine 发出。"""

    trigger: str = ""           # idle/morning/night/return_home/important_date/random
    reason: str = ""            # 给 LLM 看的系统事件描述
    priority: int = 0           # 越大越优先
    context: dict[str, Any] = field(default_factory=dict)
    source: str = "scheduler"


@dataclass
class InterruptEvent(SystemEvent):
    """用户打断了皮皮（VAD 检测到插话）。"""

    partial_reply: str = ""
    source = "vad"
