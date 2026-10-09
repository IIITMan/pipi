"""Expression —— 身体/动作表达端口（硬件接入预留）。

PULSE 心脏每帧算出身体状态（灯/眼/姿态/动作）与行为意图（靠近/退后/看向/
探索）。在没有硬件时，这些只推给监控台展示；硬件就绪后，实现一个
`ActionSink` 并注册给 PulseRuntime，即可把同一帧状态驱动到灯、屏幕表情、
姿态与底盘——心脏与解耦，不改动任何生命逻辑。

后期硬件开发只需：
    class MyRobotSink:
        def apply(self, body, behavior):
            # body:  lamp/eye/pose/act
            # behavior: motion/ target/ zh
            ...
    runtime.action_sink = MyRobotSink()
"""

from __future__ import annotations

from typing import Any, Protocol

# 意图 kind → 底盘/行为原语（motion）与中文表现
# motion 原语（后期底盘/舵机消费）：
#   approach 靠近目标 / retreat 退后 / look 转头看向 / explore 前去查看 / idle 不动
_BEHAVIOR_MAP = {
    "attachment": ("approach", "owner", "朝主人凑过去，想靠着他"),
    "loneliness": ("look", "door", "往门口的方向看，盼着主人回来"),
    "curiosity": ("explore", "sound", "竖起耳朵，想去动静那边看看"),
    "security": ("retreat", "threat", "有点警觉，往后缩了缩"),
    "sleepiness": ("idle", "none", "犯困，原地不动，慢慢歇着"),
    "useless": ("idle", "none", "没什么事，自顾自发呆"),
    "abandoned": ("idle", "none", "刚想做什么又算了，原地发愣"),
}


def behavior_from_intent(intent: Any, owner_present: bool) -> dict:
    """把 Will 意图映射为可执行行为（无意图 → idle）。"""
    if intent is None:
        return {"motion": "idle", "target": "none", "zh": "安静地待着"}
    motion, target, zh = _BEHAVIOR_MAP.get(
        intent.kind, ("idle", "none", "安静地待着"))
    # 主人不在时 "approach owner" 无意义 → 退化为看向门口
    if motion == "approach" and not owner_present:
        motion, target, zh = "look", "door", "主人不在，往门口望了望"
    return {"motion": motion, "target": target, "zh": zh}


class ActionSink(Protocol):
    """硬件表达端口协议（灯/眼/姿态/底盘）。"""

    def apply(self, body: dict, behavior: dict) -> None:
        ...


class NullActionSink:
    """空端口：什么也不做（默认；监控仍能看到状态，因为走 snapshot）。
    也可把它换成"只记录到日志"的实现，便于离线调试。"""

    def __init__(self, log: bool = False):
        self.log = log
        self.last: tuple[dict, dict] | None = None

    def apply(self, body: dict, behavior: dict) -> None:
        self.last = (body, behavior)
