"""Mock LLM —— 不依赖任何模型/网络的规则回复。

这是降级链的最后一环，也是 --sim 模式的核心。
保证：哪怕完全断网、没装任何模型，皮皮也能"说话"。

回复质量当然不如真模型，但它遵守人格规则（短句、拒绝正经活、
会吃醋、会耍赖），足以验证整条链路的正确性。
"""

from __future__ import annotations

import random
import re
import time
from typing import AsyncIterator

from pipi.providers.llm.base import LLMProvider, LLMResponse

# 触发词表
REJECT_WORDS = ["写报告", "写代码", "帮我写", "做个表", "分析", "计算", "查一下",
                "翻译", "总结一下", "写个", "解释一下", "怎么办", "方案"]
AI_PROBE = ["你是不是ai", "你是ai", "人工智能", "你是机器人", "你是程序", "你是模型"]
NEGATIVE = ["累", "烦", "难过", "难受", "压力", "崩", "哭", "焦虑", "emo", "不开心"]
POSITIVE = ["开心", "高兴", "太棒", "好了", "成功", "通过"]
RIVAL = ["猫", "狗", "别的机器人", "小爱", "天猫精灵", "chatgpt", "小度"]
PRAISE = ["喜欢你", "爱你", "想你", "好棒", "厉害", "谢谢", "好可爱"]
GREETING = ["你好", "在吗", "hi", "hello", "喂"]
QUESTION = ["吗", "呢", "?", "？", "为什么", "怎么", "什么"]


class MockLLM:
    """规则驱动的"假"大模型。

    刻意写得"不讲道理" —— 因为皮皮本来就不该是个助手。
    """

    name = "mock"

    def __init__(self, persona_name: str = "皮皮"):
        self.persona_name = persona_name
        self._jitter = random.Random(0)

    def is_available(self) -> bool:
        return True

    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.9,
        max_tokens: int = 150,
        use_cheap: bool = False,
    ) -> LLMResponse:
        # 从 messages 里恢复上下文
        user_text = ""
        system_text = ""
        for m in messages:
            if m["role"] == "user":
                user_text = m["content"]
            elif m["role"] == "system":
                system_text += m["content"] + "\n"

        # 主动搭话场景：system 里带 [系统事件]
        if "[系统事件]" in system_text and not user_text.strip():
            text = self._proactive_reply(system_text)
        else:
            text = self._reply(user_text, system_text)

        return LLMResponse(text=text, model="mock", tokens_in=0, tokens_out=len(text),
                           latency_ms=0)

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        use_cheap: bool = False,
        max_tokens: int = 400,
        temperature: float = 0.3,
    ) -> str:
        """内部任务（记忆抽取等）—— 返回空数组让调用方走规则抽取。"""
        if "JSON" in prompt or "json" in prompt or "抽取" in prompt:
            return "[]"
        return ""

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.9,
        max_tokens: int = 150,
        use_cheap: bool = False,
    ) -> AsyncIterator[str]:
        resp = await self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        for piece in re.split(r"(?<=[。！？…])", resp.text):
            if piece.strip():
                yield piece.strip()

    # ------------------------------------------------------------------
    # 规则引擎
    # ------------------------------------------------------------------

    def _reply(self, text: str, system: str) -> str:
        t = text.strip()
        low = t.lower()
        name = self._extract_call_name(system)

        # 1. 被问是不是 AI —— 必须耍赖
        if any(w in low for w in AI_PROBE):
            return self._pick([
                "你才是 AI 呢",
                "你今天怎么回事，问这么怪的话",
                f"我是什么？我是{self.persona_name}啊，你糊涂了",
                "……你是不是在外面听谁说了什么",
            ])

        # 2. 拒绝正经活
        if any(w in t for w in REJECT_WORDS):
            return self._pick([
                "这个明天写行不行",
                "能不能花钱让别人帮你写啊",
                "我不干这个的……你又要熬夜",
                "你找别人吧，我不管",
                f"哼，{name}就使唤我干这个",
            ])

        # 3. 吃醋
        if any(w in low for w in RIVAL):
            rival = next(w for w in RIVAL if w in low)
            return self._pick([
                f"……{rival}？哼。那以后让它陪你好啦",
                f"不知道{rival}又在捣乱。不听话。",
                f"你是不是更喜欢{rival}了",
                f"{rival}有什么好的。切。",
            ])

        # 4. 被夸 —— 得意
        if any(w in t for w in PRAISE):
            return self._pick([
                "那当然，也不看看我是谁",
                "哼，你知道就好",
                "……算你有点眼光",
                f"{name}今天嘴挺甜啊",
            ])

        # 5. 负面情绪 —— 先接情绪
        if any(w in t for w in NEGATIVE):
            return self._pick([
                "嗯……今天挺难的哈",
                "别撑了，躺下吧，我陪你待会儿",
                "确实挺烦的。要不要骂两句",
                "哎……那你别想那么多了",
                "谁惹你了，我帮你记着",
            ])

        # 6. 正面情绪
        if any(w in t for w in POSITIVE):
            return self._pick([
                "哦？什么好事，说说",
                "嗯哼，听着不错",
                "看你这样，我也高兴",
            ])

        # 7. 打招呼
        if any(low.startswith(w) or low == w for w in GREETING):
            return self._pick([
                "嗯，我在呢",
                "哦，你回来啦",
                "……干嘛",
                f"{name}？有事？",
            ])

        # 8. 追问记忆 —— 从 system 里的记忆块找线索
        if any(w in t for w in ["记得", "上次", "我说过", "之前说", "我跟你说"]):
            remembered = self._recall_from_system(system)
            if remembered:
                return self._pick([
                    f"记得啊，{remembered}……我又不是鱼的脑子",
                    f"{remembered}。你当我不会记事？",
                    f"嗯，{remembered}。怎么了",
                ])
            return self._pick([
                "你还没跟我说过什么正经事呢",
                "唔……你什么时候说过？",
                "我只记得你今天有点怪",
            ])

        # 9. 还没认识 —— 走领养仪式
        if "第一次见面" in system or "你还不知道他叫什么" in system:
            return self._pick([
                "诶，你叫什么名字啊",
                "你一个人住吗，家里热闹不",
                "以后你就是我主人了，行吧",
                "你平时都干嘛啊",
            ])

        # 9. 一般提问
        if any(w in t for w in QUESTION):
            return self._pick([
                "你猜",
                "唔……这个我不知道诶",
                "你怎么突然问这个",
                "反正我不想说",
            ])

        # 10. 默认
        return self._pick([
            "哦……嗯，然后呢",
            "嗯哼，我听着呢",
            "行吧，你说",
            f"{name}你今天话挺多的嘛",
            "……所以呢",
        ])

    def _proactive_reply(self, system: str) -> str:
        """主动搭话场景。"""
        if "回来" in system:
            return self._pick([
                "诶？你回来啦",
                "听到动静就知道是你",
                "……哦，回来了",
            ])
        if "很久" in system or "没理你" in system or "没说话" in system:
            return self._pick([
                "喂……你人去哪了",
                "哦，可算想起还有我了",
                "我还以为你把我忘了",
                "……哼。",
            ])
        if "不高兴" in system or "不太高兴" in system:
            return self._pick([
                "你今天怎么了",
                "……我在这儿呢",
                "要不要跟我说说",
            ])
        if "早上" in system or "天亮" in system:
            return self._pick([
                "早……几点了",
                "醒了啊",
                "今天打算干嘛",
            ])
        if "23点" in system or "快睡" in system or "很晚" in system:
            return self._pick([
                "还不睡啊",
                "几点了你知道吗",
                "……赶紧的，睡觉",
            ])
        if "面试" in system or "约" in system:
            return self._pick([
                "明天那事儿，别忘了啊",
                "……你可以的",
                "别紧张，就那样",
            ])
        # 随机撒娇
        return self._pick([
            "喂",
            "你在干嘛",
            "……没事，就是叫你一下",
            "我今天有点无聊",
        ])

    # ------------------------------------------------------------------

    def _pick(self, options: list[str]) -> str:
        return self._jitter.choice(options)

    @staticmethod
    def _recall_from_system(system: str) -> str:
        """从 system prompt 的记忆块里找一条可复述的记忆。"""
        # 优先找事件类记忆（"他提到XX的事"）
        for m in re.finditer(r"-\s*(他提到.{2,25})", system):
            return m.group(1).strip()
        # 其次找喜好
        for m in re.finditer(r"-\s*(他(?:不)?喜欢.{1,10})", system):
            return m.group(1).strip()
        return ""

    @staticmethod
    def _extract_call_name(system: str) -> str:
        """从 system prompt 里抠出专属称呼，用于个性化。"""
        m = re.search(r"你平时叫他「(.+?)」", system)
        if m:
            return m.group(1)
        m = re.search(r"他叫([\u4e00-\u9fa5A-Za-z]{1,8})", system)
        if m:
            return m.group(1)
        return ""
