"""生命模块 —— 生理状态、注意力、念头引擎与行为调度（v2.1 阶段 1）。"""

from pipi.life.engine import LifeEngine
from pipi.life.state_machine import AttentionMode, LifeState, Personality, Physiology
from pipi.life.thought_queue import Thought, ThoughtQueue

__all__ = [
    "LifeEngine", "LifeState", "AttentionMode",
    "Personality", "Physiology", "Thought", "ThoughtQueue",
]
