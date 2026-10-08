"""能力路由器 —— 决定走哪条链路（简单/复杂、哪条降级链）。

价值：
  - 成本控制：闲聊走便宜模型，复杂话题才用主模型
  - 降级决策：某个 Provider 挂了自动切下一个
  - 可观测：记录每次路由的选择，便于调优
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RouteDecision:
    capability: str
    chosen: str
    reason: str
    fallbacks_tried: list[str] = field(default_factory=list)


class CircuitBreaker:
    """熔断器 —— 某个 provider 连续失败就暂时跳过它。"""

    def __init__(self, failure_threshold: int = 3, recovery_timeout_s: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout_s
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def is_open(self, key: str, now: float) -> bool:
        """是否处于熔断状态。"""
        if key not in self._opened_at:
            return False
        if now - self._opened_at[key] >= self.recovery_timeout:
            # 半开：允许重试
            self._opened_at.pop(key, None)
            self._failures[key] = 0
            return False
        return True

    def record_success(self, key: str) -> None:
        self._failures[key] = 0
        self._opened_at.pop(key, None)

    def record_failure(self, key: str, now: float) -> None:
        self._failures[key] = self._failures.get(key, 0) + 1
        if self._failures[key] >= self.failure_threshold:
            self._opened_at[key] = now
            logger.warning("熔断器打开：%s（连续失败 %d 次）", key, self._failures[key])

    def state(self) -> dict[str, Any]:
        return {
            "failures": dict(self._failures),
            "open": list(self._opened_at.keys()),
        }


class CapabilityRouter:
    """按降级链选择可用 Provider。"""

    def __init__(self, chains: dict[str, list[str]] | None = None,
                 breaker: CircuitBreaker | None = None):
        self.chains = chains or {}
        self.breaker = breaker or CircuitBreaker()
        self._registry: dict[str, Any] = {}
        self._health: dict[str, bool] = {}
        self.decisions: list[RouteDecision] = []

    def register(self, name: str, provider: Any, available: bool | None = None) -> None:
        """注册 Provider。available=None 时自动探测。"""
        self._registry[name] = provider
        if available is None:
            try:
                available = bool(provider.is_available())
            except Exception:
                available = False
        self._health[name] = available

    def set_chain(self, capability: str, chain: list[str]) -> None:
        self.chains[capability] = chain

    def choose(self, capability: str, now: float = 0.0) -> tuple[Any | None, RouteDecision]:
        """选一个可用 Provider。

        查找顺序：`{capability}:{name}`（精确）→ `{name}`（全局）。
        这样各能力的 mock 可以按能力隔离注册，互不覆盖。
        """
        chain = self.chains.get(capability, [])
        tried: list[str] = []

        for name in chain:
            tried.append(name)
            provider = self._registry.get(f"{capability}:{name}") \
                or self._registry.get(name)
            if provider is None:
                continue
            if not self._health.get(f"{capability}:{name}",
                                    self._health.get(name, False)):
                continue
            if self.breaker.is_open(f"{capability}:{name}", now):
                continue
            decision = RouteDecision(
                capability=capability, chosen=name,
                reason=f"链上第一个可用：{name}", fallbacks_tried=tried[:-1],
            )
            self.decisions.append(decision)
            return provider, decision

        decision = RouteDecision(
            capability=capability, chosen="",
            reason=f"全部不可用（已尝试 {tried}）", fallbacks_tried=tried,
        )
        self.decisions.append(decision)
        logger.error("能力 %s 无可用 Provider", capability)
        return None, decision

    def on_failure(self, name: str, now: float) -> None:
        self.breaker.record_failure(name, now)

    def on_success(self, name: str) -> None:
        self.breaker.record_success(name)

    def health_matrix(self) -> dict[str, bool]:
        """能力健康矩阵 —— 启动时打印，便于排查环境问题。"""
        return dict(self._health)

    def summary(self) -> str:
        lines = ["能力健康矩阵："]
        for cap, chain in self.chains.items():
            states = []
            for name in chain:
                ok = self._health.get(f"{cap}:{name}", self._health.get(name, False))
                states.append(f"{name}{'√' if ok else '×'}")
            lines.append(f"  {cap:12s} {' → '.join(states)}")
        return "\n".join(lines)
