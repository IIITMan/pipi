"""真实天气 → PULSE 生命模块 同步验证。

覆盖：
  1. WeatherService.subscribe / _notify（类别变化才广播）
  2. loop.observe_weather 数值调制（克制幅度 / 雷雨波澜 / 阴雨思念 / 日记留痕）
  3. runtime._sync_weather（类别变化才触发、外部天气进快照与内心注入）
  4. 订阅回调（对话查询触发 → 同步）
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipi.capabilities.weather import KIND_ZH, WeatherService
from pipi.pulse.loop import LivingLoop
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


class FakeWeather:
    """模拟 WeatherService：kind() + subscribe() 契约。"""

    def __init__(self):
        self._k: str | None = None
        self._subs = []

    def set(self, k: str | None):
        self._k = k

    def kind(self):
        return self._k

    def subscribe(self, fn):
        self._subs.append(fn)

    def fire(self, kind: str):
        for fn in list(self._subs):
            fn(kind)


print("== 1. 类别中文映射 ==")
check("KIND_ZH 中文", KIND_ZH["rain"] == "雨" and KIND_ZH["thunder"] == "雷雨")

print("== 2. WeatherService 订阅广播 ==")
svc = WeatherService(city="测试市")
got: list[str] = []
svc.subscribe(got.append)
svc._notify("rain")
svc._notify("rain")          # 同类不重推
svc._notify("sunny")
check("类别变化才广播", got == ["rain", "sunny"], f"got={got}")
check("kind() 初始 None", svc.kind() is None)

print("== 2. loop.observe_weather 调制 ==")
loop = LivingLoop()
base0 = loop._mood_base
n0 = len(loop._diary)
lon0 = loop.tension.values.get("loneliness", 0.0)
text = loop.observe_weather("rain")
check("雨 → 底色 -0.04", math.isclose(loop._mood_base, base0 - 0.04, abs_tol=1e-9),
      f"{base0:.2f}→{loop._mood_base:.2f}")
check("雨 → 思念微涨 +0.06",
      math.isclose(loop.tension.values.get("loneliness", 0.0), lon0 + 0.06, abs_tol=1e-9),
      f"{lon0:.2f}→{loop.tension.values.get('loneliness', 0.0):.2f}")
check("雨 → 日记留痕", len(loop._diary) == n0 + 1)
check("雨 → 有描述", bool(text) and "下雨" in text)

loop2 = LivingLoop()
b0 = loop2._mood_base
p0 = loop2._mood_pulse
s0 = loop2.tension.values.get("security", 0.0)
loop2.observe_weather("thunder")
check("雷雨 → 底色 -0.05", math.isclose(loop2._mood_base, b0 - 0.05, abs_tol=1e-9))
check("雷雨 → 波澜 +0.12", math.isclose(loop2._mood_pulse, p0 + 0.12, abs_tol=1e-9))
check("雷雨 → 安全感 -0.15", math.isclose(loop2.tension.values.get("security", 0.0), s0 - 0.15, abs_tol=1e-9))

loop3 = LivingLoop()
b0 = loop3._mood_base
loop3.observe_weather("sunny")
check("晴 → 底色 +0.02", math.isclose(loop3._mood_base, b0 + 0.02, abs_tol=1e-9))
check("未知类别不生效", loop3.observe_weather("hail") == "" and
      math.isclose(loop3._mood_base, b0 + 0.02, abs_tol=1e-9))
check("幅度克制(≤0.05)", abs(loop._mood_base - 0.35) <= 0.05 and
      abs(loop2._mood_base - 0.35) <= 0.05 and abs(loop3._mood_base - 0.35) <= 0.05)

print("== 3. runtime 同步（类别变化才触发） ==")
fw = FakeWeather()
rt = PulseRuntime({"pulse": {}, "weather": {"sync_interval_s": 1800}},
                  weather=fw)
rt._sync_weather()                      # 初始 kind=None → 不动
check("未查询天气前不注入", rt._weather_ext == "" and len(rt.loop._diary) == 0)

fw.set("rain")
rt._sync_weather()
check("雨 → 外部天气入快照", rt.snapshot()["pulse"]["weather_ext"] == "雨")
check("雨 → 事件日志留痕", any(e["kind"] == "weather" for e in rt._outbox))
n_diary = len(rt.loop._diary)
rt._sync_weather()                      # 类别未变 → 不重复
check("类别不变不重复注入", len(rt.loop._diary) == n_diary)
check("inner_brief 含外面天气", "外面雨" in rt.inner_brief())

fw.set("sunny")
rt._sync_weather()
check("晴 → 快照更新", rt.snapshot()["pulse"]["weather_ext"] == "晴")

print("== 4. 订阅回调（对话查询触发） ==")
rt2 = PulseRuntime({"pulse": {}, "weather": {}}, weather=fw)
fw.subscribe(rt2._on_weather_change)    # runtime.start() 里做的同一件事
fw.set("overcast")
fw.fire("overcast")
check("订阅触发 → 阴天入快照", rt2.snapshot()["pulse"]["weather_ext"] == "阴")
check("订阅触发 → 心境已调", any(e["kind"] == "weather" for e in rt2._outbox))

print(f"\n结果 {PASS}/{PASS + FAIL}")
sys.exit(0 if FAIL == 0 else 1)
