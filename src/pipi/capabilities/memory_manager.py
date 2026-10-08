"""记忆管理器 —— 抽取、打分、去重、衰减的全部业务逻辑。

分工：
  SqliteMemoryStore  负责"存和取"（算法）
  MemoryManager      负责"决定记什么、记多重"（策略）

抽取有两条路：
  1. LLM 抽取（有模型时）：从对话里挑出值得记的事，附重要性初判
  2. 规则抽取（无模型时）：正则 + 词表，ergo 纯软件环境也能跑
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pipi.domain.memory import (
    IMPORTANCE_CEIL,
    IMPORTANCE_FLOOR,
    DEDUP_SIMILARITY_THRESHOLD,
    DecayReport,
    MemoryItem,
    MemoryLayer,
    MemoryType,
    RetrievedMemory,
    RetrieveQuery,
    compute_lambda,
)
from pipi.domain.profile import UserProfile
from pipi.providers.memory.embedder import Embedder, cosine
from pipi.providers.memory.sqlite_store import SqliteMemoryStore
from pipi.domain.dialogue import new_id

logger = logging.getLogger(__name__)

# 垃圾候选门禁：疑问/否定/闲聊语气的"事实"不该被当记忆存下来
# （曾把"我没有告诉你我叫什么吗"抽成 identity"他叫什么吗"且 pinned，污染召回）
_JUNK_CHARS = re.compile(r"什么|吗|呢|吧|啊|嘛|哪|谁|怎么|为什么|不知道|"
                         "没有告诉|是不是|要不要|好不好|可不可以")
_JUNK_TYPE_FLOOR = {MemoryType.IDENTITY, MemoryType.PREFERENCE,
                    MemoryType.EVENT, MemoryType.RELATION}


def _is_junk_candidate(c: "MemoryCandidate") -> bool:
    """身份/偏好/事件/关系类候选含疑问否定语气 → 判为垃圾丢弃。"""
    if c.type not in _JUNK_TYPE_FLOOR:
        return False
    if _JUNK_CHARS.search(c.content):
        return True
    # 身份错位：把助手自己的名字（皮皮/pipi）记成「用户/对方」的名字
    # （曾产出『对方的名字是皮皮』与『用户叫豆豆』并存，令模型身份混乱）
    if c.type is MemoryType.IDENTITY and \
            re.search(r"(?:对方|用户|他)的?名字?是\s*(?:皮皮|pipi|PP)\b", c.content):
        return True
    return False


# ---------------------------------------------------------------------------
# 规则抽取的词典
# ---------------------------------------------------------------------------

NEGATIVE_MARKERS = [
    "累", "烦", "难过", "难受", "压力", "崩溃", "哭", "焦虑", "emo",
    "不开心", "郁闷", "委屈", "疼", "生病", "加班",
]
POSITIVE_MARKERS = ["开心", "高兴", "太棒", "升职", "赢了", "成功了", "通过了", "舒服"]
RIVAL_MARKERS = [
    "猫", "狗", "别的机器人", "小爱", "天猫精灵", "ChatGPT", "小度", "朋友家",
]
TIME_MARKERS = [
    "明天", "后天", "下周", "下个月", "周末", "周一", "周二", "周三", "周四",
    "周五", "周六", "周日", "生日", "面试", "考试", "出差", "旅行", "搬家",
    "入职", "答辩", "体检", "约会",
]
DISLIKE_VERBS = r"(?:不喜欢|讨厌|烦|受不了|恶心)"
# 单字"爱"要加否定前置断言，否则"可爱多了"里的"爱"会被误判成动词。
# 只挡形容词性的"可爱/疼爱/怜爱"；"热爱/喜爱/酷爱/偏爱"是真正的动词，保留。
LIKE_VERBS = r"(?:喜欢|热爱|喜爱|酷爱|(?<![可疼怜宠])爱|偏好|中意)"

# 喜好对象里的"空壳词" —— 抽出来也没信息量，直接丢
EMPTY_OBJECTS = {
    "了", "的", "这个", "那个", "时候", "东西", "事儿", "事情", "多了",
    "少了", "死了", "这样", "那样", "什么", "怎么", "一个", "一下",
    "这口", "那种", "这种", "上头", "到不行", "得不行", "不起来",
}


@dataclass
class MemoryCandidate:
    """抽取出的候选记忆（尚未落库）。"""

    type: MemoryType
    content: str
    importance: float = 0.5
    pinned: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractResult:
    candidates: list[MemoryCandidate] = field(default_factory=list)
    profile_updates: dict[str, Any] = field(default_factory=dict)
    emotion_signals: dict[str, Any] = field(default_factory=dict)
    method: str = "rule"


class MemoryManager:
    """记忆的写入策略 + 衰减调度。"""

    def __init__(
        self,
        store: SqliteMemoryStore,
        embedder: Embedder,
        llm: Any | None = None,          # LLMProvider，可为 None（纯规则）
        *,
        pin_types: set[MemoryType] | None = None,
        top_k: int = 5,
        max_per_type: int = 2,
    ):
        self.store = store
        self.embedder = embedder
        self.llm = llm
        self.pin_types = pin_types or {MemoryType.IDENTITY, MemoryType.COMMITMENT}
        self.top_k = top_k
        self.max_per_type = max_per_type

    # ------------------------------------------------------------------
    # 抽取
    # ------------------------------------------------------------------

    async def extract(
        self,
        user_text: str,
        reply_text: str = "",
        profile: UserProfile | None = None,
        emotion_intensity: float = 0.0,
    ) -> ExtractResult:
        """从一轮对话中抽取值得长期记住的信息。"""
        # 优先尝试 LLM 抽取
        if self.llm is not None:
            try:
                result = await self._extract_with_llm(user_text, reply_text)
                if result.candidates:
                    return self._score_and_finalize(result, user_text, emotion_intensity)
            except Exception as exc:
                logger.warning("LLM 抽取失败，回退规则抽取：%s", exc)

        result = self._extract_with_rules(user_text, emotion_intensity)
        return self._score_and_finalize(result, user_text, emotion_intensity)

    async def _extract_with_llm(self, user_text: str, reply_text: str) -> ExtractResult:
        """用便宜模型抽取。返回结构化 JSON。"""
        from pipi.prompts import render  # 延迟导入避免循环

        prompt = render(
            "memory_extract.jinja",
            user_text=user_text,
            reply_text=reply_text,
        )
        raw = await self.llm.complete(prompt, use_cheap=True, max_tokens=400)
        data = _parse_json_array(raw)

        result = ExtractResult(method="llm")
        for d in data:
            try:
                mtype = MemoryType(d.get("type", "chitchat"))
            except ValueError:
                mtype = MemoryType.CHITCHAT
            content = str(d.get("content", "")).strip()
            if not content:
                continue
            result.candidates.append(MemoryCandidate(
                type=mtype,
                content=content,
                importance=float(d.get("importance", 0.5)),
                pinned=bool(d.get("pinned", False)),
                meta={"hint": d.get("hint", "")},
            ))
        return result

    def _extract_with_rules(self, text: str, emotion_intensity: float) -> ExtractResult:
        """正则 + 词表抽取。不依赖任何模型。

        注意：规则路径**刻意不抽 CHITCHAT**。
        判断"这句闲聊值不值得长期记住"需要语义理解，规则做不到；
        硬抽只会让记忆库塞满"今天天气不错"这种噪音。
        规则路径只抽**有明确句式信号**的东西：身份、喜好、约定、事件、吃醋对象。
        真正的闲聊记忆（"他今天好像心情不错"）交给 LLM 路径。
        """
        result = ExtractResult(method="rule")
        updates: dict[str, Any] = {}

        # --- 1. 身份信息 ---
        if m := re.search(r"(?:我叫|我是|叫我|你可以叫我)([\u4e00-\u9fa5A-Za-z·]{1,8})", text):
            name = m.group(1)
            # 疑问/否定句不是自我介绍（"我叫什么吗" "我是不是…"）
            if not _JUNK_CHARS.search(name):
                updates["nickname"] = name
                updates["call_name"] = name
                result.candidates.append(MemoryCandidate(
                    type=MemoryType.IDENTITY, content=f"他叫{name}",
                    importance=1.0, pinned=True,
                ))

        if m := re.search(r"我(?:住在|来自)([\u4e00-\u9fa5]{2,10}?)(?:市|区|县|$|[，。！？\s])", text):
            updates["city"] = m.group(1)
            result.candidates.append(MemoryCandidate(
                type=MemoryType.IDENTITY, content=f"他住在{m.group(1)}",
                importance=0.9, pinned=True,
            ))

        if m := re.search(r"我(?:是|做|干)(?:个)?([\u4e00-\u9fa5]{2,10}?)(?:的|工作|这行)", text):
            updates["job"] = m.group(1)
            result.candidates.append(MemoryCandidate(
                type=MemoryType.IDENTITY, content=f"他是做{m.group(1)}的",
                importance=0.85, pinned=True,
            ))

        if re.search(r"(?:我)?(?:一个人住|独居|自己住)", text):
            updates["living"] = "独居"
            result.candidates.append(MemoryCandidate(
                type=MemoryType.IDENTITY, content="他一个人住",
                importance=0.85, pinned=True,
            ))

        # --- 2. 喜好 / 厌恶 ---
        # 先匹配否定式，再从原文中抹去，避免"不喜欢X"被误抽成"喜欢X"
        stripped = text
        for m in re.finditer(rf"{DISLIKE_VERBS}([\u4e00-\u9fa5A-Za-z0-9]{{2,12}})", text):
            item = _clean_item(m.group(1))
            if item:
                result.candidates.append(MemoryCandidate(
                    type=MemoryType.PREFERENCE, content=f"他不喜欢{item}",
                    importance=0.75, meta={"dislike": item},
                ))
                stripped = stripped.replace(m.group(0), "　")

        for m in re.finditer(rf"{LIKE_VERBS}([\u4e00-\u9fa5A-Za-z0-9]{{2,12}})", stripped):
            item = _clean_item(m.group(1))
            if item:
                result.candidates.append(MemoryCandidate(
                    type=MemoryType.PREFERENCE, content=f"他喜欢{item}",
                    importance=0.75, meta={"like": item},
                ))

        # --- 3. 吃醋对象 ---
        for marker in RIVAL_MARKERS:
            if marker in text and re.search(r"(?:养|有|喜欢|想|买了|新)", text):
                result.candidates.append(MemoryCandidate(
                    type=MemoryType.RELATION, content=f"他提到了{marker}",
                    importance=0.6, meta={"rival": marker},
                ))
                break

        # --- 4. 事件 ---
        for marker in TIME_MARKERS:
            if marker in text:
                result.candidates.append(MemoryCandidate(
                    type=MemoryType.EVENT,
                    content=f"{_date_prefix()}他提到{marker}的事：{text.strip()[:30]}",
                    importance=0.65, meta={"event_marker": marker},
                ))
                break

        # --- 5. 情感印记（情绪强烈时）---
        if any(w in text for w in NEGATIVE_MARKERS):
            result.emotion_signals["negative"] = True
            if emotion_intensity >= 0.6:
                result.candidates.append(MemoryCandidate(
                    type=MemoryType.EMOTION,
                    content=f"他今天情绪不太好：{text.strip()[:30]}",
                    importance=0.55,
                ))
        if any(w in text for w in POSITIVE_MARKERS):
            result.emotion_signals["positive"] = True

        # --- 6. 约定（必须是主动承诺，不能是"你还记得吗"这种提问/追问）---
        if re.search(r"(?:我答应你|答应你|说好了|约好了|一言为定|我保证|我发誓)", text):
            # 排除问句
            if not re.search(r"(?:吗|呢|\?|？)\s*$", text.strip()):
                result.candidates.append(MemoryCandidate(
                    type=MemoryType.COMMITMENT,
                    content=f"他和你约定过：{text.strip()[:30]}",
                    importance=0.9, pinned=True,
                ))

        result.profile_updates = updates
        return result

    def _score_and_finalize(
        self,
        result: ExtractResult,
        user_text: str,
        emotion_intensity: float,
    ) -> ExtractResult:
        """按类型修正重要性，并决定是否 pinned。"""
        # 档案字段同样过质检门（nickname 曾被污染成"什么吗"直接进 Prompt）
        for key in ("nickname", "call_name", "city", "job", "living"):
            val = result.profile_updates.get(key)
            if isinstance(val, str) and _JUNK_CHARS.search(val):
                logger.debug("丢弃垃圾档案更新：%s=%r", key, val)
                result.profile_updates.pop(key)

        kept: list[MemoryCandidate] = []
        for c in result.candidates:
            # 质检门：疑问/否定语气的"事实"直接丢弃（覆盖规则与 LLM 两条路）
            if _is_junk_candidate(c):
                logger.debug("丢弃垃圾记忆候选：%r", c.content)
                continue
            kept.append(c)
        result.candidates = kept

        for c in result.candidates:
            # 类型下限/上限
            floor = IMPORTANCE_FLOOR.get(c.type, 0.4)
            ceil = IMPORTANCE_CEIL.get(c.type, 1.0)
            c.importance = max(floor, min(ceil, c.importance))

            # 情绪强度加成：激动时说的话记得更牢
            if emotion_intensity > 0.7 and c.type != MemoryType.CHITCHAT:
                c.importance = min(1.0, c.importance + 0.1)

            # pinned 类型
            if c.type in self.pin_types:
                c.pinned = True

        return result

    # ------------------------------------------------------------------
    # 写入（含去重合并）
    # ------------------------------------------------------------------

    async def commit(
        self,
        user_id: str,
        candidates: list[MemoryCandidate],
        now: float,
        turn_id: str | None = None,
    ) -> list[str]:
        """把候选记忆写入存储，自动去重。"""
        written: list[str] = []
        for c in candidates:
            emb = None
            try:
                emb = self.embedder.embed(c.content)
            except Exception:
                pass

            # 去重：找高度相似的旧记忆
            existing = await self._find_similar(user_id, c, emb, now)
            if existing:
                # 合并：保留更高重要性，累加强化
                existing.content = self._merge_content(existing.content, c.content)
                existing.importance = max(existing.importance, c.importance)
                existing.pinned = existing.pinned or c.pinned
                existing.reinforce(now)
                existing.updated_at = now
                if emb is not None:
                    existing.embedding = emb
                self.store.repo.update(existing)
                written.append(existing.id)
                continue

            item = MemoryItem(
                id=new_id("mem"),
                user_id=user_id,
                type=c.type,
                layer=MemoryLayer.L1 if c.pinned else MemoryLayer.L2,
                content=c.content,
                importance=c.importance,
                pinned=c.pinned,
                created_at=now,
                updated_at=now,
                last_access_at=now,
                embedding=emb,
                embed_model=self.embedder.name if emb else None,
                source_turn_id=turn_id,
                meta=c.meta,
            )
            # 显式固化写入时的 λ（access_count=0 视角），经 property setter
            # 写入 _lambda_override，保证 repo 持久化/读回闭环
            item.decay_lambda = compute_lambda(item.type, item.importance, 0)
            await self.store.write(item)
            written.append(item.id)
        return written

    async def _find_similar(
        self, user_id: str, c: MemoryCandidate,
        emb: list[float] | None, now: float,
    ) -> MemoryItem | None:
        """查找可合并的相似记忆。"""
        if emb is None:
            return None
        pool = self.store.repo.list_live(user_id, limit=2000)
        best: tuple[float, MemoryItem] | None = None
        for m in pool:
            if m.type != c.type or not m.embedding:
                continue
            if len(m.embedding) != len(emb):
                continue
            sim = cosine(emb, m.embedding)
            if sim >= DEDUP_SIMILARITY_THRESHOLD:
                if best is None or sim > best[0]:
                    best = (sim, m)
        return best[1] if best else None

    @staticmethod
    def _merge_content(old: str, new: str) -> str:
        """合并重复记忆的内容（保留更具体的那条）。"""
        if new in old:
            return old
        if old in new:
            return new
        return f"{old}；{new}"[:80]

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    async def recall(
        self,
        user_id: str,
        text: str,
        now: float,
        top_k: int | None = None,
        reinforce: bool = True,
        turn_id: str | None = None,
    ) -> list[RetrievedMemory]:
        """召回相关记忆，并（默认）强化它们。"""
        q = RetrieveQuery(
            user_id=user_id,
            text=text,
            top_k=top_k or self.top_k,
            now=now,
            max_per_type=self.max_per_type,
        )
        results = await self.store.retrieve(q)
        if reinforce and results:
            await self.store.reinforce(
                [r.item.id for r in results],
                now,
                turn_id=turn_id,
                scores={r.item.id: r.score for r in results},
            )
        return results

    # ------------------------------------------------------------------
    # 衰减
    # ------------------------------------------------------------------

    async def decay(self, user_id: str, now: float) -> DecayReport:
        report = await self.store.decay_pass(user_id, now)
        logger.info("记忆衰减：%s", report.summary())
        return report

    # ------------------------------------------------------------------
    # 应用档案更新
    # ------------------------------------------------------------------

    @staticmethod
    def apply_profile_updates(profile: UserProfile, updates: dict[str, Any]) -> bool:
        """把抽取到的身份信息合并进档案。返回是否有变化。"""
        changed = False
        for key, val in updates.items():
            if key == "likes":
                for v in val:
                    if v not in profile.likes:
                        profile.likes.append(v)
                        changed = True
            elif key == "dislikes":
                for v in val:
                    if v not in profile.dislikes:
                        profile.dislikes.append(v)
                        changed = True
            elif key in ("nickname", "city", "job", "living", "call_name"):
                if val and getattr(profile, key, "") != val:
                    setattr(profile, key, val)
                    changed = True
        return changed


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _date_prefix() -> str:
    return time.strftime("%m月%d日", time.localtime()) + ""


def _clean_item(raw: str) -> str:
    """清洗抽取到的喜好对象，过滤掉无意义片段。"""
    item = raw.strip()
    # 剥掉尾部的语气词/助词（可能叠好几层："美式了" → "美式"）
    tails = ("了", "的", "啊", "吧", "呢", "呀", "嘛", "哦", "咯", "啦")
    for _ in range(2):
        for tail in tails:
            if item.endswith(tail) and len(item) > len(tail) + 1:
                item = item[: -len(tail)]
                break
        else:
            break
    item = item.strip()

    # 太短或空壳词 → 丢弃
    if len(item) < 2:
        return ""
    if item in EMPTY_OBJECTS:
        return ""
    # 全数字/全标点 → 丢弃
    if not re.search(r"[\u4e00-\u9fa5A-Za-z]", item):
        return ""
    return item


def _parse_json_array(raw: str) -> list[dict]:
    """从模型输出里抠出 JSON 数组，容忍 markdown 代码块包裹。"""
    if not raw:
        return []
    text = raw.strip()
    # 去掉 ```json ... ```
    if "```" in text:
        parts = text.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("["):
                text = p
                break
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []
