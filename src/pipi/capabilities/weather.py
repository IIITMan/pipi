"""天气工具 —— 让皮皮"查得到"外面。

对话出现天气意图（天气/温度/下雨/冷/热…）时触发：调免费 Open-Meteo API
（免 key、零成本，符合项目成本约束）取实时天气，把结果注入 System Prompt，
LLM 再用皮皮的口吻回答，而不是只会说"我又出不去"。

设计要点：
- 关键词触发（意图单一明确，不值得走 LLM function calling）
- 结果缓存 30 分钟：同一小时多次追问不重复请求
- 查询失败/断网 → 返回 None，静默降级（对话照常，不打断）
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

# WMO 天气代码 → 中文
_WMO = {
    0: "晴", 1: "基本晴", 2: "多云间晴", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷雨", 96: "雷雨伴冰雹", 99: "雷雨伴强冰雹",
}

# WMO 代码 → 粗类别（喂给生命模块，类别变化才触发心境调制）
_WMO_KIND = {
    0: "sunny", 1: "sunny", 2: "cloudy", 3: "overcast",
    45: "fog", 48: "fog",
    51: "rain", 53: "rain", 55: "rain",
    56: "rain", 57: "rain",
    61: "rain", 63: "rain", 65: "rain",
    66: "rain", 67: "rain",
    71: "snow", 73: "snow", 75: "snow", 77: "snow",
    80: "rain", 81: "rain", 82: "rain",
    85: "snow", 86: "snow",
    95: "thunder", 96: "thunder", 99: "thunder",
}

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
    """天气查询服务（带缓存与降级）。"""

    GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FC_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, city: str = "济南", cache_seconds: int = 1800):
        self.city = city
        self.cache_seconds = cache_seconds
        self._cache: tuple[float, str | None] | None = None
        self._kind: str | None = None        # 最近一次成功查询的粗类别
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
        """最近一次成功查询的粗类别（sunny/cloudy/overcast/fog/rain/snow/thunder）。"""
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
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    async def _locate(self) -> tuple[float, float, str] | None:
        """城市 → 经纬度（geocoding）。失败返回 None。"""
        q = urllib.parse.urlencode({
            "name": self.city, "count": 1, "language": "zh", "format": "json"})
        try:
            data = await asyncio.to_thread(self._get, f"{self.GEO_URL}?{q}")
        except Exception as exc:
            logger.warning("天气定位失败（%s）：%s", self.city, exc)
            return None
        results = data.get("results") or []
        if not results:
            return None
        r = results[0]
        return r["latitude"], r["longitude"], r.get("name", self.city)

    async def _forecast(self, lat: float, lon: float) -> dict | None:
        params = {
            "latitude": lat, "longitude": lon,
            "current": ("temperature_2m,apparent_temperature,"
                        "relative_humidity_2m,weather_code,wind_speed_10m,"
                        "wind_direction_10m"),
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "forecast_days": 2,
            "timezone": "auto",
        }
        url = f"{self.FC_URL}?{urllib.parse.urlencode(params)}"
        try:
            return await asyncio.to_thread(self._get, url)
        except Exception as exc:
            logger.warning("天气查询失败：%s", exc)
            return None

    @staticmethod
    def _wind_dir(deg: float | None) -> str:
        if deg is None:
            return ""
        names = ["北风", "东北风", "东风", "东南风",
                 "南风", "西南风", "西风", "西北风"]
        return names[int((deg + 22.5) // 45) % 8]

    def _render(self, data: dict, name: str) -> str:
        cur = data.get("current") or {}
        daily = (data.get("daily") or {})
        d_max = (daily.get("temperature_2m_max") or [None])[0]
        d_min = (daily.get("temperature_2m_min") or [None])[0]
        d_wmo = (daily.get("weather_code") or [None])[0]
        t = cur.get("temperature_2m")
        now_desc = _WMO.get(cur.get("weather_code"), "未知")
        today_desc = _WMO.get(d_wmo, now_desc)
        unit = (data.get("current_units") or {}).get("temperature_2m", "°C")
        parts = [f"{name}今天：{today_desc}"]
        if d_min is not None and d_max is not None:
            parts.append(f"{d_min:.0f}~{d_max:.0f}{unit}")
        if t is not None:
            parts.append(f"现在 {t:.0f}{unit}")
            ap = cur.get("apparent_temperature")
            if ap is not None and abs(ap - t) > 1:
                parts.append(f"体感 {ap:.0f}{unit}")
        h = cur.get("relative_humidity_2m")
        if h is not None:
            parts.append(f"湿度 {h:.0f}%")
        w = cur.get("wind_speed_10m")
        if w is not None:
            parts.append(f"{self._wind_dir(cur.get('wind_direction_10m'))}"
                         f"{w:.0f}km/h")
        return "，".join(parts)

    # ------------------------------------------------------------------

    async def fetch(self) -> str | None:
        """查一次（带 30 分钟缓存）。失败返回 None → 对话不注入，照常回答。

        成功时同步粗类别（_kind）并通知订阅者（类别变化才推）——
        生命模块据此调制心境。
        """
        now = time.time()
        if self._cache is not None and now - self._cache[0] < self.cache_seconds:
            return self._cache[1]
        loc = await self._locate()
        if loc is None:
            self._cache = (now, None)
            return None
        lat, lon, name = loc
        data = await self._forecast(lat, lon)
        if data is None:
            self._cache = (now, None)
            return None
        text = self._render(data, name)
        self._cache = (now, text)
        cur = (data.get("current") or {})
        code = cur.get("weather_code")
        kind = _WMO_KIND.get(code) if code is not None else None
        self._kind = kind or self._kind
        if kind:
            self._notify(kind)
        return text
