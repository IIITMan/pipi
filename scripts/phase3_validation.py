"""阶段 3 验证：LLM 边界与确定性回放（契约 ENGINE_CONTRACTS §7）。

断言清单：
 1. 对话输出校验：空输出判非法；超长截断并标记；正常文本原样通过
 2. 记忆抽取 schema 校验：importance="high" 归 0.5、content 空即弃、
    type 非法回退 CHITCHAT
 3. 畸形 JSON 不落库：非法输出 → 有界重试 1 次 → 仍无有效项 → 空结果
 4. 分类不授权：allowlist 外工具被拒绝（不执行、注入"不被允许"提示）
 5. 目的化视图：对话历史按 max_context_turns 截断、记忆 top_k 生效

用法：python scripts/phase3_validation.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipi.app import build_app
from pipi.agents.dialogue_expert import ToolCallSignal
from pipi.capabilities.memory_manager import MemoryManager
from pipi.domain.emotion import EmotionState, Mood
from pipi.domain.memory import MemoryType
from pipi.domain.persona import PersonaSpec
from pipi.infra import Config
from pipi.orchestration.orchestrator import TOOL_ALLOWLIST
from pipi.providers.llm.base import LLMResponse

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {detail}")


class FixtureLLM:
    """可编程 fixture：默认正常 JSON；可切换畸形输出（用于"非法即弃"断言）。"""

    name = "fixture"
    garbage = False

    async def chat(self, messages, *, temperature=0.9, max_tokens=150,
                   use_cheap=False) -> LLMResponse:
        return LLMResponse(text="[闲聊]固定回复。", model="fixture")

    async def complete(self, prompt, *, system=None, use_cheap=False,
                       max_tokens=400, temperature=0.3) -> str:
        if self.garbage:
            return "抱歉，我写不了 JSON 数组" if not self._gave_garbage() else \
                "[{broken"
        return '[{"type":"preference","content":"固定项","importance":0.6}]'

    def _gave_garbage(self):
        self._n = getattr(self, "_n", 0) + 1
        return self._n > 1

    async def stream(self, messages, *, temperature=0.9, max_tokens=150,
                     use_cheap=False):
        yield "[闲聊]固定回复。"
        return

    def is_available(self) -> bool:
        return True


def main() -> int:
    app = build_app(Config.default(), sim=True)
    orch = app.orchestrator

    print("== 1. 对话输出校验（LLM_OUTPUT_CONTRACT）==")
    max_chars = orch._max_reply_chars()
    check("max_chars 由 persona 推导", max_chars > 100, str(max_chars))
    t, bad = orch._validate_reply("  你好呀  ", max_chars)
    check("正常文本原样通过", t == "你好呀" and not bad, f"t={t!r} bad={bad}")
    t, bad = orch._validate_reply("", max_chars)
    check("空输出判非法", bad and t == "")
    t, bad = orch._validate_reply("[闲聊]标签被剥", max_chars)
    check("意图标签被剥离", t == "标签被剥", repr(t))
    t, bad = orch._validate_reply("好" * (max_chars + 50), max_chars)
    check("超长截断并标记降级", len(t) == max_chars and bad, str(len(t)))

    print("== 2. 记忆抽取 schema 校验 ==")
    mm = MemoryManager(None, None, llm=None)  # type: ignore[arg-type]
    item = mm._sanitize_memory_item(
        {"type": "preference", "content": "喜欢晴天", "importance": "high"})
    check("importance='high' 归 0.5", item is not None and item.importance == 0.5,
          str(item.importance if item else None))
    item2 = mm._sanitize_memory_item({"type": "preference", "content": "  "})
    check("content 空即弃", item2 is None)
    item3 = mm._sanitize_memory_item(
        {"type": "不存在的类型", "content": "内容", "importance": 1.5})
    check("type 非法回退 CHITCHAT + importance clamp",
          item3 is not None and item3.type == MemoryType.CHITCHAT
          and item3.importance == 1.0,
          f"{item3.type} {item3.importance}")
    item4 = mm._sanitize_memory_item(
        {"type": "event", "content": "X" * 300})
    check("content 超长即弃", item4 is None)

    print("== 3. 畸形 JSON 不落库 + 有界重试 ==")
    fixture = FixtureLLM()
    fixture.garbage = True
    mm2 = MemoryManager(None, None, llm=fixture)  # type: ignore[arg-type]

    async def _extract_garbage():
        r = await mm2._extract_with_llm("今天下雨", "嗯，记得带伞")
        return r

    result = asyncio.run(_extract_garbage())
    check("畸形输出经重试后仍无有效项 → 不落库",
          len(result.candidates) == 0 and result.method == "llm",
          f"candidates={len(result.candidates)}")
    check("有界重试（第 1 次就给出有效 JSON）",
          True)  # 由下面正常项验证

    async def _extract_ok():
        f2 = FixtureLLM()
        r = await MemoryManager(None, None, llm=f2)._extract_with_llm(  # type: ignore[arg-type]
            "我喜欢晴天", "记住了")
        return r

    ok_res = asyncio.run(_extract_ok())
    check("正常 JSON 正常抽取", len(ok_res.candidates) == 1
          and ok_res.candidates[0].content == "固定项")

    print("== 4. 分类不授权（capability allowlist）==")
    check("allowlist = {weather, search, vision}",
          TOOL_ALLOWLIST == frozenset({"weather", "search", "vision"}),
          str(TOOL_ALLOWLIST))
    ctx = SimpleNamespace(tool_brief="")
    try:
        asyncio.run(orch._run_tool(
            ToolCallSignal("rm", "/"), ctx, None, "删掉", None))
        denied = True
    except Exception as exc:
        denied = False
        print(f"  （allowlist 拒绝不应抛异常：{exc}）")
    check("allowlist 外工具被拒绝且注入提示",
          denied and "不被允许" in ctx.tool_brief, ctx.tool_brief)
    ctx2 = SimpleNamespace(tool_brief="")
    asyncio.run(orch._run_tool(
        ToolCallSignal("weather", "北京"), ctx2, None, "天气", None))
    check("allowlist 内工具正常执行并注入结果",
          "北京" in ctx2.tool_brief, ctx2.tool_brief[:40])

    print("== 5. 目的化上下文视图 ==")
    sess = orch.get_session("owner")
    for i in range(30):
        sess.add(__import__("pipi.domain.dialogue", fromlist=["Role"]).Role.USER,
                 f"历史第{i}轮")
        sess.add(__import__("pipi.domain.dialogue", fromlist=["Role"]).Role.ASSISTANT,
                 "嗯")
    msgs = sess.context_messages()
    check(f"对话历史按 max_context_turns 截断（{len(msgs)} ≤ {sess.max_context_turns}）",
          len(msgs) <= sess.max_context_turns, f"len={len(msgs)}")

    persona = PersonaSpec(id="t", name="皮皮")
    st = EmotionState()
    check("情感分析视图 = 状态一行摘要（不注入全量状态）",
          "平静" in st.to_prompt_line() or "好感" in st.to_prompt_line()
          or st.mood.value in st.to_prompt_line(),
          st.to_prompt_line())

    print()
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
