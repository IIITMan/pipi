"""感知接入生命模块验证（PERCEPTION_PLAN §5）。

覆盖：四类事件→生理/关心列表/startle、去重窗口、表情转变语义、
vision=None 可用、感知循环端到端（脚本视觉）。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pipi.life.engine import LifeEngine
from pipi.life.perception import MonotonicClock, PerceptionEvent, PerceptionLoop

PASS = 0


def ok(cond, msg):
    global PASS
    assert cond, f"FAIL: {msg}"
    PASS += 1
    print(f"  ✓ {msg}")


class FakeSession:
    state = "idle"
    is_busy = False

    def __init__(self):
        self.spoken = []

    def try_speak_spontaneous(self, text):
        self.spoken.append(text)
        return True


class FakeVision:
    """脚本化假视觉：capture() 依次返回脚本观测。"""

    def __init__(self, script):
        self._script = script
        self._idx = 0

    async def capture(self):
        if self._idx < len(self._script):
            obs = self._script[self._idx]
            self._idx += 1
            return obs
        return None


def make_engine(vision=None):
    eng = LifeEngine(
        cfg={"life": {"perception": {"enabled": True, "fps": 20.0,
                                     "debounce_s": 0.0,
                                     "stranger_cooldown_s": 0.0}}},
        llm=None, persona_name="皮皮", persona_hint="傲娇",
        memory_getter=lambda: None, session=FakeSession(),
        vision=vision,
    )
    startle_calls = []
    eng.scheduler.startle = lambda: startle_calls.append(1)
    return eng, startle_calls


class Obs:
    def __init__(self, owner_present=True, strangers=0, expression=""):
        self.owner_present = owner_present
        self.extra_person_count = strangers
        self.faces = []
        self.expressions = {"owner": type("E", (), {"label": expression})()} \
            if expression else {}


def test_events_from():
    print("[1] PerceptionLoop 事件判定（沿触发/表情转变/去重）")
    loop = PerceptionLoop(None, MonotonicClock(),
                          {"debounce_s": 0.0, "stranger_cooldown_s": 0.0},
                          on_event=lambda e: None)

    # 首帧：只建立基线，不发事件
    ok(loop._events_from(Obs(owner_present=True)) == [], "首帧只建基线")
    # 离场沿（基线在场 → 不在场）
    evs = loop._events_from(Obs(owner_present=False))
    ok(len(evs) == 1 and evs[0].kind == "owner_gone", "在场→离场 → owner_gone")
    # 到场沿
    evs = loop._events_from(Obs(owner_present=True))
    ok(len(evs) == 1 and evs[0].kind == "owner_arrived", "不在场→在场 → owner_arrived")
    # 陌生人
    evs = loop._events_from(Obs(owner_present=True, strangers=1))
    ok(any(e.kind == "stranger" for e in evs), "有陌生人 → stranger")
    # 表情：转变才推
    ok(loop._events_from(Obs(owner_present=True, expression="sad")) != [],
       "neutral→sad 推事件")
    ok(loop._events_from(Obs(owner_present=True, expression="sad")) == [],
       "持续 sad 不重推（防 mood 钉死）")
    ok(loop._events_from(Obs(owner_present=True, expression="happy")) == [],
       "happy 不需要关心，不推")


def test_observe_vision():
    print("[2] LifeEngine.observe_vision：生理/关心列表/startle")
    eng, startle_calls = make_engine(None)

    eng.observe_vision(PerceptionEvent(time.time(), "owner_arrived", "主人回来了"))
    ok(eng.physio.social < 1.0 and len(eng.concerns) == 1,
       "owner_arrived → social 重置 + 关心列表")
    ok(all(c.source != "vocal" for c in eng.concerns), "到场不产生发声念头")

    eng.observe_vision(PerceptionEvent(time.time(), "stranger", "有陌生人"))
    ok(len(startle_calls) == 1, "stranger → startle() 被调用")

    m0 = eng.physio.mood
    eng.observe_vision(PerceptionEvent(
        time.time(), "owner_expression", "主人好像不太开心（sad）",
        {"label": "sad"}))
    ok(eng.physio.mood < m0 and eng.physio.social > 0,
       "主人低落 → mood 微降 + social 微升")

    eng.observe_vision(PerceptionEvent(time.time(), "owner_gone", "主人走了"))
    ok(any(c.text == "又剩我一个了" for c in eng.concerns), "owner_gone 记入关心")

    ok(len([e for e in eng.log if e["kind"].startswith(("owner", "stranger"))]) >= 3,
       "感知事件写入监控心理记录")


async def test_loop_end_to_end():
    print("[3] 感知循环端到端（脚本视觉 → 事件 → 引擎）")
    eng, startle_calls = make_engine(FakeVision([
        Obs(owner_present=False),                       # 基线
        Obs(owner_present=True),                        # 到场
        Obs(owner_present=True, strangers=1),           # 陌生人
    ]))
    eng.start()
    await asyncio.sleep(0.3)   # 让感知循环消费 3 帧
    await eng.stop()
    kinds = [e["kind"] for e in eng.log]
    ok("owner_arrived" in kinds and "stranger" in kinds,
       f"循环真实产生了事件（{kinds}）")
    ok(len(startle_calls) >= 1, "陌生人触发受惊")


async def test_no_vision():
    print("[4] vision=None：链路可用")
    eng, _ = make_engine(None)
    eng.start()
    await asyncio.sleep(0.1)
    await eng.stop()
    ok(eng._perception is None, "无摄像头 → 感知循环不启动，引擎正常")


async def main():
    test_events_from()
    test_observe_vision()
    await test_loop_end_to_end()
    await test_no_vision()
    print(f"\n结果：{PASS} 项断言全部通过")

asyncio.run(main())
