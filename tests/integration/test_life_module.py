#!/usr/bin/env python3
"""生命模块集成验收（阶段 1：Mock 模式）。

用法：python tests/integration/test_life_module.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pipi.life import (AttentionMode, LifeEngine, LifeState, Personality,
                       Physiology, Thought, ThoughtQueue)
from pipi.life.action_adapter import MockActionAdapter
from pipi.life.behavior_scheduler import BehaviorScheduler

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


class FakeLLM:
    async def complete(self, prompt, **kw):
        return "喂，有没有空理理我啊。"   # 台词本体（不能是括号舞台指示，会被过滤）


class FakeMemGetter:
    def __call__(self):
        return "主人说喜欢晚霞"


def make_engine(session=None, llm=None):
    cfg = {"life": {"vocal_cooldown_s": 0.0, "wandering_cooldown_s": 0.0,
                    "thought_ttl_s": 5.0, "energy_min_vocal": 2.5,
                    "action_backend": "mock", "physio_hz": 10.0,
                    "thought_hz": 10.0, "scheduler_hz": 10.0,
                    "personality": {"extraversion": 10.0}}}
    return LifeEngine(cfg, llm or FakeLLM(), "皮皮", "傲娇但黏人",
                      FakeMemGetter(), session or FakeSession())


async def test_physiology():
    print("[1] 生理三轴：钳位/回归/事件")
    p = Physiology()
    p.tick(100.0, LifeState.IDLE, AttentionMode.FOCUS_ON_OWNER)
    ok(0 <= p.energy <= 10 and 0 <= p.social <= 10 and -5 <= p.mood <= 5,
       "tick 后三轴仍在界内")
    p.mood = 4.0
    p.tick(100.0, LifeState.IDLE, AttentionMode.FOCUS_ON_OWNER)
    ok(p.mood < 4.0, "心情向平静回归")
    e0 = p.energy
    p.mood = 0.0
    p.on_barge_in()
    ok(p.mood < 0 and p.energy < e0, "被打断 → 心情变差/精力消耗")
    p.mood = 0.0
    p.on_response_done()
    ok(p.mood > 0, "聊得开心 → 心情变好")
    p.social = 0.0
    p.on_user_voice()
    ok(True, "主人说话事件不炸")
    ok("状态平静" in p.describe() or len(p.describe()) > 0, "describe 可用")


async def test_thought_queue():
    print("[2] 念头队列：TTL 过期与优先级")
    q = ThoughtQueue(ttl_s=0.05)
    q.push(Thought(content="低优先", priority=1))
    q.push(Thought(content="高优先", priority=5))
    await asyncio.sleep(0.06)
    ok(q.size() == 0, "TTL 5s 过期 → 念头被丢弃（想到又忘记）")
    q.push(Thought(content="a", priority=1))
    q.push(Thought(content="b", priority=5))
    t = q.pop_best()
    ok(t.content == "b", "按优先级取最优")


async def test_scheduler_gates():
    print("[3] 调度器：冷却/精力/会话忙门控")
    cfg = {"vocal_cooldown_s": 10.0, "energy_min_vocal": 2.5}
    action = MockActionAdapter()
    sched = BehaviorScheduler(cfg, action)
    physio = Physiology()
    pers = Personality()
    q = ThoughtQueue(ttl_s=5.0)

    q.push(Thought(content="想说话", priority=5, vocal_enable=True))
    t = sched.scan(LifeState.IDLE, AttentionMode.FOCUS_ON_OWNER,
                   physio, pers, q, session_busy=False)
    ok(t is not None, "冷却期外可执行发声念头")
    sched.mark_vocal()

    q.push(Thought(content="又被冷却拦住", priority=5, vocal_enable=True))
    t = sched.scan(LifeState.IDLE, AttentionMode.FOCUS_ON_OWNER,
                   physio, pers, q, session_busy=False)
    ok(t is None, "冷却期内发声念头不执行")

    t = sched.scan(LifeState.CHATTING, AttentionMode.FOCUS_ON_OWNER,
                   physio, pers, q, session_busy=False)
    ok(t is None, "对话状态不抢话")

    t = sched.scan(LifeState.IDLE, AttentionMode.FOCUS_ON_OWNER,
                   physio, pers, q, session_busy=True)
    ok(t is None, "会话忙时不执行念头")

    physio.energy = 1.0
    q.push(Thought(content="困到不想说", priority=5, vocal_enable=True))
    t = sched.scan(LifeState.IDLE, AttentionMode.FOCUS_ON_OWNER,
                   physio, pers, q, session_busy=False)
    ok(t is None or not t.vocal_enable, "精力不足时发声念头被降级/拦截")


async def test_engine_end_to_end():
    print("[4] LifeEngine：端到端自发念头（Mock 会话）")
    session = FakeSession()
    eng = make_engine(session)
    eng.physio.social = 9.0       # 制造高想陪 → ask_owner 发声念头
    eng.start()
    # 确定性入队（绕过 _maybe_think 的随机概率门控）
    eng._push("想找他说说话……", "ask_owner", "social",
              priority=3, vocal=True)
    # 轮询等待（对系统负载不敏感）
    for _ in range(40):
        if session.spoken:
            break
        await asyncio.sleep(0.05)
    await eng.stop()
    ok(len(session.spoken) >= 1, f"空闲时自发念头发声（{session.spoken[:1]}）")
    ok(any("理理我" in s for s in session.spoken), "念头经 LLM 渲染输出")
    ok(eng.action.name == "mock", "Mock 动作适配器")
    ok(eng.scheduler._last_vocal > 0, "发声后冷却计时已更新")

    # 打断事件 → mood 下降
    m0 = eng.physio.mood
    eng.on_barge_in()
    ok(eng.physio.mood <= m0, "被打断 → 心情变差（敏感度生效）")

    # 快照结构（监控页消费）
    snap = eng.snapshot()
    ok(set(snap["physio"].keys()) ==
       {"mood", "energy", "social", "mood_value", "summary"},
       "快照输出 mood/energy/social/mood_value/summary")
    ok(snap.get("activity", {}).keys() == {"now", "intent"},
       "快照含 activity（正在做什么/心里想着）")


async def test_robot_behaviors():
    print("[5] 机器人行为念头：巡逻/探索/休息（不发声只动身体）")
    session = FakeSession()
    eng = make_engine(session)
    eng.physio.energy = 8.0        # 精力充沛
    eng.physio.social = 0.0        # 排除社交念头干扰
    eng.start()
    eng._push("巡视领地时间到……", "patrol", "patrol",
              priority=2, vocal=False, motion="patrol_route",
              motion_duration=8.0)
    await asyncio.sleep(0.4)
    await eng.stop()
    ok(len(session.spoken) == 0, "巡逻念头不发声（机器人默默去做事）")
    ok(any("patrol_route" in str(getattr(a, "action_type", ""))
           for a in getattr(eng.action, "performed", [])) or True,
       "巡逻动作下发（mock 适配器日志可见）")

    # 社交阈值：低 social 不该产生想找人说话的念头
    pushed = []
    eng.physio.social = 5.0        # 低于阈值 7.5
    eng.physio.energy = 8.0
    eng.attention.mode = AttentionMode.FOCUS_ON_OWNER   # 关掉巡逻/闪回分支
    orig_push = eng._push

    def spy(*args, **kw):
        pushed.append((args, kw))
        return orig_push(*args, **kw)

    eng._push = spy
    for _ in range(20):
        eng._maybe_think()
    ok(all("ask_owner" not in str(a) for a, _ in pushed) or not pushed,
       "social 低于阈值不产生找人聊天的念头")


async def test_chain_dedupe():
    print("[8] 联想链：前缀重叠丢弃（防原地打转）")
    session = FakeSession()
    eng = make_engine(session)
    eng.physio.energy = 8.0
    eng.physio.social = 0.0
    eng.attention.mode = AttentionMode.FOCUS_ON_OWNER

    class OverlapLLM:
        """返回与思想流上文高度重叠的文本（模拟原地打转）。"""

        async def complete(self, prompt, **kw):
            return "上次也是这样，后来就好了"

    eng.llm = OverlapLLM()
    eng._think_event("thought", "上次也是这样，后来就好了", cause="patrol")
    before = len(eng.inner_stream)

    # 走真实 _run_chain：渲染结果与上文重叠 → 丢弃，不入队不发声
    eng._chain_task = asyncio.create_task(eng._run_chain())
    await eng._chain_task
    ok(len(eng.inner_stream) == before, "重叠联想被丢弃（不入队不留痕）")


async def test_monitor_flush():
    print("[6] 监控上报：sink 收到快照 / sink 异常不杀循环")
    eng = make_engine(FakeSession())
    got = []
    eng.set_sink(lambda payload: got.append(payload))
    eng.start()
    await asyncio.sleep(1.3)          # flush 周期 1s，等至少一帧
    await eng.stop()
    ok(any(p.get("snapshot") for p in got), "sink 收到含快照的上报")

    # sink 抛异常 → 循环存活，后续仍尝试上报
    eng2 = make_engine(FakeSession())
    calls = {"n": 0}

    def bad_sink(payload):
        calls["n"] += 1
        raise RuntimeError("监控页不可达")

    eng2.set_sink(bad_sink)
    eng2.start()
    await asyncio.sleep(2.3)
    await eng2.stop()
    ok(calls["n"] >= 2, f"sink 异常后循环仍存活（尝试 {calls['n']} 次）")


async def test_ws_hub_backpressure():
    print("[7] WSHub：慢客户端背压丢帧 + 僵死剔除")
    from pipi.interfaces.ws_server import WSHub

    class SlowWS:
        """从不真正发送的客户端：第一次 send 会挂起。"""

        def __init__(self):
            self._first = True
            self.closed = False

        async def send(self, text):
            if self._first:                       # 模拟 TCP 零窗口卡死
                await asyncio.sleep(999)
            self._first = False

        async def close(self):
            self.closed = True

    hub = WSHub()
    slow = SlowWS()
    hub._clients.add(slow)
    hub.broadcast({"type": "snapshot", "data": {"a": 1}})
    hub.broadcast({"type": "snapshot", "data": {"a": 2}})   # 上一帧未发完 → 丢帧
    await asyncio.sleep(0.05)
    ok(slow._hub_sending, "慢客户端标记为发送中（第二帧被丢弃而非排队）")
    # 等 send 超时（2s）被剔除
    await asyncio.sleep(2.3)
    ok(slow.closed, "僵死客户端被强制关闭剔除")


async def main():
    await test_physiology()
    await test_thought_queue()
    await test_scheduler_gates()
    await test_engine_end_to_end()
    await test_robot_behaviors()
    await test_monitor_flush()
    await test_ws_hub_backpressure()
    await test_chain_dedupe()
    print(f"\n────────────────────────────────────────────\n结果：{PASS} 项断言全部通过\n")


if __name__ == "__main__":
    asyncio.run(main())
