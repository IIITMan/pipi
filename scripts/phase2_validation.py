"""阶段 2 验证：控制车道 + 情绪推断伦理护栏（契约 ENGINE_CONTRACTS §5.4/§6.1）。

断言清单：
 1. idle/ignored 转移不再产生委屈（target=CALM、affection_delta=0）
 2. 吃醋学习开关：check_rival_opt_out 识别；rival_disabled=True 时不扣好感
 3. 被忽视降频：3 次 → 上限 0；2 次 → 减半；回应 → 恢复
 4. 沉默降频：silent ≥ 2×idle_hours → 上限再减半
 5. 静音门：/quiet 后自发发声被抑制、LLM 渲染不被唤醒（runtime 发声入口）
 6. 被忽视降频运行时接通：自发 10min 无回应 → ignored_observer 被调用

用法：python scripts/phase2_validation.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipi.domain.emotion import EmotionState, Mood, MOOD_TRANSITIONS
from pipi.capabilities.emotion_engine import EmotionEngine
from pipi.domain.persona import PersonaSpec
from pipi.domain.profile import UserProfile
from pipi.orchestration.proactive import ProactiveEngine
from pipi.pulse.runtime import PulseRuntime

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


class FakeClock:
    def __init__(self, hour: float = 15.0):
        self._h = hour

    def hour(self) -> float:
        return self._h

    def now_hour(self) -> float:
        return self._h

    def now_minute(self) -> int:
        return int((self._h % 1.0) * 60)

    def now_day(self) -> int:
        return 1

    def datetime(self):
        import datetime
        return datetime.datetime(2026, 10, 11, int(self._h), 0)

    def now(self) -> float:
        return self._h * 3600.0

    def mono(self) -> float:
        return self._h * 3600.0


class FakeRepo:
    def __init__(self, today_count: int = 0):
        self._count = today_count
        self.fired: dict[str, float] = {}

    def count_today(self, user_id: str, day_start: float) -> int:
        return self._count

    def last_fired(self, user_id: str, trigger: str) -> float | None:
        return self.fired.get(trigger)

    def log(self, *a, **k):
        pass


class FakeSession:
    """session 替身：支持静音门。"""

    def __init__(self):
        self.is_busy = False
        self._quiet = False
        self.spoken: list[str] = []

    @property
    def is_quiet(self) -> bool:
        return self._quiet

    def set_quiet(self, enabled: bool) -> None:
        self._quiet = enabled

    def try_speak_spontaneous(self, text: str) -> bool:
        if self._quiet:
            return False
        self.spoken.append(text)
        return True


def main() -> int:
    print("== 1. MOOD_TRANSITIONS：沉默/被忽视不再委屈 ==")
    t_idle = MOOD_TRANSITIONS["idle"]
    t_ign = MOOD_TRANSITIONS["ignored"]
    check("idle → CALM", t_idle.target == Mood.CALM, str(t_idle.target))
    check("idle 不扣好感", t_idle.affection_delta == 0.0)
    check("idle 陪伴需求仍累积", t_idle.need_delta >= 0.1, str(t_idle.need_delta))
    check("ignored → CALM", t_ign.target == Mood.CALM, str(t_ign.target))
    check("ignored 不扣好感/不累积需求",
          t_ign.affection_delta == 0.0 and t_ign.need_delta == 0.0)

    st = EmotionState()
    st.apply_by_key("idle")
    check("apply idle 后情绪为平静", st.mood == Mood.CALM, str(st.mood))
    check("apply idle 后好感不变", st.affection == 500.0, str(st.affection))
    st2 = EmotionState()
    st2.apply_by_key("ignored")
    check("apply ignored 后情绪为平静", st2.mood == Mood.CALM, str(st2.mood))

    print("== 2. 吃醋触发器学习开关 ==")
    eng = EmotionEngine(PersonaSpec(id="t", name="皮皮"))
    check("'别吃醋' 触发 opt-out", eng.check_rival_opt_out("别吃醋了"))
    check("'不喜欢这样' 触发 opt-out", eng.check_rival_opt_out("我不喜欢你这样"))
    check("'不喜欢你吃醋' 触发 opt-out",
          eng.check_rival_opt_out("我不喜欢你吃醋"))
    check("普通文本不误触", not eng.check_rival_opt_out("今天看见一只猫"))

    from pipi.capabilities.emotion_engine import EmotionSignal
    st3 = EmotionState()
    sig = EmotionSignal(has_rival=True)
    upd, trig = eng.update(st3, [sig], 1.0, rival_disabled=True)
    check("开关关闭 → 不吃醋", trig == "rival_disabled", trig)
    check("开关关闭 → 好感不扣", upd.affection == 500.0, str(upd.affection))
    st4 = EmotionState()
    upd2, trig2 = eng.update(st4, [sig], 1.0)
    check("开关开启 → 吃醋保留", trig2 == "mention_rival", trig2)
    check("吃醋仍扣好感（显式语义）", upd2.affection < 500.0, str(upd2.affection))

    print("== 3. 被忽视降频（更安静而非更委屈）==")
    clock = FakeClock()
    pe = ProactiveEngine(clock=clock, repo=FakeRepo(), daily_limit=4)
    check("初始上限 4", pe.effective_daily_limit() == 4)
    pe.note_ignored()
    pe.note_ignored()
    check("2 次被忽视 → 减半", pe.effective_daily_limit() == 2,
          str(pe.effective_daily_limit()))
    pe.note_ignored()
    check("3 次被忽视 → 归零", pe.effective_daily_limit() == 0,
          str(pe.effective_daily_limit()))
    pe.note_user_response(clock.now())
    check("用户回应 → 恢复", pe.effective_daily_limit() == 4)

    print("== 4. 沉默降频（idle → 上限减半）==")
    profile = UserProfile(user_id="u1")
    state = EmotionState()
    pe2 = ProactiveEngine(clock=clock, repo=FakeRepo(today_count=1),
                          daily_limit=3)
    now = clock.now()
    d = pe2.evaluate("u1", profile=profile, state=state,
                     last_user_message_at=now - 10 * 3600.0)
    check("沉默 10h（≥2×idle_hours）→ 上限减半并抑制",
          "上限" in d.suppressed_by, d.suppressed_by)
    d2 = pe2.evaluate("u1", profile=profile, state=state,
                      last_user_message_at=now - 3600.0)
    check("仅沉默 1h → 不触发降频上限", "上限" not in d2.suppressed_by,
          d2.suppressed_by)

    print("== 5. 静音门：/quiet 期间 LLM 不可唤醒主动发言 ==")
    sess = FakeSession()
    rt = PulseRuntime({"pulse": {"vocal_prob": 0.008}},
                      session=sess, clock=clock, mono=clock.mono)
    rendered = []

    async def fake_renderer(event):
        rendered.append(event)

    rt.vocal_renderer = fake_renderer
    snap = {
        "sleeping": False,
        "intent": SimpleNamespace(kind="loneliness"),
        "tension": SimpleNamespace(waves={"loneliness": 1.0}),
    }
    ok = sess.try_speak_spontaneous("我想你")
    check("未静音时自发发声可用", ok is True)
    sess.set_quiet(True)
    check("静音后自发发声被抑制",
          sess.try_speak_spontaneous("我想你") is False)
    rt._last_vocal = 0.0
    rt._last_event_logged = 0.0
    rt._ignored_flagged = False
    # 静音时 _maybe_vocal 不应触达渲染器（vocal_renderer 不被唤醒）
    rt._maybe_vocal(snap)
    check("静音期间不调用 LLM 渲染", len(rendered) == 0,
          f"rendered={len(rendered)}")

    print("== 6. 被忽视降频运行时接通 ==")
    sess2 = FakeSession()
    rt2 = PulseRuntime({"pulse": {"vocal_prob": 0.008}},
                       session=sess2, clock=clock, mono=clock.mono)
    ignored_calls = []
    rt2.set_ignored_observer(lambda: ignored_calls.append(1))
    m = clock.mono()
    rt2._last_vocal = m - 700.0          # 10 分钟前的自发发声
    rt2._last_event_logged = m - 700.0   # 且此后无任何用户回应
    rt2._ignored_flagged = False
    rt2._maybe_vocal(snap)
    check("自发 10min 无回应 → 触发降频回调", len(ignored_calls) == 1,
          str(len(ignored_calls)))
    rt2._maybe_vocal(snap)               # 不重复触发
    check("降频回调只触发一次", len(ignored_calls) == 1)
    rt2.observe_user_voice()             # 用户终于回应
    rt2._last_vocal = m - 700.0
    rt2._ignored_flagged = False
    rt2._maybe_vocal(snap)
    check("回应后不再重复降频", len(ignored_calls) == 1)

    print()
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
