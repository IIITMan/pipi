"""LLM Provider 接口定义。

抽象成接口的意义：
  - 换模型（Qwen/DeepSeek/GPT/本地）不改业务代码
  - 断网时降级到 MockLLM，保证皮皮不沉默
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


@dataclass
class LLMResponse:
    """一次生成的结果。"""

    text: str
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    finish_reason: str = "stop"


class LLMProvider(Protocol):
    """大模型接口。"""

    name: str

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.9,
        max_tokens: int = 150,
        use_cheap: bool = False,
    ) -> LLMResponse:
        """多轮对话生成。"""
        ...

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        use_cheap: bool = False,
        max_tokens: int = 400,
        temperature: float = 0.3,
    ) -> str:
        """单轮补全（用于记忆抽取、情感分析等内部任务）。"""
        ...

    def is_available(self) -> bool:
        """是否可用（用于降级决策）。"""
        ...


class StreamingLLMProvider(LLMProvider, Protocol):
    """支持流式输出的 LLM。"""

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.9,
        max_tokens: int = 150,
        use_cheap: bool = False,
    ) -> AsyncIterator[str]:
        ...
