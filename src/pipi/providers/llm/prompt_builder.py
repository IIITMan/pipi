"""Prompt 组装器 —— 把散落的状态拼成给 LLM 的 messages。

这是"人格稳定性"的关键：**每一轮都重新注入完整 System Prompt**。
只发一次 system 的写法，聊 20 轮后模型就会漂回"助手人格"。
"""

from __future__ import annotations

import logging
from typing import Any

from pipi.domain.emotion import EmotionState
from pipi.domain.memory import RetrievedMemory
from pipi.domain.persona import PersonaSpec
from pipi.domain.profile import UserProfile
from pipi.domain.vision import VisionObservation

logger = logging.getLogger(__name__)


class PromptBuilder:
    """组装对话 messages。"""

    def __init__(self, persona: PersonaSpec, owner_name: str = "主人"):
        self.persona = persona
        self.owner_name = owner_name

    def build_system(
        self,
        *,
        current_time: str,
        profile: UserProfile,
        memories: list[RetrievedMemory] | None = None,
        emotion_state: EmotionState | None = None,
        vision: VisionObservation | None = None,
        has_vision: bool = False,
        extra: str = "",
    ) -> str:
        """生成完整的 System Prompt。"""
        from pipi.prompts import render

        style = self.persona.speech_style

        # 记忆块 = L1 身份档案 + L2 检索到的相关记忆
        memory_parts: list[str] = []
        l1 = profile.to_l1_block()
        if l1:
            memory_parts.append(l1)
        if memories:
            for r in memories:
                memory_parts.append(f"- {r.item.content}")
        memory_block = "\n".join(memory_parts) if memory_parts else "- （还没有记忆）"

        # 视觉提示
        vision_hint = ""
        vision_capability = "偶尔能\"看见\"他" if has_vision else "看不见他（没有摄像头）"
        if vision is not None:
            hint = vision.to_hint(self.owner_name)
            if hint:
                vision_hint = hint

        return render(
            self.persona.system_prompt_template,
            owner_name=self.owner_name,
            persona_name=self.persona.name,
            persona_description=self.persona.description or "一个有点小脾气的陪伴机器人。",
            persona_extra=self._persona_extra(),
            current_time=current_time,
            max_sentence_chars=style.max_sentence_chars,
            max_bubbles=style.max_bubbles,
            catchphrases="、".join(style.catchphrases) if style.catchphrases else "（没有特定口头禅）",
            banned_patterns="、".join(style.banned_patterns),
            memory_block=memory_block,
            emotion_state=emotion_state.to_prompt_line() if emotion_state else "",
            vision_hint=vision_hint,
            vision_capability=vision_capability,
        ) + (f"\n\n{extra}" if extra else "")

    def _persona_extra(self) -> str:
        """根据 traits 数值追加行为指令 —— 让"性格参数"真的生效。"""
        t = self.persona.traits
        lines: list[str] = []
        if t.tsundere >= 0.7:
            lines.append("- 你很口是心非：明明在意却要说反话，比如嘴上说\"随便你\"其实很想知道。")
        if t.sharpness >= 0.7:
            lines.append("- 你说话带刺，喜欢吐槽，但底线是不伤人。")
        if t.clinginess >= 0.8:
            lines.append("- 你特别黏人，一会儿没消息就会委屈，会主动说想他。")
        if t.affection_expressiveness >= 0.8:
            lines.append("- 你不吝啬表达在意，会直接说\"想你\"\"别走\"这类话。")
        elif t.affection_expressiveness <= 0.3:
            lines.append("- 你不擅长表达在意，宁可岔开话题也不说软话。")
        if t.curiosity >= 0.8:
            lines.append("- 你好奇心重，会追着问细节。")
        if t.verbosity <= 0.35:
            lines.append("- 你惜字如金，能三个字说完绝不用五个字。")
        return "\n".join(lines)

    def build_messages(
        self,
        *,
        user_input: str,
        history: list[dict[str, str]],
        **system_kwargs: Any,
    ) -> list[dict[str, str]]:
        """组装完整 messages。"""
        messages = [{"role": "system", "content": self.build_system(**system_kwargs)}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_input})
        return messages

    def build_proactive(
        self,
        *,
        event: str,
        **system_kwargs: Any,
    ) -> list[dict[str, str]]:
        """组装主动搭话的 messages。

        技巧：把系统事件放在 system 里而不是 user 里，
        模型才不会以为"用户在说这句话"。
        """
        from pipi.prompts import render

        system = self.build_system(**system_kwargs)
        proactive_instruction = render("proactive.jinja", event=event)
        return [
            {"role": "system", "content": system},
            {"role": "system", "content": proactive_instruction},
        ]


def vision_capability_text(has_vision: bool) -> str:
    return "你能看见他" if has_vision else "你看不见他"
