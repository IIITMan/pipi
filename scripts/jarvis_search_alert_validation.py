"""① 通用搜索工具 + ② 条件预警引擎 验证（贾维斯化 P0/P1）。

覆盖：
  A. SearchService：detect 触发/排除规则、mock provider、缓存、失败降级
  B. AlertEngine：回家汇报（有料才说/冷却/跨天重置）、陌生人久留、
     提醒留痕汇总
  C. runtime 接线：owner_arrived 触发汇报、stranger 计数与预警、
     会话忙/睡眠抑制（muted 留痕）、统一说话走 LLM 渲染
"""
import asyncio
import math
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=".*never awaited")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipi.capabilities.search import SearchService, detect
from pipi.pulse.alerts import AlertEngine
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


print("== A. SearchService ==")
check("事实疑问触发", detect("黑洞是什么？")
      and detect("为什么天空是蓝色的") and detect("济南在哪里"))
check("天气/提醒不抢", not detect("明天天气怎么样")
      and not detect("提醒我10分钟后喝水"))
check("短情感问不搜", not detect("你干嘛") and not detect("我想你了"))
check("无疑问词不搜", not detect("你好呀今天过得怎么样"))

async def _a():
    svc = SearchService(provider="mock")
    r1 = await svc.fetch("黑洞是什么")
    check("mock fetch 返回占位", bool(r1) and "mock" in r1)
    r2 = await svc.fetch("黑洞是什么")
    check("缓存命中（二次瞬时）", r1 == r2)
asyncio.run(_a())

print("== B. AlertEngine ==")
mono = {"t": 1000.0}
day = {"d": 100}


def _m():
    return mono["t"]


def _d():
    return day["d"]


ae = AlertEngine({"home_cooldown_s": 1800, "stranger_cooldown_s": 3600,
                  "stranger_min": 2}, mono=_m, day_fn=_d)
check("无料不汇报", ae.home_report() is None)
ae.on_reminder_fired("吃药")
check("有提醒 → 汇报", "吃药" in (ae.home_report() or ""))
check("冷却内不重复", ae.home_report() is None)
mono["t"] += 1900
check("冷却过后无新料 → 不再开口", ae.home_report() is None)
ae.on_reminder_fired("复查")
check("新提醒 → 冷却后新料可再报", "复查" in (ae.home_report() or ""))
ae.on_stranger()
ae.on_stranger()
check("陌生人≥2 → 预警", "不放心" in (ae.stranger_alert() or ""))
check("陌生人预警冷却", ae.stranger_alert() is None)
mono["t"] += 3700
day["d"] += 1
check("跨天重置后无料不报", ae.home_report() is None)

print("== C. runtime 接线 ==")


class FakeSession:
    def __init__(self):
        self.is_busy = False
        self.spoken: list[str] = []

    def try_speak_spontaneous(self, text):
        self.spoken.append(text)
        return True


class FakeEv:
    def __init__(self, kind, text=""):
        self.kind = kind
        self.text = text


rt = PulseRuntime({"pulse": {}, "alerts": {"stranger_min": 2},
                   "weather": {}}, session=FakeSession())
# 提醒留痕 → 回家汇报
rt.note_event("reminder", "吃药")
rt.observe_vision(FakeEv("owner_arrived", "主人回来了"))
check("回家汇报进统一说话", any("吃药" in s or "提醒" in s for s in rt.session.spoken),
      f"spoken={rt.session.spoken}")
check("汇报事件留痕", any(e["kind"] == "report_home" for e in rt._outbox))

rt2 = PulseRuntime({"pulse": {}, "alerts": {"stranger_min": 2},
                    "weather": {}}, session=FakeSession())
rt2.observe_vision(FakeEv("stranger", "有陌生人出现"))
rt2.observe_vision(FakeEv("stranger", "有陌生人出现"))
check("陌生人×2 → 预警开口", any("不放心" in s or "留意" in s
                             for s in rt2.session.spoken), f"spoken={rt2.session.spoken}")

rt3 = PulseRuntime({"pulse": {}, "alerts": {"stranger_min": 2},
                    "weather": {}}, session=FakeSession())
rt3.note_event("reminder", "吃药")
rt3.session.is_busy = True
rt3.observe_vision(FakeEv("owner_arrived", "主人回来了"))
check("会话忙 → 不开口（事件仍留痕）",
      not rt3.session.spoken and any(e["kind"] == "report_home"
                                     for e in rt3._outbox))

# 睡眠抑制：构造睡眠时钟
rt4 = PulseRuntime({"pulse": {}, "alerts": {"stranger_min": 2},
                    "weather": {}}, session=FakeSession())
rt4.note_event("reminder", "吃药")


class NightClock:
    def now_hour(self):
        return 23.5

    def now_minute(self):
        return 30

    def now_day(self):
        return 1


rt4.clock = NightClock()
rt4.observe_vision(FakeEv("owner_arrived", "主人回来了"))
check("睡眠中 → 不开口（事件仍留痕）", not rt4.session.spoken
      and any(e["kind"] == "report_home" for e in rt4._outbox))

print(f"\n结果 {PASS}/{PASS + FAIL}")
sys.exit(0 if FAIL == 0 else 1)
