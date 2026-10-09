# -*- coding: utf-8 -*-
"""验证：身体动作上监控 + 意图→主动行为 + 统一说话。
断言 body/behavior 进 snapshot、action_sink 被驱动、
主动发声优先 LLM 渲染、LLM 失败用句库兜底。"""
import asyncio
import sys

sys.path.insert(0, r"D:\Work\pipi-robot\src")
from pipi.pulse.runtime import PulseRuntime
from pipi.pulse.expression import behavior_from_intent, NullActionSink


class Clock:
    def __init__(self, hour=14.0):
        self.hour = hour

    def now_hour(self):
        return self.hour

    def now_minute(self):
        return 0

    def now_day(self):
        return 1

    def mono(self):
        return self.hour * 3600.0


class Sess:
    def __init__(self):
        self.is_busy = False
        self.state = None
        self.spoken = []

    def try_speak_spontaneous(self, t):
        self.spoken.append(t)
        return True


passed = total = 0


def chk(name, ok, detail=""):
    global passed, total
    total += 1
    passed += int(ok)
    print(f'  [{"PASS" if ok else "FAIL"}] {name}  {detail}')


# ---- 1. 意图 → 行为映射 ----
print("== 意图→行为 ==")
class I:
    def __init__(self, k):
        self.kind = k


chk("attachment → approach owner",
    behavior_from_intent(I("attachment"), True)["motion"] == "approach")
chk("attachment 主人不在 → look door",
    behavior_from_intent(I("attachment"), False)["motion"] == "look")
chk("curiosity → explore",
    behavior_from_intent(I("curiosity"), True)["motion"] == "explore")
chk("security → retreat",
    behavior_from_intent(I("security"), True)["motion"] == "retreat")
chk("无意图 → idle",
    behavior_from_intent(None, True)["motion"] == "idle")

# ---- 2. body/behavior 进 snapshot + action_sink 驱动 ----
print("== 身体/行为上监控 + 硬件端口 ==")
clk = Clock()
sess = Sess()
sink = NullActionSink()
rt = PulseRuntime({"pulse": {"tick_s": 60.0}}, session=sess,
                  clock=clk, mono=clk.mono)
rt.action_sink = sink
rt._beat()
p = rt.snapshot()["pulse"]
chk("snapshot 含 body(lamp/eye/pose/act)",
    all(k in p["body"] for k in ("lamp", "eye", "pose", "act")),
    f'lamp={p["body"]["lamp"]} eye={p["body"]["eye"]} pose={p["body"]["pose"]}')
chk("snapshot 含 behavior(motion/target/zh)",
    all(k in p["behavior"] for k in ("motion", "target", "zh")),
    p["behavior"]["zh"])
chk("action_sink.apply 被调用", sink.last is not None)

# ---- 3. 统一说话：LLM 渲染优先 / 失败句库兜底 ----
print("== 统一说话 ==")


async def main():
    # 3a. 有 LLM 渲染器 → 用渲染结果
    async def good_renderer(event):
        return "[LLM] " + event[:12]
    rt.vocal_renderer = good_renderer
    await rt._render_and_speak("loneliness", "句库兜底")
    chk("主动发声走 LLM 渲染", sess.spoken[-1].startswith("[LLM]"),
        sess.spoken[-1])

    # 3b. 渲染器抛错 → 句库兜底
    async def bad_renderer(event):
        raise RuntimeError("llm down")
    rt.vocal_renderer = bad_renderer
    await rt._render_and_speak("attachment", "句库兜底")
    chk("LLM 失败 → 句库兜底", sess.spoken[-1] == "句库兜底",
        sess.spoken[-1])

    # 3c. 无渲染器 → 句库
    rt.vocal_renderer = None
    await rt._render_and_speak("curiosity", "句库兜底")
    chk("无渲染器 → 句库", sess.spoken[-1] == "句库兜底")

    # 3d. 内心事件含分离时长
    rt.loop.sep_min = 5 * 60
    ev = rt._vocal_event("loneliness")
    chk("内心事件含分离时长", "5.0 小时" in ev, ev[:30])


asyncio.run(main())

print(f"\n  通过 {passed}/{total}")
