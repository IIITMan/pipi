"""记忆领域模型与稀疏记忆算法。

这是"皮皮"区别于普通聊天 AI 的核心 —— 像人一样**选择性遗忘**：

  salience = importance × exp(−λ · Δt_hours)

  - importance  基础重要性（写入时打分，身份信息接近 1，闲聊 0.3）
  - λ           衰减率，由「记忆类型 + 重要性 + 被访问次数」共同决定
  - Δt          距上次被回忆的时间

被回忆时获得强化（间隔重复效应）：稳定性 S 提升、λ 下降、salience 回升。
闲聊几小时就淡，喜好能记一个月，名字永不遗忘。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryType(str, Enum):
    """记忆类型 —— 决定衰减速度。"""

    IDENTITY = "identity"       # 身份：名字/城市/职业      → 永不遗忘
    COMMITMENT = "commitment"   # 约定/承诺                → 永不遗忘
    PREFERENCE = "preference"   # 喜好/厌恶                → 半衰期约 40 天
    RELATION = "relation"       # 人际关系/吃醋对象         → 半衰期约 6 天
    EVENT = "event"             # 事件：面试/出差           → 半衰期约 3 天
    EMOTION = "emotion"         # 情感印记                  → 半衰期约 1.5 天
    CHITCHAT = "chitchat"       # 闲聊（最易遗忘）          → 半衰期约 18 小时


class MemoryLayer(str, Enum):
    """记忆分层。L1 常驻注入，L2 按需检索。"""

    L1 = "L1"   # 身份档案级，每轮都注入
    L2 = "L2"   # 长期记忆，按相关性检索


# 各类型的基础衰减率 λ_base（每小时）
LAMBDA_BASE: dict[MemoryType, float] = {
    MemoryType.IDENTITY: 0.0,      # 由 pinned 保证，不衰减
    MemoryType.COMMITMENT: 0.0,    # 同上
    MemoryType.PREFERENCE: 0.002,  # 半衰期 ≈ 347h ≈ 14.5 天
    MemoryType.RELATION: 0.005,
    MemoryType.EVENT: 0.010,
    MemoryType.EMOTION: 0.020,
    MemoryType.CHITCHAT: 0.050,    # 半衰期 ≈ 14h
}

# 各类型的默认重要性下限（LLM 打分偏低时的兜底）
IMPORTANCE_FLOOR: dict[MemoryType, float] = {
    MemoryType.IDENTITY: 0.95,
    MemoryType.COMMITMENT: 0.90,
    MemoryType.PREFERENCE: 0.70,
    MemoryType.RELATION: 0.60,
    MemoryType.EVENT: 0.55,
    MemoryType.EMOTION: 0.50,
    MemoryType.CHITCHAT: 0.30,
}

# 各类型的重要性上限（闲聊不可能很重要）
IMPORTANCE_CEIL: dict[MemoryType, float] = {
    MemoryType.CHITCHAT: 0.35,
}

# 淘汰阈值
FORGET_THRESHOLD = 0.05
FORGET_MIN_ACCESS = 2

# 回忆强化系数
REINFORCE_STABILITY_FACTOR = 1.4
REINFORCE_LAMBDA_DISCOUNT = 0.3

# 去重阈值
DEDUP_SIMILARITY_THRESHOLD = 0.92


def compute_lambda(
    mem_type: MemoryType,
    importance: float,
    access_count: int,
) -> float:
    """计算单条记忆的衰减率 λ。

    λ = λ_base × (1 − 0.8·importance) × 1/(1 + 0.3·log(1+access_count))

    - 越重要衰减越慢（乘上 (1-0.8·imp)，importance=1 时几乎不衰减）
    - 被回忆越多衰减越慢（对数折扣，收益递减）
    """
    base = LAMBDA_BASE.get(mem_type, 0.02)
    if base <= 0.0:
        return 0.0
    importance_factor = 1.0 - 0.8 * max(0.0, min(1.0, importance))
    access_factor = 1.0 / (1.0 + REINFORCE_LAMBDA_DISCOUNT * math.log(1 + max(0, access_count)))
    return base * importance_factor * access_factor


def half_life_hours(mem_type: MemoryType, importance: float, access_count: int = 0) -> float:
    """半衰期（小时）。λ=0（pinned）时返回 inf。"""
    lam = compute_lambda(mem_type, importance, access_count)
    if lam <= 0.0:
        return float("inf")
    return math.log(2) / lam


@dataclass
class MemoryItem:
    """一条记忆。

    salience 是"当前显著性"（会随时间衰减的动态值），
    importance 是"基础重要性"（写入时定下，基本不变），
    两者不可混淆 —— 检索时两个都要用。
    """

    id: str
    user_id: str
    type: MemoryType
    content: str
    importance: float
    layer: MemoryLayer = MemoryLayer.L2
    created_at: float = 0.0
    updated_at: float = 0.0
    last_access_at: float = 0.0
    access_count: int = 0
    stability: float = 1.0
    pinned: bool = False
    source_turn_id: str | None = None
    embedding: list[float] | None = None
    embed_model: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    deleted_at: float | None = None

    # 内部：允许持久化层回填已知的 λ（避免每次重算）
    _lambda_override: float | None = field(default=None, repr=False, compare=False)

    # -------- 兼容性：允许用字符串构造 --------

    def __post_init__(self) -> None:
        if isinstance(self.type, str):
            self.type = MemoryType(self.type)
        if isinstance(self.layer, str):
            self.layer = MemoryLayer(self.layer)

    # -------- 衰减计算 --------

    @property
    def decay_lambda(self) -> float:
        """衰减率 λ。若构造时显式给过值（如从数据库读回），优先用它。"""
        if self.pinned:
            return 0.0
        if self._lambda_override is not None:
            return self._lambda_override
        return compute_lambda(self.type, self.importance, self.access_count)

    @decay_lambda.setter
    def decay_lambda(self, value: float) -> None:
        """显式覆写 λ（写入时 / 衰减巡检重算时）。

        走 _lambda_override，保证 repo 持久化与读回闭环。
        """
        self._lambda_override = float(value)

    def salience(self, now: float) -> float:
        """当前显著性。pinned 记忆恒等于 importance。"""
        if self.pinned:
            return self.importance
        lam = self.decay_lambda
        if lam <= 0.0:
            return self.importance
        hours = max(0.0, (now - (self.last_access_at or self.created_at)) / 3600.0)
        return self.importance * math.exp(-lam * hours)

    def should_forget(self, now: float) -> bool:
        """是否应被淘汰（软删除）。pinned 与常被访问的记忆豁免。"""
        if self.pinned or self.access_count >= FORGET_MIN_ACCESS:
            return False
        return self.salience(now) < FORGET_THRESHOLD

    def reinforce(self, now: float) -> "MemoryItem":
        """被回忆时强化 —— 间隔重复效应。"""
        self.access_count += 1
        self.stability *= REINFORCE_STABILITY_FACTOR
        self.last_access_at = now
        self.updated_at = now
        return self

    def half_life_hours(self) -> float:
        return half_life_hours(self.type, self.importance, self.access_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "type": self.type.value,
            "layer": self.layer.value,
            "content": self.content,
            "importance": self.importance,
            "access_count": self.access_count,
            "pinned": self.pinned,
            "created_at": self.created_at,
            "last_access_at": self.last_access_at,
        }


@dataclass
class RetrieveQuery:
    """记忆检索请求。"""

    user_id: str
    text: str
    query_embedding: list[float] | None = None
    top_k: int = 5
    min_salience: float = FORGET_THRESHOLD
    types: list[MemoryType] | None = None
    now: float | None = None
    max_per_type: int = 2


@dataclass
class RetrievedMemory:
    """检索结果，带得分与召回理由（便于调试）。"""

    item: MemoryItem
    score: float
    reason: str = ""

    def __str__(self) -> str:
        return f"[{self.score:.3f}] {self.item.content}  ({self.reason})"


@dataclass
class DecayReport:
    """一次衰减巡检的报告。"""

    checked: int = 0
    decayed: int = 0
    forgotten: int = 0
    pinned_kept: int = 0
    forgotten_ids: list[str] = field(default_factory=list)
    avg_salience_before: float = 0.0
    avg_salience_after: float = 0.0

    def summary(self) -> str:
        return (
            f"巡检 {self.checked} 条 / 衰减 {self.decayed} 条 / "
            f"淘汰 {self.forgotten} 条 / 常驻保留 {self.pinned_kept} 条 "
            f"(平均显著性 {self.avg_salience_before:.3f} → {self.avg_salience_after:.3f})"
        )


# ---------------------------------------------------------------------------
# 检索排序权重
# ---------------------------------------------------------------------------

RETRIEVE_WEIGHTS = {
    "relevance": 0.45,   # 语义相关性
    "salience": 0.25,    # 当前显著性
    "recency": 0.10,     # 新近程度
    "importance": 0.12,  # 基础重要性
    "pinned": 0.08,      # 常驻加成
}


def recency_score(last_access_at: float, now: float, tau_days: float = 30.0) -> float:
    """新近度：exp(−Δt_days / τ)。"""
    days = max(0.0, (now - last_access_at) / 86400.0)
    return math.exp(-days / tau_days)
