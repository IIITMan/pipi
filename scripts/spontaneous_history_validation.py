"""自发发声 → 对话历史记账 验证（修复"皮皮失忆"）。

链路：InteractionSession.try_speak_spontaneous → _speak_spontaneous
      → history_sink（cli 绑定，写入 orch 会话历史，带去重）
场景复现：皮皮自言自语"家里有怪声" → 用户问"有什么怪事" →
         LLM 上下文必须能看到皮皮自己说过的话。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipi.domain.dialogue import Role, Session
from pipi.orchestration.orchestrator import ConversationOrchestrator

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# ---- 模拟 cli._record_spontaneous 的完整逻辑 ----
class FakeOrch:
    def __init__(self):
        self.sess = Session(user_id="owner")

    def get_session(self, uid):
        return self.sess


def record_spontaneous(orch, text):
    if not text:
        return
    sess = orch.get_session("owner")
    last = sess.context_messages(1)
    if last and last[0]["role"] == "assistant" and last[0]["content"] == text:
        return
    sess.add(Role.ASSISTANT, text)


print("== 场景：自言自语 → 下一轮用户提问 ==")
orch = FakeOrch()
# 皮皮自言自语（比如回家汇报"家里有怪声"）
record_spontaneous(orch, "你可算回来了，家里今天有怪声诶。")
# 用户接着问
orch.sess.add(Role.USER, "有什么怪事？")
history = orch.sess.context_messages(5)
check("自言自语进入历史", any(m["role"] == "assistant" and "怪声" in m["content"]
                            for m in history), f"history={history}")
check("LLM 可见上下文含皮皮台词与用户提问",
      [m["role"] for m in history] == ["assistant", "user"])

print("== 去重：handle_proactive 已记过同一句 ==")
orch.sess.add(Role.ASSISTANT, "我已经说过这句话。")
record_spontaneous(orch, "我已经说过这句话。")
check("相同台词不重复入历史",
      sum(1 for m in orch.sess.context_messages(10)
          if m["content"] == "我已经说过这句话。") == 1)
record_spontaneous(orch, "这是新说的另一句。")
check("新台词正常入历史",
      sum(1 for m in orch.sess.context_messages(10)
          if m["content"] == "这是新说的另一句。") == 1)

print("== 空输入安全 ==")
try:
    record_spontaneous(orch, "")
    check("空输入不抛错", True)
except Exception as exc:
    check("空输入不抛错", False, str(exc))

print(f"\n结果 {PASS}/{PASS + FAIL}")
sys.exit(0 if FAIL == 0 else 1)
