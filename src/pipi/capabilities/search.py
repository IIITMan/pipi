"""通用联网搜索工具 —— 让皮皮"查得到"（贾维斯化 ①）。

对话出现事实类疑问（什么/为什么/怎么/谁/多少……）时触发：
调免费 Wikipedia API（零 key、零成本、国内可访问）取摘要，
把结果注入 System Prompt，LLM 用皮皮的口吻回答，而不是只能说
"我哪知道，我又出不去"。

设计要点（与 weather.py 同款模式）：
- 关键词触发（意图单一明确，不值得走 LLM function calling）
- 结果缓存 30 分钟
- 查询失败/断网 → 返回 None，静默降级（对话照常，不打断）
- provider 可配（wikipedia 默认；mock 供验证/离线环境）
- 零第三方依赖（标准库 urllib + 线程池）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# 事实类疑问关键词（子串匹配）
_KEYS = (
    "什么", "为什么", "怎么", "哪里", "哪儿", "谁", "多少", "几",
    "是不是", "是否", "啥", "咋", "如何", "哪个", "哪些", "何时",
    "为何", "怎么回事", "是什么意思", "是干什么", "在哪", "有哪些",
    "怎么弄", "怎么办", "能不能", "会不会", "为什么",
    "多高", "多远", "多深", "多重", "多长", "多大", "多快", "多早",
    "多久", "哪年", "哪一年", "哪天",
)
# 明确排除：提醒/天气/时间/问皮皮自己（对应工具或皮皮自身状态就能答，
# 无需联网搜索 —— 否则"星期几/你在干什么"也要拉维基百科，白等好几秒）
_EXCLUDE_WORDS = (
    "天气", "气温", "温度", "多少度", "下雨", "晴天", "阴天", "多云",
    "冷不冷", "热不热", "提醒我", "闹钟", "待办", "帮我记", "记一下",
    "记住", "别忘了", "几点", "星期几", "周几", "几号", "几月",
    "日期", "多少号", "现在时间",
    # 问皮皮自己的状态/行为（答案在心搏与对话上下文里）
    "你在干什么", "你干什么", "你在干嘛", "你干嘛", "你在做什么",
    "你做什么", "你在忙", "你在搞什么", "你怎么样", "你最近怎么样",
    "你在哪", "你吃饭", "你睡觉",
)
# "你/我 开头"的短句（问候/情感问）不搜
_EMOTION_SHORT = re.compile(r"^[你我][^，。！？]{0,12}[？?]?$")
# 句尾撒娇式语气词（"我很忙的，好不好？"）—— 闲聊，不是疑问
_TAIL_MODAL = re.compile(
    r"(好不好|行不行|对不对|是不是啊|要不要啊|有没有搞错|听没听到)"
    r"[？?！!。~～\s]*$")

# Wikipedia 中文站（零 key、国内可访问）
_SEARCH_URL = ("https://zh.wikipedia.org/w/api.php?action=query"
               "&list=search&srlimit=3&format=json&utf8=1")
_EXTRACT_URL = ("https://zh.wikipedia.org/w/api.php?action=query"
                "&prop=extracts&exintro&explaintext&format=json&utf8=1"
                "&redirects=1&exsentences=3")

_UA = {"User-Agent": "pipi-robot/0.1 (companion robot; contact: owner)"}
# Bing 网页搜索需要浏览器 UA（否则可能被反爬拦截）
_UA_BROWSER = {"User-Agent":
               "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0.0.0 Safari/537.36"}


def detect(text: str) -> bool:
    """是否事实类疑问意图（宽松命中即可；LLM 可忽略不适用的结果）。"""
    if not text:
        return False
    t = text.strip()
    if len(t) < 5:
        return False
    if _EMOTION_SHORT.match(t):
        return False
    if _TAIL_MODAL.search(t):
        return False
    if any(w in t for w in _EXCLUDE_WORDS):
        return False
    return any(k in t for k in _KEYS)


def _strip_tags(s: str) -> str:
    """清洗 HTML/标记（Wikipedia snippet / Bing 结果高亮）。"""
    s = re.sub(r"<[^>]+>", "", s or "")
    return (s.replace("&quot;", '"').replace("&#39;", "'")
            .replace("&amp;", "&").replace("&nbsp;", " ")
            .replace("&ensp;", " ").replace("&emsp;", " ")
            .replace("&#0183;", "·").replace("&middot;", "·").strip())


class SearchService:
    """联网搜索（多源链 + 缓存 + 降级）。

    provider：auto（默认，顺序尝试 bing → wikipedia，哪个可用用哪个）
              | bing | wikipedia | mock（供验证/离线）。
    """

    def __init__(self, provider: str = "auto", cache_seconds: int = 1800):
        self.provider = provider
        self.cache_seconds = cache_seconds
        self._cache: tuple[float, str | None] | None = None

    def detect(self, text: str) -> bool:
        """是否事实类疑问意图（orchestrator 在拉搜索结果前先调用）。"""
        return detect(text)

    async def smart_fetch(self, text: str, gate=None) -> str | None:
        """关键词命中后的二次确认 + 联网搜索。

        gate：异步分类器（便宜模型判断是否真需要联网），用于过滤
        '星期几/几点了'这类皮皮自己能答的问题。gate 失败 → 保守照搜。
        """
        if gate is not None:
            try:
                if not await gate(text):
                    return None
            except Exception:
                pass
        return await self.fetch(text)

    # ------------------------------------------------------------------

    def _get_text(self, url: str, timeout: float = 8.0,
                  ua: dict | None = None) -> str:
        """同步 GET 文本（在 asyncio.to_thread 中调用）。"""
        req = urllib.request.Request(url, headers=ua or _UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _get(self, url: str, timeout: float = 8.0) -> dict:
        """同步 GET JSON（在 asyncio.to_thread 中调用）。"""
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    async def _bing_search(self, query: str) -> str | None:
        """Bing 网页搜索（cn.bing.com，零 key、国内可访问）：前 3 条标题+摘要。"""
        url = ("https://cn.bing.com/search?q=" + urllib.parse.quote(query)
               + "&setlang=zh-hans&mkt=zh-CN")
        try:
            html = await asyncio.to_thread(
                self._get_text, url, 5.0, _UA_BROWSER)
        except Exception as exc:
            logger.warning("Bing 搜索失败（%s）：%s", query[:20], exc)
            return None
        parts: list[str] = []
        for li in re.findall(r'<li class="b_algo"[\s\S]*?</li>', html)[:3]:
            m = re.search(r'<h2[^>]*>\s*<a[^>]*>(.*?)</a>', li)
            p = re.search(r'<p[^>]*>(.*?)</p>', li)
            if not m:
                continue
            title = _strip_tags(m.group(1))
            summ = _strip_tags(p.group(1)) if p else ""
            if title:
                parts.append(f"《{title}》{summ[:140]}")
        return "；".join(parts) if parts else None

    async def _wiki_search(self, query: str) -> str | None:
        """Wikipedia 搜索 → 前 2 条 snippet。"""
        q = urllib.parse.urlencode({"srsearch": query, "srprop": "snippet"})
        try:
            data = await asyncio.to_thread(
                self._get, f"{_SEARCH_URL}&{q}")
        except Exception as exc:
            logger.warning("Wikipedia 搜索失败（%s）：%s", query[:20], exc)
            return None
        hits = (data.get("query") or {}).get("search") or []
        if not hits:
            return None
        # 用 search snippet 直接拼（免二次请求，够回答用）
        parts = [f"《{h.get('title')}》{_strip_tags(h.get('snippet', ''))}"
                 for h in hits[:2]]
        return "；".join(parts) if parts else None

    async def fetch(self, query: str) -> str | None:
        """查一次（带缓存）。失败返回 None → 对话不注入，照常回答。"""
        now = time.time()
        if self._cache is not None and now - self._cache[0] < self.cache_seconds:
            return self._cache[1]
        brief = None
        if self.provider == "mock":
            brief = f"[mock] {query} 的相关资料：皮皮还没有联网，这是占位摘要。"
        else:
            chain = (["bing", "wikipedia"] if self.provider == "auto"
                     else [self.provider])
            for p in chain:
                fn = self._bing_search if p == "bing" else self._wiki_search
                brief = await fn(query)
                if brief:
                    break
        self._cache = (now, brief)
        return brief


__all__ = ["SearchService", "detect"]
