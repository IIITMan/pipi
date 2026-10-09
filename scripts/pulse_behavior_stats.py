# -*- coding: utf-8 -*-
"""统计：行为(behavior)多久变一次、idle 占多少、意图维持多久。"""
import sys
sys.path.insert(0, r"D:\Work\pipi-robot\src")
from pipi.pulse.runtime import PulseRuntime


class FakeClock:
    def __init__(self, day=1, hour=8.0):
        self.day, self.hour = day, hour

    def now_hour(self):
        return self.hour

    def now_minute(self):
        return int((self.hour % 1.0) * 60)

    def now_day(self):
        return self.day

    def mono(self):
        return self.day * 86400.0 + self.hour * 3600.0


class Sess:
    is_busy = False

    def try_speak_spontaneous(self, t):
        return True


clk = FakeClock()
rt = PulseRuntime({"pulse": {"tick_s": 60.0, "vocal_prob": 0.0}},  # 关发声，纯行为
                  session=Sess(), clock=clk, mono=clk.mono)

last_motion, last_t = None, 0
runs = []         # (motion, 持续分钟)
motion_min = {}
t = 0             # 仿真分钟
for d in range(1, 4):                 # 3 天
    for m in range(24 * 60):          # 每分钟 1 拍（tick_s=60）
        h = m / 60.0
        clk.hour, clk.day = h, d
        rt.loop.owner_present = (8 <= h < 22)
        rt._beat()
        t += 1
        mo = rt._behavior["motion"]
        motion_min[mo] = motion_min.get(mo, 0) + 1
        if mo != last_motion:
            if last_motion is not None:
                runs.append((last_motion, t - last_t))
            last_motion, last_t = mo, t

tot = sum(motion_min.values())
print("行为占比(3天):")
for k, v in sorted(motion_min.items(), key=lambda x: -x[1]):
    print(f"  {k:10s} {v/tot*100:5.1f}%  (≈{v/60:.1f}h)")
print(f"\n行为切换次数: {len(runs)} (3天)")
print("各行为连续时长(分钟): 最短/中位/最长/次数")
for k in ("idle", "approach", "look", "explore", "retreat"):
    rr = [x[1] for x in runs if x[0] == k]
    if rr:
        rr.sort()
        print(f"  {k:10s} min={min(rr):6.1f} med={rr[len(rr)//2]:6.1f} "
              f"max={max(rr):6.1f}  n={len(rr)}")
