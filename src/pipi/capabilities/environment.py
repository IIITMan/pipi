"""环境档案 —— 贾维斯化③：皮皮对"家"的持续认知。

不是数据库，是一本越养越厚的"小本本"（JSON 落盘，零依赖）：
  - 人物：主人作息（几点回家是学的，不是写死的）、家人/熟人称呼
  - 物品 / 地点：对话里反复提到的，记下次数与最近时间
  - 现场：主人在不在、今天有几次陌生人
  - 世界：外面天气、今天定过的提醒

来源全部是"观察 + 顺带记录"（对话/视觉/天气/提醒），不主动打扰；
输出给监控 HUD 一张"家的认知"卡片，并留扩展口（预警上下文/主动行为）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 对话词表（克制：只认常见词，避免误抓；命中记次数+最近时间）
_PLACES = ("书房", "卧室", "客厅", "厨房", "阳台", "门口", "卫生间",
           "浴室", "楼上", "楼下", "车库", "花园")
_ITEMS = ("猫", "狗", "鱼", "鸟", "玩具", "钥匙", "手机", "水杯",
          "药", "快递", "外卖", "零食", "花", "绿植")
_PEOPLE = ("老婆", "媳妇", "老公", "女朋友", "男朋友", "爸爸", "妈妈",
           "爸", "妈", "奶奶", "爷爷", "姥姥", "姥爷", "孩子", "儿子",
           "女儿", "宝宝", "兄弟", "朋友", "同事", "邻居")


class EnvironmentMemory:
    """家的认知档案（JSON 落盘；任何失败都不影响主流程）。"""

    def __init__(self, data_dir: str | Path = "data",
                 cfg: dict | None = None):
        cfg = cfg or {}
        self.path = Path(data_dir) / "environment.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 能力在线状态（装配事实，供 HUD 显示 ✓/—）
        self.online = {
            "search": bool(cfg.get("search", {}).get("enabled", True)),
            "alerts": bool(cfg.get("alerts", {}).get("enabled", True)),
            "weather": bool(cfg.get("weather", {}).get("enabled", True)),
        }
        self.max_arrivals = int(cfg.get("env", {}).get("max_arrivals", 14))
        self._data = self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "places": {}, "items": {}, "people": {},
                "presence": {"stranger_today": 0, "last_stranger_t": 0},
                "owner": {"present": False, "arrivals": [], "last_t": 0},
                "weather": None, "reminders": [],
            }

    def _save(self) -> None:
        try:
            self._data["updated_at"] = round(time.time(), 1)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception as exc:
            logger.debug("环境档案落盘失败（不影响使用）：%s", exc)

    # ------------------------------------------------------------------
    # 观察入口（全部 try 包裹，由调用方负责不阻塞主流程）
    # ------------------------------------------------------------------

    def observe_chat(self, text: str) -> None:
        """对话中提到的地点/物品/人称 → 记账（次数+最近时间）。"""
        if not text:
            return
        changed = False
        for grp, words in (("places", _PLACES), ("items", _ITEMS),
                           ("people", _PEOPLE)):
            for w in words:
                if w in text:
                    rec = self._data[grp].setdefault(
                        w, {"count": 0, "last_t": 0})
                    rec["count"] += 1
                    rec["last_t"] = round(time.time(), 1)
                    changed = True
        if changed:
            self._save()

    def observe_vision(self, event: Any = None) -> None:
        """视觉事件：主人回家/离开、陌生人出现。"""
        kind = getattr(event, "kind", None)
        now = round(time.time(), 1)
        owner = self._data["owner"]
        pres = self._data["presence"]
        if kind == "owner_arrived":
            owner["present"] = True
            owner["last_t"] = now
            # 作息学习：记录回家钟点（0-23），保留最近 N 次
            hour = time.localtime(now).tm_hour
            arr = owner.setdefault("arrivals", [])
            arr.append(hour)
            del arr[:-self.max_arrivals]
            self._save()
        elif kind == "owner_left":
            owner["present"] = False
            owner["last_t"] = now
            self._save()
        elif kind == "stranger":
            pres["stranger_today"] = pres.get("stranger_today", 0) + 1
            pres["last_stranger_t"] = now
            self._save()

    def observe_weather(self, kind: str, zh: str = "") -> None:
        """外面天气（类别变化时记录）。"""
        if not kind:
            return
        self._data["weather"] = {"kind": kind, "zh": zh,
                                 "t": round(time.time(), 1)}
        self._save()

    def observe_reminder(self, text: str) -> None:
        """今日提醒摘要（保留最近 4 条）。"""
        if not text:
            return
        rec = {"text": text[:40], "t": round(time.time(), 1)}
        lst = self._data.setdefault("reminders", [])
        if lst and lst[-1].get("text") == text:
            return                      # 同一条不重复记
        lst.append(rec)
        del lst[:-4]
        self._save()

    # ------------------------------------------------------------------
    # 查询（HUD / 预警上下文 / 主动行为预留）
    # ------------------------------------------------------------------

    def home_hour_expected(self) -> int | None:
        """主人平均回家钟点（学到才说；无数据 → None）。"""
        arr = self._data.get("owner", {}).get("arrivals", [])
        if not arr:
            return None
        return round(sum(arr) / len(arr))

    def facts_brief(self) -> str:
        """一句话"家的认知"（供预警/主动行为上下文，克制注入）。"""
        owner = self._data.get("owner", {})
        parts: list[str] = []
        hh = self.home_hour_expected()
        if hh is not None:
            parts.append(f"主人一般{hh}点左右回来")
        pres = self._data.get("presence", {})
        if pres.get("stranger_today", 0) >= 2:
            parts.append("今天有不认识的人出现过")
        w = self._data.get("weather")
        if w and w.get("zh"):
            parts.append(f"外面是{w['zh']}")
        return "；".join(parts) if parts else ""

    def snapshot(self) -> dict:
        """HUD 渲染用：结构固定、全部有默认值。"""
        d = self._data
        owner = d.get("owner", {})
        pres = d.get("presence", {})
        w = d.get("weather")
        top = (lambda g: [f"{k}×{v['count']}" for k, v in
                          sorted(g.items(), key=lambda x: -x[1]["count"])[:4]])
        return {
            "owner_present": owner.get("present", False),
            "owner_home_hour": self.home_hour_expected(),
            "owner_arrivals_today": len(owner.get("arrivals", [])),
            "stranger_today": pres.get("stranger_today", 0),
            "places": top(d.get("places", {})),
            "items": top(d.get("items", {})),
            "people": top(d.get("people", {})),
            "weather": {"zh": (w or {}).get("zh", ""),
                        "t": (w or {}).get("t", 0)} if w else None,
            "reminders": d.get("reminders", []),
            "online": dict(self.online),
        }


__all__ = ["EnvironmentMemory"]
