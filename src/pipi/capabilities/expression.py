"""输出后处理 —— 把 LLM 的原始输出"人格化"。

两道保险（来自《皮皮人格Prompt方案》）：
  1. Markdown 清洗：去掉模型顽固输出的 **、###、- 列表
  2. 长度熔断：超过句长上限就按句切分或截断

以及把长回复拆成 1~3 条气泡，模拟微信连发的节奏感。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pipi.domain.dialogue import Bubble
from pipi.domain.persona import SpeechStyle


# Markdown 清洗规则
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_HEADING = re.compile(r"^#{1,6}\s*", re.M)
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)
_MD_NUMBER = re.compile(r"^\s*\d+[.、)]\s+", re.M)
_MD_CODE = re.compile(r"`{1,3}(.+?)`{1,3}", re.S)
_MD_LINK = re.compile(r"\[(.+?)\]\(.+?\)")
_MD_HR = re.compile(r"^\s*[-*_]{3,}\s*$", re.M)

# AI 腔套话（出现就删）
AI_PHRASES = [
    "作为一个人工智能", "作为一个AI", "作为AI助手",
    "希望对你有所帮助", "希望能帮到你", "还有什么可以帮你的",
    "我理解你的感受", "我明白你的感受",
    "首先，", "其次，", "最后，", "总的来说，", "综上所述，",
    "需要注意的是", "需要提醒的是", "值得一提的是",
]

# 句末标点（用于切分气泡）
_SENT_END = re.compile(r"(?<=[。！？!?…；;])")


def strip_markdown(text: str) -> str:
    """去掉 Markdown 痕迹 —— 聊天窗口里不该出现这些。"""
    if not text:
        return ""
    t = text
    t = _MD_CODE.sub(r"\1", t)
    t = _MD_BOLD.sub(r"\1", t)
    t = _MD_ITALIC.sub(r"\1", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _MD_HEADING.sub("", t)
    t = _MD_HR.sub("", t)
    t = _MD_BULLET.sub("", t)
    t = _MD_NUMBER.sub("", t)
    return t.strip()


def strip_ai_phrases(text: str) -> str:
    """删掉暴露 AI 身份的套话。"""
    t = text
    for phrase in AI_PHRASES:
        t = t.replace(phrase, "")
    # 清理因删除产生的开头空白和多余标点
    t = re.sub(r"^[，。、\s]+", "", t)
    t = re.sub(r"[，]{2,}", "，", t)
    return t.strip()


def enforce_length(text: str, max_chars: int) -> str:
    """长度熔断：单句超长则截断到最近的句末标点。"""
    if len(text) <= max_chars:
        return text
    # 在 max_chars 附近找句末标点
    window = text[: max_chars + 8]
    for i in range(len(window) - 1, max(0, max_chars - 12), -1):
        if window[i] in "。！？!?…":
            return window[: i + 1]
    # 找不到就在硬上限截断，补个省略号
    return text[:max_chars].rstrip("，、") + "…"


def split_bubbles(
    text: str,
    style: SpeechStyle | None = None,
    *,
    max_bubbles: int = 3,
    max_chars: int = 60,
) -> list[str]:
    """拆成多条气泡 —— 模拟"连发微信"的节奏。"""
    if style:
        max_bubbles = style.max_bubbles
        max_chars = style.max_sentence_chars * 2

    t = (text or "").strip()
    if not t:
        return []

    # 短回复直接一条
    if len(t) <= max_chars:
        return [t]

    # 按句末标点切
    parts = [p.strip() for p in _SENT_END.split(t) if p.strip()]

    # 合并过短的片段
    merged: list[str] = []
    for p in parts:
        if merged and len(merged[-1]) + len(p) <= max_chars:
            merged[-1] += p
        else:
            merged.append(p)

    # 超长片段单独熔断
    out: list[str] = []
    for m in merged:
        if len(m) > max_chars:
            out.extend(_hard_split(m, max_chars))
        else:
            out.append(m)

    # 超过气泡上限则合并尾部
    if len(out) > max_bubbles:
        head = out[: max_bubbles - 1]
        tail = "".join(out[max_bubbles - 1:])
        head.append(tail)
        out = head

    return out


def _hard_split(text: str, max_chars: int) -> list[str]:
    """无标点长文本的硬切分。"""
    out = []
    while len(text) > max_chars:
        out.append(text[:max_chars])
        text = text[max_chars:]
    if text:
        out.append(text)
    return out


@dataclass
class PostProcessResult:
    """后处理结果。"""

    raw: str
    text: str
    bubbles: list[str]
    modified: bool = False
    reasons: list[str] = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []


def postprocess(
    raw: str,
    style: SpeechStyle | None = None,
    *,
    emotion: str = "neutral",
) -> PostProcessResult:
    """完整后处理流水线。"""
    reasons: list[str] = []
    text = raw or ""

    cleaned = strip_markdown(text)
    if cleaned != text:
        reasons.append("去markdown")
    text = cleaned

    stripped = strip_ai_phrases(text)
    if stripped != text:
        reasons.append("去AI腔")
    text = stripped

    if style:
        if len(text) > style.max_sentence_chars * style.max_bubbles:
            text = enforce_length(text, style.max_sentence_chars * style.max_bubbles)
            reasons.append("长度熔断")

    bubbles = split_bubbles(text, style)

    return PostProcessResult(
        raw=raw,
        text=text,
        bubbles=bubbles,
        modified=bool(reasons),
        reasons=reasons,
    )


def to_bubbles(
    result: PostProcessResult,
    style: SpeechStyle | None = None,
    emotion: str = "neutral",
) -> list[Bubble]:
    """转成 Bubble 对象，带拟人延迟。"""
    bubbles: list[Bubble] = []
    for i, text in enumerate(result.bubbles):
        # 第一条立刻发，后续按长度给一点延迟（像真人在打字）
        delay = 0 if i == 0 else min(1200, 180 + len(text) * 28)
        bubbles.append(Bubble(text=text, delay_ms=delay, emotion=emotion))
    return bubbles
