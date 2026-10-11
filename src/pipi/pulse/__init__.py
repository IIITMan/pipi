"""PULSE · 心搏 —— 生命模块重设计（PULSE_DESIGN.md）。

8→4 精简（2026-10-11，消融证据 scripts/pulse_ablation.py）：
  - 保留器官：Tension（驱力场）/ Will（行为仲裁）/ Bond（关系）/ Seed（并入 growth 前暂存）
  - 派生展示：Affect（心境天气）/ Rhythm（呼吸波形）降为纯函数（pulse/derive.py），
    不再作为独立器官对象；Anticipation/River 将随后精简。
"""

from pipi.pulse.derive import weather_label
from pipi.pulse.organs.anticipation import Anticipation, Surprise
from pipi.pulse.organs.bond import Bond
from pipi.pulse.organs.river import River
from pipi.pulse.organs.seed import Seed
from pipi.pulse.organs.tension import TensionField, TensionSnapshot
from pipi.pulse.organs.will import Intent, Will

__all__ = [
    "TensionField", "TensionSnapshot",
    "Will", "Intent",
    "Bond",
    "Anticipation", "Surprise",
    "River",
    "Seed",
    "weather_label",
]
