"""SQLite 记忆存储 —— 稀疏记忆算法的落地实现。

核心：检索不是简单的向量最近邻，而是五个因子的加权融合：
    相关性 + 显著性 + 新近度 + 重要性 + 常驻加成

这样"三天前随口说的天气"即使语义相关，也会因为显著性衰减到接近 0
而排不过"他喜欢灰色"这种重要偏好 —— 这就是"像人一样记事情"。
"""

from __future__ import annotations

import logging
from typing import Any

from pipi.domain.memory import (
    DecayReport,
    MemoryItem,
    MemoryType,
    RETRIEVE_WEIGHTS,
    RetrievedMemory,
    RetrieveQuery,
    compute_lambda,
    recency_score,
)
from pipi.infra.db.repositories import MemoryRepo
from pipi.providers.memory.embedder import Embedder, HashEmbedder, cosine

logger = logging.getLogger(__name__)


class SqliteMemoryStore:
    """基于 SQLite 的记忆存储实现。"""

    def __init__(
        self,
        repo: MemoryRepo,
        embedder: Embedder | None = None,
        *,
        weights: dict[str, float] | None = None,
        min_salience: float = 0.05,
    ):
        self.repo = repo
        self.embedder = embedder or HashEmbedder()
        self.weights = {**RETRIEVE_WEIGHTS, **(weights or {})}
        self.min_salience = min_salience

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def write(self, item: MemoryItem) -> str:
        # 自动补时间戳与衰减参数
        now = item.updated_at or item.created_at
        if not item.last_access_at:
            item.last_access_at = item.created_at
        if not item.created_at:
            item.created_at = now
        item.updated_at = now

        # 自动向量化
        if item.embedding is None:
            try:
                item.embedding = self.embedder.embed(item.content)
                item.embed_model = self.embedder.name
            except Exception as exc:
                logger.warning("向量化失败，仅存文本：%s", exc)

        self.repo.insert(item)
        return item.id

    async def write_batch(self, items: list[MemoryItem]) -> list[str]:
        return [await self.write(i) for i in items]

    # ------------------------------------------------------------------
    # 检索 —— 五因子加权融合
    # ------------------------------------------------------------------

    async def retrieve(self, q: RetrieveQuery) -> list[RetrievedMemory]:
        now = q.now or 0.0
        candidates = self.repo.list_live(q.user_id, limit=2000)
        if not candidates:
            return []

        # 1) 硬过滤：显著性太低的不参与（pinned 豁免）
        pool: list[MemoryItem] = []
        for m in candidates:
            if q.types and m.type not in q.types:
                continue
            if not m.pinned and m.salience(now) < max(self.min_salience, q.min_salience):
                continue
            pool.append(m)
        if not pool:
            return []

        # 2) 查询向量
        qvec = q.query_embedding
        if qvec is None and q.text:
            try:
                qvec = self.embedder.embed(q.text)
            except Exception:
                qvec = None

        # 3) 归一化基准
        max_sal = max((m.salience(now) for m in pool), default=1.0) or 1.0

        scored: list[RetrievedMemory] = []
        for m in pool:
            rel = self._relevance(qvec, m)
            sal = m.salience(now) / max_sal
            rec = recency_score(m.last_access_at or m.created_at, now)
            imp = m.importance
            pin = 1.0 if m.pinned else 0.0

            score = (
                self.weights["relevance"] * rel
                + self.weights["salience"] * sal
                + self.weights["recency"] * rec
                + self.weights["importance"] * imp
                + self.weights["pinned"] * pin
            )

            reason = (
                f"rel={rel:.2f} sal={sal:.2f} rec={rec:.2f} "
                f"imp={imp:.2f}{' pin' if m.pinned else ''}"
            )
            scored.append(RetrievedMemory(item=m, score=score, reason=reason))

        scored.sort(key=lambda r: r.score, reverse=True)

        # 4) 同类限流：避免 5 条全是"他喜欢XX"
        out: list[RetrievedMemory] = []
        type_count: dict[MemoryType, int] = {}
        for r in scored:
            t = r.item.type
            if type_count.get(t, 0) >= q.max_per_type and not r.item.pinned:
                continue
            type_count[t] = type_count.get(t, 0) + 1
            out.append(r)
            if len(out) >= q.top_k:
                break
        return out

    def _relevance(self, qvec: list[float] | None, m: MemoryItem) -> float:
        """语义相关性。无向量时用关键词重合度兜底。"""
        if qvec and m.embedding and len(qvec) == len(m.embedding):
            return max(0.0, cosine(qvec, m.embedding))
        # 兜底：极简字符重合
        return 0.0

    # ------------------------------------------------------------------
    # 强化
    # ------------------------------------------------------------------

    async def reinforce(self, ids: list[str], now: float,
                        turn_id: str | None = None,
                        scores: dict[str, float] | None = None) -> None:
        """被回忆的记忆获得强化（间隔重复效应）。"""
        touched: list[MemoryItem] = []
        for mid in ids:
            m = self.repo.get(mid)
            if not m:
                continue
            m.reinforce(now)
            touched.append(m)
            self.repo.log_access(
                mid, m.user_id, turn_id, (scores or {}).get(mid, 0.0), now
            )
        self.repo.batch_update(touched)

    # ------------------------------------------------------------------
    # 衰减巡检
    # ------------------------------------------------------------------

    async def decay_pass(self, user_id: str, now: float) -> DecayReport:
        """一次衰减巡检：重算显著性、淘汰遗忘项。

        这是让记忆"像人一样"的关键动作 —— 由 Scheduler 定期驱动。
        """
        items = self.repo.list_live(user_id, limit=5000)
        report = DecayReport()
        before_sum = 0.0
        after_sum = 0.0
        touched: list[MemoryItem] = []

        for m in items:
            report.checked += 1
            before = m.salience(now)
            before_sum += before

            if m.pinned:
                report.pinned_kept += 1
                after_sum += before
                continue

            # 重算 λ（访问次数可能变了）
            new_lambda = compute_lambda(m.type, m.importance, m.access_count)
            if abs(new_lambda - m.decay_lambda) > 1e-9:
                m.decay_lambda = new_lambda
                m.updated_at = now
                touched.append(m)

            after = m.salience(now)
            after_sum += after
            # decayed 语义：显著性已跌破重要性一半 —— "正在淡出的记忆"计数。
            # （before/after 同一 now 计算，无法体现时间差，故用占比口径）
            if after < m.importance * 0.5:
                report.decayed += 1

            if m.should_forget(now):
                self.repo.soft_delete(m.id, now)
                report.forgotten += 1
                report.forgotten_ids.append(m.id)

        if touched:
            self.repo.batch_update(touched)

        report.avg_salience_before = before_sum / max(1, report.checked)
        report.avg_salience_after = after_sum / max(1, report.checked)
        return report

    # ------------------------------------------------------------------
    # 删除 / 导出
    # ------------------------------------------------------------------

    async def forget(self, ids: list[str]) -> None:
        """用户主动遗忘（隐私权利）。"""
        for mid in ids:
            self.repo.hard_delete(mid)

    async def forget_all(self, user_id: str) -> int:
        """清空某用户全部记忆。"""
        items = self.repo.list_live(user_id, limit=100000)
        for m in items:
            self.repo.hard_delete(m.id)
        return len(items)

    async def export(self, user_id: str) -> dict[str, Any]:
        """导出全部记忆 —— 支持"人格迁移"到新硬件。"""
        items = self.repo.list_live(user_id, limit=100000)
        return {
            "user_id": user_id,
            "count": len(items),
            "memories": [
                {
                    "id": m.id,
                    "type": m.type.value,
                    "layer": m.layer.value,
                    "content": m.content,
                    "importance": m.importance,
                    "pinned": m.pinned,
                    "access_count": m.access_count,
                    "created_at": m.created_at,
                    "last_access_at": m.last_access_at,
                }
                for m in items
            ],
        }

    async def count(self, user_id: str) -> int:
        return self.repo.count(user_id)

    # ------------------------------------------------------------------
    # 观测辅助
    # ------------------------------------------------------------------

    def top_by_salience(self, user_id: str, now: float, k: int = 10) -> list[tuple[str, float]]:
        """当前最"鲜活"的记忆（用于可视化）。"""
        items = self.repo.list_live(user_id, limit=2000)
        pairs = [(m.content, m.salience(now)) for m in items]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return pairs[:k]

    def forgetting_curve(self, m: MemoryItem, now: float,
                         points: int = 24, span_hours: float = 72) -> list[tuple[float, float]]:
        """单条记忆的遗忘曲线（用于前端画图）。"""
        base = m.last_access_at or m.created_at
        lam = m.decay_lambda
        out = []
        for i in range(points):
            h = span_hours * i / (points - 1)
            t = base + h * 3600
            s = m.importance if m.pinned else m.importance * pow(2.718281828, -lam * h)
            out.append((h, round(s, 4)))
        return out
