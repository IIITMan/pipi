"""对话领域模型。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM_EVENT = "system_event"   # 由程序注入的事件（如"主人回家了"）


def new_id(prefix: str = "") -> str:
    uid = uuid.uuid4().hex[:12]
    return f"{prefix}_{uid}" if prefix else uid


@dataclass
class Bubble:
    """一条待发送的气泡消息。

    皮皮不会回一大段 —— 它会拆成 1~3 条短气泡，像微信连发。
    这是"没有 AI 味"的关键视觉体现。
    """

    text: str
    delay_ms: int = 0            # 发送前的拟人延迟
    emotion: str = "neutral"     # 对应 TTS 的情感

    def __str__(self) -> str:
        return self.text


@dataclass
class DialogueTurn:
    """一轮对话（一次用户输入 + 一次助手回复）。"""

    id: str = field(default_factory=lambda: new_id("turn"))
    user_id: str = ""
    session_id: str = ""
    turn_index: int = 0
    role: Role = Role.USER
    content: str = ""
    created_at: float = field(default_factory=time.time)

    # 附带的感知与状态快照（用于回放与调试）
    asr_emotion: str | None = None
    vision_snapshot: dict[str, Any] | None = None
    emotion_state: dict[str, Any] | None = None

    # 可观测性
    latency_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    trace_id: str | None = None

    def to_message(self) -> dict[str, str]:
        """转成 LLM messages 格式。"""
        role = "user" if self.role == Role.USER else "assistant"
        return {"role": role, "content": self.content}


@dataclass
class Session:
    """一次会话的状态容器。

    负责维护短期上下文（最近 N 轮），并在超出窗口时丢弃最旧的 —— 
    长期信息不靠它记，靠记忆系统。
    """

    id: str = field(default_factory=lambda: new_id("sess"))
    user_id: str = ""
    started_at: float = field(default_factory=time.time)
    turns: list[DialogueTurn] = field(default_factory=list)
    max_context_turns: int = 20
    trace_id: str = field(default_factory=lambda: new_id("trace"))

    def add(self, role: Role, content: str, **kwargs: Any) -> DialogueTurn:
        turn = DialogueTurn(
            user_id=self.user_id,
            session_id=self.id,
            turn_index=len(self.turns),
            role=role,
            content=content,
            trace_id=self.trace_id,
            **kwargs,
        )
        self.turns.append(turn)
        return turn

    def context_messages(self, limit: int | None = None) -> list[dict[str, str]]:
        """取最近 N 轮的 messages（不含 system）。"""
        n = limit or self.max_context_turns
        recent = self.turns[-n:]
        return [t.to_message() for t in recent if t.role != Role.SYSTEM_EVENT]

    def last_user_text(self) -> str:
        for t in reversed(self.turns):
            if t.role == Role.USER:
                return t.content
        return ""
