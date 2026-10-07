"""向量化 —— 把记忆文本变成向量，用于语义检索。

三级降级（保证任何环境都能跑）：
  1. sentence-transformers + bge-small-zh   语义最好，需下模型
  2. TF-IDF 字符 n-gram                       无需任何模型，纯 numpy
  3. 哈希词袋                                  无 numpy 也能跑

接口统一：embed(text) -> list[float]
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from typing import Protocol

logger = logging.getLogger(__name__)

# 简单中文分词：按字切 + 抽取 2-gram（无需 jieba 依赖）
_CJK = re.compile(r"[\u4e00-\u9fa5]")
_WORD = re.compile(r"[a-zA-Z0-9]+")


def _tokenize(text: str) -> list[str]:
    """中英混合分词：汉字取 1-gram 和 2-gram，英文取单词。"""
    text = text.lower()
    tokens: list[str] = []
    # 汉字
    cjk = _CJK.findall(text)
    tokens.extend(cjk)
    for i in range(len(cjk) - 1):
        tokens.append(cjk[i] + cjk[i + 1])
    # 英文/数字
    tokens.extend(_WORD.findall(text))
    return tokens


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------------------
# 1. sentence-transformers（最优）
# ---------------------------------------------------------------------------

class BGEEmbedder:
    """基于 sentence-transformers 的语义向量。"""

    name = "bge_small"

    def __init__(self, model_path: str = "BAAI/bge-small-zh-v1.5", device: str = "auto"):
        from sentence_transformers import SentenceTransformer  # 延迟导入

        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"

        self._model = SentenceTransformer(model_path, device=device)
        self.dim = self._model.get_sentence_embedding_dimension()
        self.name = f"bge_small:{device}"
        logger.info("已加载语义向量模型 %s (dim=%d, device=%s)",
                    model_path, self.dim, device)

    def embed(self, text: str) -> list[float]:
        vec = self._model.encode(text, normalize_embeddings=True,
                                 show_progress_bar=False)
        return vec.tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = self._model.encode(texts, normalize_embeddings=True,
                                  show_progress_bar=False, batch_size=32)
        return [v.tolist() for v in vecs]


# ---------------------------------------------------------------------------
# 2. TF-IDF 字符 n-gram（无需模型，效果尚可）
# ---------------------------------------------------------------------------

class TfidfEmbedder:
    """基于字符 n-gram 的 TF-IDF 向量。

    没有"训练"过程 —— 用固定的哈希空间 + 子线性 TF 加权，
    再 L2 归一化。对短文本的中文相似度够用。
    """

    name = "tfidf"

    def __init__(self, dim: int = 512):
        self.dim = dim
        self._df: Counter = Counter()      # 文档频率
        self._n_docs = 0

    def _hash(self, token: str) -> int:
        h = hashlib.md5(token.encode("utf-8")).digest()
        return int.from_bytes(h[:4], "little") % self.dim

    def _raw_vector(self, text: str, tf_weights: dict[str, float] | None = None) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _tokenize(text)
        if not tokens:
            return vec
        counts = Counter(tokens)
        for token, cnt in counts.items():
            weight = tf_weights.get(token, 1.0) if tf_weights else 1.0
            # 子线性 TF：1 + log(count)
            tf = 1.0 + math.log(cnt)
            idx = self._hash(token)
            vec[idx] += tf * weight
        return vec

    def _idf(self, token: str) -> float:
        if self._n_docs == 0:
            return 1.0
        df = self._df.get(token, 0)
        return math.log((1 + self._n_docs) / (1 + df)) + 1.0

    def fit(self, corpus: list[str]) -> None:
        """用已有语料统计 IDF（可选，不调用也能用）。"""
        self._df.clear()
        self._n_docs = len(corpus)
        for doc in corpus:
            for token in set(_tokenize(doc)):
                self._df[token] += 1

    def embed(self, text: str) -> list[float]:
        tokens = _tokenize(text)
        weights = {t: self._idf(t) for t in set(tokens)}
        vec = self._raw_vector(text, weights)
        return _l2_normalize(vec)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


# ---------------------------------------------------------------------------
# 3. 哈希词袋（无依赖兜底）
# ---------------------------------------------------------------------------

class HashEmbedder:
    """最小实现：只做词袋哈希，不依赖 numpy。"""

    name = "hash"
    dim = 256

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _tokenize(text):
            h = int.from_bytes(hashlib.md5(token.encode()).digest()[:4], "little")
            vec[h % self.dim] += 1.0
        return _l2_normalize(vec)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-12:
        return vec
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。两个向量都已归一化时可退化为点积。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def create_embedder(prefer: str = "auto", model_path: str = "") -> Embedder:
    """按可用性创建 embedder。

    prefer: auto | bge_small | tfidf | hash
    """
    if prefer in ("auto", "bge_small"):
        try:
            return BGEEmbedder(model_path or "BAAI/bge-small-zh-v1.5")
        except Exception as exc:
            if prefer == "bge_small":
                logger.warning("bge_small 加载失败，降级：%s", exc)
            else:
                logger.debug("语义向量模型不可用，用 TF-IDF 兜底：%s", exc)

    if prefer in ("auto", "tfidf"):
        return TfidfEmbedder()

    return HashEmbedder()
