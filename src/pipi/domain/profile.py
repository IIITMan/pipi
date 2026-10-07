"""用户档案 —— "认主"的载体。

L1 身份档案：结构化、每轮注入、永不衰减。
这里的信息来自首次"领养仪式"和后续对话抽取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UserProfile:
    """用户结构化档案。"""

    user_id: str
    nickname: str = ""
    call_name: str = ""          # 专属称呼（皮皮怎么叫他）
    city: str = ""
    living: str = ""             # 居住情况："独居，养了1猫1狗"
    job: str = ""
    likes: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    rivals: list[str] = field(default_factory=list)       # 会让皮皮吃醋的对象
    important_dates: dict[str, str] = field(default_factory=dict)  # {"面试": "2026-10-08"}
    extra: dict[str, Any] = field(default_factory=dict)

    # 关系元数据
    is_owner: bool = False       # 是否已完成"认主"
    met_at: float = 0.0
    last_seen: float = 0.0
    total_turns: int = 0
    persona_id: str = "tsundere"

    @property
    def display_name(self) -> str:
        """皮皮称呼用户时用的名字。"""
        return self.call_name or self.nickname or "你"

    @property
    def met_days(self) -> float:
        """认识多少天。注意：需要外部传入 now，这里只做近似。"""
        return 0.0

    def known_days(self, now: float) -> int:
        if not self.met_at:
            return 0
        return max(0, int((now - self.met_at) / 86400))

    def is_new(self) -> bool:
        """是否还是第一次见面（尚未知道名字）。"""
        return not self.nickname

    def to_l1_block(self) -> str:
        """生成 L1 记忆块 —— 每轮都注入给 LLM。

        刻意写成自然语言而非 JSON：
        JSON 会让模型输出"字段感"的回复，自然语言它才会当聊天背景。
        """
        if self.is_new():
            return "- 这是第一次见面，你还不知道他叫什么。"

        lines = [f"- 他叫{self.nickname}，你平时叫他「{self.display_name}」"]
        if self.city:
            lines.append(f"- 住在{self.city}")
        if self.living:
            lines.append(f"- {self.living}")
        if self.job:
            lines.append(f"- 工作是{self.job}")
        if self.likes:
            lines.append(f"- 喜欢：{'、'.join(self.likes[:6])}")
        if self.dislikes:
            lines.append(f"- 不喜欢：{'、'.join(self.dislikes[:6])}")
        if self.rivals:
            lines.append(f"- 让他吃醋的对象：{'、'.join(self.rivals[:6])}")
        for label, date in list(self.important_dates.items())[:5]:
            lines.append(f"- {date} {label}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "call_name": self.call_name,
            "city": self.city,
            "living": self.living,
            "job": self.job,
            "likes": list(self.likes),
            "dislikes": list(self.dislikes),
            "rivals": list(self.rivals),
            "important_dates": dict(self.important_dates),
            "extra": dict(self.extra),
            "is_owner": self.is_owner,
            "met_at": self.met_at,
            "last_seen": self.last_seen,
            "total_turns": self.total_turns,
            "persona_id": self.persona_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserProfile":
        return cls(
            user_id=data["user_id"],
            nickname=data.get("nickname", ""),
            call_name=data.get("call_name", ""),
            city=data.get("city", ""),
            living=data.get("living", ""),
            job=data.get("job", ""),
            likes=list(data.get("likes") or []),
            dislikes=list(data.get("dislikes") or []),
            rivals=list(data.get("rivals") or []),
            important_dates=dict(data.get("important_dates") or {}),
            extra=dict(data.get("extra") or {}),
            is_owner=bool(data.get("is_owner", False)),
            met_at=float(data.get("met_at") or 0.0),
            last_seen=float(data.get("last_seen") or 0.0),
            total_turns=int(data.get("total_turns") or 0),
            persona_id=data.get("persona_id", "tsundere"),
        )
