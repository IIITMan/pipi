"""Body —— 身体表达层（PULSE_DESIGN §7 body.py）。

内部状态 → 可观察的身体：呼吸灯亮度、眼睛状态、姿态、自发动作。
P1 只有文本/数值表达（不接真实硬件）；生产由 action 层消费。
"""

from __future__ import annotations

from typing import Any

USELESS_ACT = {
    "stretch": "伸了个懒腰",
    "hum": "哼了几句歌",
    "gaze_window": "盯着窗外发呆",
}


class Body:
    def __init__(self):
        self.eye = "正常"
        self.pose = "松弛"

    # ------------------------------------------------------------------

    def express(self, breath: dict, snap: Any, weather: dict,
                intent: Any | None, startle: bool = False) -> dict:
        """一帧身体表达。breath/weather 为派生 dict，snap/intent 来自 tick。"""
        # 呼吸灯亮度：随呼吸深浅（depth）与相位起伏
        lamp = round(0.35 + 0.65 * breath["depth"], 2)

        # 眼睛：惊吓睁大 > 犯困半闭 > 正常
        if startle:
            self.eye = "睁大"
        elif snap.waves["sleepiness"] > 0.7:
            self.eye = "半闭"
        else:
            self.eye = "正常"

        # 姿态：底色沉重→蜷缩；波澜紧绷；否则松弛
        if weather["base"] < -0.3:
            self.pose = "蜷缩"
        elif weather["pulse"] > 0.5:
            self.pose = "紧绷"
        else:
            self.pose = "松弛"

        # 自发动作：意图驱动（useless/驱力主导）
        act = ""
        if startle:
            act = "身体一僵"
        elif intent is not None:
            if intent.kind == "useless":
                act = USELESS_ACT.get(intent.action, "发呆")
            elif intent.kind == "abandoned":
                act = "刚才想做啥来着，又忘了"   # P2：意图被打断太多而放弃
            elif intent.kind == "sleepiness":
                act = "打了个哈欠，眼皮变重"
            elif intent.kind == "loneliness":
                act = "往门口看了一眼"
            elif intent.kind == "curiosity":
                act = "竖起耳朵听动静"
            elif intent.kind == "attachment":
                act = "朝主人那边蹭了蹭"

        return {"lamp": lamp, "eye": self.eye, "pose": self.pose, "act": act}
