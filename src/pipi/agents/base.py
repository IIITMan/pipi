"""Agent 基类与共享数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pipi.domain.audio import Transcript
from pipi.domain.emotion import EmotionState
from pipi.domain.memory import RetrievedMemory
from pipi.domain.persona import PersonaSpec
from pipi.domain.profile import UserProfile
from pipi.domain.vision import VisionObservation


@dataclass
class AgentContext:
    """一次对话处理所需的全部上下文。"""

    user_id: str
    user_text: str
    now: float

    # 状态
    profile: UserProfile | None = None
    emotion_state: EmotionState | None = None
    persona: PersonaSpec | None = None
    inner_brief: str = ""        # PULSE 此刻真实内心状态（注入对话，消除两张皮）
    tool_brief: str = ""         # 工具查询结果（如天气），注入 system 供 LLM 用人格口吻作答

    # 感知
    transcript: Transcript | None = None
    vision: VisionObservation | None = None

    # 会话
    history: list[dict[str, str]] = field(default_factory=list)
    turn_id: str | None = None
    trace_id: str | None = None

    # 中间结果（Agent 之间传递）
    memories: list[RetrievedMemory] = field(default_factory=list)
    emotion_signals: list[Any] = field(default_factory=list)
    needs_comfort: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """Agent 执行结果。"""

    ok: bool = True
    data: Any = None
    error: str = ""
    latency_ms: int = 0


class BaseAgent:
    """Agent 基类。"""

    name: str = "base"

    async def run(self, ctx: AgentContext) -> AgentResult:  # pragma: no cover
        raise NotImplementedError
