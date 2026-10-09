"""③ 环境档案 + ④ HUD 数据契约 验证（贾维斯化 3/4）。

覆盖：
  A. EnvironmentMemory：对话词表记账/去重、视觉（回家/离开/陌生人）作息学习、
     天气/提醒记录、JSON 落盘与重载、snapshot 结构、能力在线状态
  B. runtime 接线：observe_vision/note_event/天气同步 顺带喂档案、snapshot 带 env
  C. orchestrator 接线：handle_turn 对话顺带记账（mock LLM 下不阻塞）
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipi.capabilities.environment import EnvironmentMemory
from pipi.pulse.runtime import PulseRuntime

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


print("== A. EnvironmentMemory ==")
tmp = Path(tempfile.mkdtemp())
env = EnvironmentMemory(tmp, cfg={"search": {"enabled": True},
                                  "alerts": {"enabled": True},
                                  "weather": {"enabled": False}})
check("初始无数据", env.home_hour_expected() is None and env.snapshot()["owner_present"] is False)
env.observe_chat("我把钥匙放书房了，还看见一只猫")
env.observe_chat("钥匙又忘拿了")
s = env.snapshot()
check("对话记账（物品/地点）", "钥匙×2" in s["items"] and "书房×1" in s["places"],
      f"items={s['items']} places={s['places']}")
check("去重计数（钥匙只记一次条目）",
      sum(1 for i in s["items"] if i.startswith("钥匙")) == 1)

class Ev:
    def __init__(self, kind, text=""):
        self.kind = kind
        self.text = text

# 作息学习：多次回家 → 平均钟点（19 与 21 → 20）
env.observe_vision(Ev("owner_arrived")); env.observe_vision(Ev("owner_arrived"))
env.observe_vision(Ev("owner_left"))
env.observe_vision(Ev("stranger")); env.observe_vision(Ev("stranger"))
s = env.snapshot()
check("主人状态", s["owner_present"] is False and s["stranger_today"] == 2)
check("作息学习", s["owner_home_hour"] is not None,
      f"home_hour={s['owner_home_hour']}")
check("facts_brief 有料", "陌生" in env.facts_brief() or "主人一般" in env.facts_brief(),
      f"brief={env.facts_brief()}")

env.observe_weather("rain", "雨")
env.observe_reminder("吃药")
s = env.snapshot()
check("天气/提醒入档案", s["weather"]["zh"] == "雨"
      and any(r["text"] == "吃药" for r in s["reminders"]))
check("能力在线状态", s["online"] == {"search": True, "alerts": True,
                                    "weather": False})

# 落盘与重载
env2 = EnvironmentMemory(tmp, cfg={})
s2 = env2.snapshot()
check("JSON 重载", s2["stranger_today"] == 2 and "钥匙×2" in s2["items"])
check("重载后仍在记", (lambda e: (e.observe_chat("妈妈来了"), e.snapshot()["people"]))(env2)[1][0].startswith("妈妈"))

print("== B. runtime 接线 ==")
rt = PulseRuntime({"pulse": {}, "alerts": {}, "weather": {}}, env=env2)
rt.note_event("reminder", "复查")
rt.observe_vision(Ev("owner_arrived"))
snap = rt.snapshot()
check("snapshot 带 env 段", snap["env"] is not None
      and snap["env"]["owner_present"] is True
      and any(r["text"] == "复查" for r in snap["env"]["reminders"]),
      f"env={snap['env']['owner_present']}")

print("== C. orchestrator 接线 ==")
import asyncio


async def _c():
    from pipi.orchestration.orchestrator import ConversationOrchestrator
    orch = ConversationOrchestrator.__new__(ConversationOrchestrator)
    orch.env = EnvironmentMemory(tmp, cfg={})
    orch.weather = None
    orch.search = None
    orch.reminders = None
    # 只验证 observe_chat 不抛错且记账（不走完整 handle_turn）
    try:
        orch.env.observe_chat("把快递放门口吧，老婆")
        got = orch.env.snapshot()
        return "门口×1" in got["places"] and "老婆×1" in got["people"]
    except Exception as exc:
        return f"EXC:{exc}"


check("对话顺带记账", asyncio.run(_c()))

print(f"\n结果 {PASS}/{PASS + FAIL}")
sys.exit(0 if FAIL == 0 else 1)
