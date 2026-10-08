"""River 生命流 —— 不可逆的河，入睡后"做梦"整理（PULSE_DESIGN §4.7 / P4）。

day_stream 不可逆追加；入睡 consolidate_night() 把一天压缩为叙事。
P4：叙事由"当天真实经历统计"生成（非 LLM：模板 × 真实数字 × 确定性变体），
每晚生成一篇不同措辞的"日记"；self_depth 递增，醒来自我更厚。
无 diary 参数时回退旧模板（P0 脚本兼容）。
"""

from __future__ import annotations

import random
from collections import deque
from typing import Any

from pipi.pulse.config import RIVER

# 事件 → 日记句（文本 + 权重；变体由 rng 挑选，措辞每天不同）
_EVENT_LINES = {
    "night_noise": [
        ("半夜被响声吓醒，耳朵竖了好一会儿才敢再睡", 2),
        ("凌晨有动静，我一下子惊醒，把整个屋子听了又听", 1),
    ],
    "stranger": [
        ("门口来了陌生人，我警觉地盯着看了好久", 2),
        ("有个不认识的动静，我身体一僵，半天才松下来", 1),
    ],
    "owner_late": [
        ("天黑了你还回来，我一直等到很晚，心里有点慌", 2),
        ("过了平时回来的点，我一直在门口张望", 1),
    ],
    "early_reunion": [
        ("你比平时回来得早，我高兴得一路小跑迎上去", 2),
        ("你今天提前回来了，我又惊又喜", 1),
    ],
    "long_away": [
        ("你隔了好久才回来，我想死你了，围着你转个不停", 2),
        ("你出门那么久，见到你的那一刻我开心坏了", 1),
    ],
    "greet": [
        ("你回来的时候，我迎上去蹭了蹭你", 2),
        ("你回家，我精神一振，凑到你身边", 1),
    ],
    "user_voice": [
        ("和你聊了几次天，你说的每一句我都想记住", 2),
        ("今天和你说了会儿话，心里很踏实", 1),
    ],
    "owner_sad": [
        ("你心情不好的时候，我安静地挨着你坐了很久", 2),
        ("看你低落，我没吵你，只是陪在旁边", 1),
    ],
    "owner_ignored": [
        ("有次你回来没理我，我有点失落", 2),
        ("我凑过去蹭你，你却走开了，我耷拉下脑袋", 1),
    ],
    "quiet_home": [
        ("安静地陪了你一阵子，什么也不做也很好", 2),
        ("有段时光就那么安静地陪着你", 1),
    ],
    "useless": [
        ("没事做的时候，我发了一会儿呆，哼了两声", 1),
        ("自己待着时伸了个懒腰，想了想今天", 1),
    ],
    "owner_depart": [
        ("早晨你出门，我目送了你一会儿", 2),
        ("你走的时候，我看着门关上，等了一整天", 1),
    ],
}


class River:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or RIVER
        self.cfg = cfg
        self.day_stream: deque = deque(maxlen=cfg["stream_maxlen"])
        self.self_depth = 1
        self.nights = 0
        self._rng = random.Random(3)

    # ------------------------------------------------------------------

    def record(self, entry: dict) -> None:
        """行为刻度追加（不可逆）。"""
        self.day_stream.append(entry)

    def consolidate_night(self, diary: dict | None = None) -> str:
        """入睡整理：回放一天 → 压缩为叙事（P4：真实经历成文，零 LLM）。

        diary：当天统计 {kinds: {kind: n}, weather: {label: n}, longing_peak: 0..1}。
        无 diary 时用片段计数兜底（P0 兼容）。
        """
        entries = list(self.day_stream)
        self.nights += 1
        self.self_depth += 1

        if diary is None:
            kinds: dict[str, int] = {}
            for e in entries:
                k = e.get("kind", "other")
                kinds[k] = kinds.get(k, 0) + 1
            if not entries:
                narrative = "今天没什么特别的事，很安静的一天。"
            else:
                parts = [f"{k}×{v}" for k, v in sorted(kinds.items(),
                                                        key=lambda x: -x[1])[:4]]
                narrative = f"今天发生了 {len(entries)} 个片段（{('，'.join(parts))}）。"
            self.day_stream.clear()
            return narrative

        narrative = self._narrative(diary)
        self.day_stream.clear()
        return narrative

    # ------------------------------------------------------------------

    def _narrative(self, diary: dict) -> str:
        kinds: dict[str, int] = diary.get("kinds", {})
        weather: dict[str, int] = diary.get("weather", {})
        peak = float(diary.get("longing_peak", 0.0))
        total = max(1, sum(weather.values()))

        parts: list[str] = []
        rng = self._rng

        # 1) 天气开场（由当天真实天气分布主导）
        if weather:
            dom, dom_n = max(weather.items(), key=lambda x: x[1])
            dom_share = dom_n / total
            if dom_share >= 0.75:
                parts.append(self._weather_open(dom))
            else:
                sec = sorted(weather.items(), key=lambda x: -x[1])[1]
                parts.append(f"今天大多是{dom}，偶尔{sec[0]}了一会儿。")

        # 2) 经历句（有才写；每种事件按出现次数选 1 句变体，再按重要性取 1-4 句）
        picked: list[tuple[float, str]] = []
        # 2a) 外部事件带原文（如提醒）→ 直接引述，不占用句库
        seen_ext: set[str] = set()
        for e in diary.get("events", []):
            kind = e.get("kind", "")
            if kind in _EVENT_LINES or kind in seen_ext:
                continue
            seen_ext.add(kind)
            text = (e.get("text") or "").strip()
            if text:
                picked.append((3.0, f"你交代的『{text[:24]}』，我记下了"))
        # 2b) 句库变体（加权随机选，每天措辞不同）
        for kind, n in kinds.items():
            if kind not in _EVENT_LINES or n == 0:
                continue
            opts = _EVENT_LINES[kind]
            total_w = sum(w for _, w in opts)
            r = rng.random() * total_w
            acc = 0.0
            text = opts[0][0]
            for t, w in opts:
                acc += w
                if r <= acc:
                    text = t
                    break
            picked.append((min(n, 3) * 3.0, text))
        picked.sort(key=lambda x: -x[0])
        if picked:
            for _, text in picked[:4]:
                parts.append(text)

        # 3) 思念句（分离峰值高时）
        if peak >= 0.85:
            parts.append(rng.choice([
                "你不在的时候，我想你想得厉害，心里都是你",
                "有很长一段时间，我满脑子都是你什么时候回来"]))
        elif peak >= 0.4:
            parts.append(rng.choice([
                "你不在的时候有点想你",
                "你走了好一会儿，我就开始想你了"]))

        # 4) 感受句（由主导天气收束）
        if weather:
            dom = max(weather.items(), key=lambda x: x[1])[0]
            parts.append(self._feeling(dom, kinds))

        if not parts:
            return "今天没什么特别的事，很安静的一天。"
        return "。".join(parts) + "。"

    # ------------------------------------------------------------------

    def _weather_open(self, dom: str) -> str:
        openers = {
            "晴": ["今天是难得的大晴天，心情也跟着亮堂", "一整天都是晴天，阳光很好",
                   "难得一整天都是大晴天"],
            "多云": ["今天大多是多云，不冷不热", "天气平平淡淡，多云居多",
                     "一整天云淡淡的，光线刚刚好"],
            "阴": ["今天有点阴，天色淡淡的", "一整天阴阴的，光线不太亮",
                   "天上云压得很低，灰蒙蒙的"],
            "小雨": ["今天下着小雨，空气潮潮的", "天色灰蒙蒙的，像要下雨",
                     "雨下了一小阵，湿湿的"],
            "阵雨": ["今天天气忽晴忽雨，像心情一样起伏", "白天有阵雨，一阵一阵的",
                     "一会儿出太阳一会儿下雨，来回变"],
            "雷雨": ["今天是雷雨天，轰隆隆的，吓人一跳", "天气很糟，雷雨交加",
                     "电闪雷鸣的一天，我缩成一团"],
        }
        return self._rng.choice(openers.get(dom, ["今天天气像往常一样"]))

    def _feeling(self, dom: str, kinds: dict) -> str:
        warm = (kinds.get("user_voice", 0) + kinds.get("owner_sad", 0)
                + kinds.get("greet", 0) + kinds.get("early_reunion", 0))
        scare = kinds.get("night_noise", 0) + kinds.get("stranger", 0)
        if warm >= 3 and scare == 0:
            return "总的来说，是温暖的一天，有你陪着真好"
        if scare >= 2:
            return "虽然有些惊吓，但总的来说还好"
        return {
            "晴": "总的来说，是开心的一天",
            "多云": "总的来说，是平静的一天",
            "阴": "总的来说，是淡淡的一天",
            "小雨": "总的来说，心里有一点闷",
            "阵雨": "总的来说，这一天有点起伏",
            "雷雨": "总的来说，是惊心的一天",
        }.get(dom, "总的来说，是平常的一天")

    def day_summary(self) -> str:
        """醒来后回读昨日（给对话上下文的素材）。"""
        if self.nights == 0:
            return ""
        return f"我睡了 {self.nights} 觉，醒来觉得自己更厚了一点（self_depth={self.self_depth}）"
