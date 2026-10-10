"""天气工具 —— 让皮皮"查得到"外面。

对话出现天气意图（天气/温度/下雨/冷/热…）时触发：调 UAPIs 免费天气 API
（国内直连 ~0.1s、免 key、零成本）取实时天气，把结果注入 System Prompt，
LLM 再用皮皮的口吻回答，而不是只会说"我又出不去"。

设计要点：
- 单一数据源，不做兜底链（国内直连稳定；失败返回 None 静默降级，
  orchestrator 会告知 LLM 诚实回复"查不到"）
- 结果缓存 30 分钟：同一小时多次追问不重复请求
- 城市可配置（默认济南）；零第三方依赖（标准库 urllib + 线程池）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# 天气意图关键词（子串匹配）
_KEYS = (
    "天气", "气温", "温度", "多少度", "会不会下雨", "下不下雨",
    "下雨吗", "有雨吗", "晴天", "阴天", "多云", "降温", "升温",
    "冷不冷", "热不热", "适合出门", "穿什么",
)
# 单字词必须带程度/语气才触发（防"冷静""别冷场"等误触）
_KEYS2 = (
    "好冷", "太冷", "真冷", "冷死了", "冷得", "冷啊", "冷吗", "冷么",
    "好热", "太热", "真热", "热死了", "热吗", "热么",
    "刮风", "大风", "风大", "晒不晒", "凉快", "暖和", "闷热",
)

# 类别 → 中文（监控/内心注入展示）
KIND_ZH = {
    "sunny": "晴", "cloudy": "多云", "overcast": "阴", "fog": "雾",
    "rain": "雨", "snow": "雪", "thunder": "雷雨",
}


def detect(text: str) -> bool:
    """是否天气意图。"""
    if not text:
        return False
    if any(k in text for k in _KEYS):
        return True
    return any(k in text for k in _KEYS2)   # 程度/语气词组（防单字误触）


class WeatherService:
    """天气查询服务（UAPIs 单源 + 30 分钟缓存）。"""

    _UAPI_URL = "https://uapis.cn/api/v1/misc/weather"
    _UA = {"User-Agent": "pipi-robot/0.1"}

    def __init__(self, city: str = "济南", cache_seconds: int = 1800):
        self.city = city
        self.cache_seconds = cache_seconds
        self._caches: dict[str, tuple[float, str | None]] = {}   # 按城市分缓存
        self._kind: str | None = None        # 最近一次成功查询的粗类别
        self._last_uapis_weather: str = ""   # 最近一次的天气文字（粗分类用）
        self._last_notified: str | None = None
        self._subscribers: list = []

    # ------------------------------------------------------------------

    @staticmethod
    def detect(text: str) -> bool:
        """是否天气意图（委托模块级 detect，orchestrator 在实例上调用）。"""
        return detect(text)

    # ------------------------------------------------------------------

    def subscribe(self, fn) -> None:
        """登记订阅者（fn(kind)）。对话查询成功且类别变化时被通知，
        供生命模块同步（如 PULSE 心境调制）。"""
        if fn not in self._subscribers:
            self._subscribers.append(fn)

    def kind(self) -> str | None:
        """最近一次成功查询的粗类别（sunny/cloudy/fog/rain/snow/windy）。"""
        return self._kind

    def _notify(self, kind: str) -> None:
        """类别与上次通知不同才广播（避免缓存期内重复推同一类别）。"""
        if kind == self._last_notified:
            return
        self._last_notified = kind
        for fn in list(self._subscribers):
            try:
                fn(kind)
            except Exception as exc:
                logger.debug("天气订阅者回调失败：%s", exc)

    def _get(self, url: str, timeout: float = 6.0) -> dict:
        """同步 GET（在 asyncio.to_thread 中调用）。"""
        req = urllib.request.Request(url, headers=self._UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ------------------------------------------------------------------

    async def fetch(self, city: str | None = None) -> str | None:
        """查一次（按城市缓存 30 分钟）。失败返回 None → 对话不注入。

        city=None 用配置的默认城市；查外地（"北京天气"）传城市名 —— UAPIs
        天然支持任意城市，按城市分缓存。
        成功时同步粗类别（_kind）并通知订阅者（类别变化才推）——
        生命模块据此调制心境。
        """
        now = time.time()
        city = (city or self.city).strip() or self.city
        cache = self._caches.get(city)
        if cache is not None and now - cache[0] < self.cache_seconds:
            return cache[1]
        text = await self._fetch_uapis(city)
        if text is None:
            self._caches[city] = (now, None)
            return None
        self._caches[city] = (now, text)
        kind = self._kind_uapis()
        self._kind = kind or self._kind
        if kind and city == self.city:   # 只有主城市天气调制心境
            self._notify(kind)
        return text

    async def _fetch_uapis(self, city: str) -> str | None:
        """UAPIs 天气（城市名直查，国内直连 ~0.1s，免费无 key）。"""
        params = {"city": city, "extended": "true", "forecast": "true"}
        url = f"{self._UAPI_URL}?{urllib.parse.urlencode(params)}"
        try:
            data = await asyncio.to_thread(self._get, url, 6.0)
        except Exception as exc:
            logger.warning("UAPIs 天气查询失败：%s", exc)
            return None
        try:
            self._last_uapis_weather = str(data.get("weather") or "")
            return self._render_uapis(data, city)
        except Exception as exc:
            logger.warning("UAPIs 天气渲染失败：%s", exc)
            return None

    def _render_uapis(self, d: dict, city: str) -> str:
        parts = [f"{city}今天：{d.get('weather', '未知')}"]
        tmax, tmin = d.get("temp_max"), d.get("temp_min")
        if tmax is not None and tmin is not None:
            parts.append(f"{tmin:.0f}~{tmax:.0f}°C")
        t = d.get("temperature")
        if t is not None:
            parts.append(f"现在 {t:.0f}°C")
        fl = d.get("feels_like")
        if fl is not None and t is not None and abs(fl - t) > 1:
            parts.append(f"体感 {fl:.0f}°C")
        h = d.get("humidity")
        if h is not None:
            parts.append(f"湿度 {h:.0f}%")
        wd = d.get("wind_direction")
        if wd:
            parts.append(f"{wd}{d.get('wind_power') or ''}")
        aqi = d.get("aqi_category")
        if aqi:
            parts.append(f"空气{aqi}")
        return "，".join(parts)

    def _kind_uapis(self) -> str | None:
        """从 UAPIs 响应文字粗分类（晴/雨/雪/风/雾），供生命模块调制。"""
        text = self._last_uapis_weather or ""
        if not text:
            return None
        if "雪" in text:
            return "snow"
        if "雨" in text:
            return "rain"
        if "雾" in text or "霾" in text:
            return "fog"
        if "风" in text and "级" in text and any(
                c in text for c in "六七八九十"):
            return "windy"
        if "晴" in text:
            return "sunny"
        if "阴" in text or "云" in text:
            return "cloudy"
        return None
