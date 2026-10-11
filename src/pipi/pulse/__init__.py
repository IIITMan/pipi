"""PULSE · 心搏 —— 生命模块重设计（PULSE_DESIGN.md）。

8→4 精简（2026-10-11，消融证据 scripts/pulse_ablation.py）：
  - 保留器官：Tension（驱力场）/ Will（行为仲裁）/ Bond（关系）/ Seed（并入 growth 前暂存）
  - 派生展示：Affect（心境天气）/ Rhythm（呼吸波形）降为纯函数（pulse/derive.py）
  - 习惯化简化：Anticipation 删除，异常事件"终身只吓一次"由 loop 字段表达
  - River 瘦身：删 day_stream 缓冲，保留跨天整理（日记/自我深度/认主日历）
"""

from pipi.pulse.derive import weather_label
from pipi.pulse.organs.bond import Bond
from pipi.pulse.organs.river import River
from pipi.pulse.organs.seed import Seed
from pipi.pulse.organs.tension import TensionField, TensionSnapshot
from pipi.pulse.organs.will import Intent, Will

__all__ = [
    "TensionField", "TensionSnapshot",
    "Will", "Intent",
    "Bond",
    "River",
    "Seed",
    "weather_label",
]
