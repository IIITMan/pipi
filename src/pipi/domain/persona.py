"""人格规格 —— 把"性格"变成可配置、可量化、可切换的参数。

参考《皮皮人格Prompt方案》里的四大机制，但这里做得更工程化：
  - traits         数值化性格向量，驱动行为（主动频率、话长、语气强度）
  - speech_style   说话风格（句长上限、口头禅、禁用词）
  - emotion_bias   情绪基线（好感度衰减速度、吃醋触发阈值）
  - system_prompt  模板名，从 prompts/ 加载

换人格只要换一个 YAML。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Traits:
    """数值化性格向量，每项 0~1。

    这些数值不是装饰 —— 它们真的驱动行为：
      - verbosity      影响 max_tokens 与气泡数
      - clinginess     影响主动搭话频率与冷却期
      - tsundere       影响"口是心非"的表达概率
      - sharpness      影响毒舌程度
    """

    verbosity: float = 0.4          # 话痨度
    clinginess: float = 0.5         # 粘人度
    tsundere: float = 0.5           # 口是心非度
    sharpness: float = 0.3          # 毒舌度
    affection_expressiveness: float = 0.5  # 表达亲密的意愿
    curiosity: float = 0.5          # 好奇心（主动提问倾向）

    def clamp(self) -> "Traits":
        for f in (
            "verbosity", "clinginess", "tsundere", "sharpness",
            "affection_expressiveness", "curiosity",
        ):
            setattr(self, f, max(0.0, min(1.0, float(getattr(self, f)))))
        return self


@dataclass
class SpeechStyle:
    """说话风格的硬约束 —— 这是"没有 AI 味"的工程保障。"""

    max_sentence_chars: int = 25
    max_bubbles: int = 3
    catchphrases: list[str] = field(default_factory=list)
    banned_patterns: list[str] = field(default_factory=list)
    emoji_budget: int = 0

    def __post_init__(self) -> None:
        if not self.banned_patterns:
            # 默认禁用词 —— 出现这些立刻暴露 AI 身份
            self.banned_patterns = [
                "首先", "其次", "最后", "总的来说", "综上所述",
                "希望对你有所帮助", "作为一个人工智能", "作为AI",
                "我理解你的感受", "还有什么可以帮你的",
            ]


@dataclass
class PersonaSpec:
    """完整的人格规格。"""

    id: str
    name: str
    traits: Traits = field(default_factory=Traits)
    speech_style: SpeechStyle = field(default_factory=SpeechStyle)
    baseline_mood: str = "平静"
    affection_decay_per_hour: float = 3.0
    praise_affection_delta: float = 2.0
    jealousy_affection_delta: float = -5.0
    voice_id: str | None = None
    default_voice_emotion: str = "neutral"
    system_prompt_template: str = "persona_system.jinja"
    description: str = ""
    # ---- 人格级覆盖（定义了就盖掉全局配置，build_app 启动时应用）----
    bot_name: str | None = None              # 皮皮自己的名字（贾维斯 → "贾维斯"）
    wake_words: list[str] | None = None      # 独立唤醒词（["贾维斯", "jarvis"]）

    # ---- 从 traits 派生的行为参数（编排层直接读这些，不读 traits）----

    @property
    def proactive_daily_limit(self) -> int:
        """每日主动搭话上限。粘人的可以多说话，但绝不无限。"""
        if self.traits.clinginess >= 0.8:
            return 5
        if self.traits.clinginess >= 0.5:
            return 3
        return 2

    @property
    def proactive_cooldown_min(self) -> int:
        """同类主动搭话的冷却期（分钟）。"""
        base = 30
        return max(10, int(base * (1.2 - self.traits.clinginess)))

    @property
    def max_tokens(self) -> int:
        """回复长度上限（token）。话痨给多点，但也不许写作文。"""
        return int(80 + 120 * self.traits.verbosity)

    @property
    def temperature(self) -> float:
        """采样温度。温度高才有"人味"，但也别失控。"""
        return 0.80 + 0.20 * self.traits.verbosity

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonaSpec":
        """从 YAML/dict 构造。"""
        traits = Traits(**{k: v for k, v in (data.get("traits") or {}).items()
                           if k in Traits.__dataclass_fields__}).clamp()
        style_data = data.get("speech_style") or {}
        style = SpeechStyle(
            **{k: v for k, v in style_data.items()
               if k in SpeechStyle.__dataclass_fields__}
        )
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            traits=traits,
            speech_style=style,
            baseline_mood=data.get("baseline_mood", "平静"),
            affection_decay_per_hour=data.get("affection_decay_per_hour", 3.0),
            praise_affection_delta=data.get("praise_affection_delta", 2.0),
            jealousy_affection_delta=data.get("jealousy_affection_delta", -5.0),
            voice_id=data.get("voice_id"),
            default_voice_emotion=data.get("default_voice_emotion", "neutral"),
            system_prompt_template=data.get("system_prompt_template", "persona_system.jinja"),
            description=data.get("description", ""),
            bot_name=data.get("bot_name"),
            wake_words=data.get("wake_words"),
        )
