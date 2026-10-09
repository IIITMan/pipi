# -*- coding: utf-8 -*-
"""P0 验证：内心状态注入对话。构造三种处境，打印 inner_brief，
并渲染完整 system prompt 截取"此刻的状态"，证明内心真正进入对话。"""
import sys

sys.path.insert(0, r"D:\Work\pipi-robot\src")
from pipi.pulse.runtime import PulseRuntime
from pipi.providers.llm.prompt_builder import PromptBuilder
from pipi.domain.persona import PersonaSpec


class Clock:
    def __init__(self, hour):
        self.hour = hour

    def now_hour(self):
        return self.hour

    def now_minute(self):
        return int((self.hour % 1) * 60)

    def now_day(self):
        return 1

    def mono(self):
        return self.hour * 3600.0


class Sess:
    def __init__(self):
        self.is_busy = False
        self.state = None

    def try_speak_spontaneous(self, t):
        return True


class FakeProfile:
    def to_l1_block(self):
        return ""


def advance(rt, clk, minutes):
    for _ in range(minutes):
        rt._beat()
        clk.hour += 1.0 / 60.0


def build_rt(hour):
    clk = Clock(hour)
    sess = Sess()
    rt = PulseRuntime({"pulse": {"tick_s": 60.0}}, session=sess,
                      clock=clk, mono=clk.mono)
    return rt, clk


def show(title, rt):
    pb = PromptBuilder(PersonaSpec(id="pipi", name="皮皮"), "主人")
    sys_prompt = pb.build_system(
        current_time="--", profile=FakeProfile(),
        inner_state=rt.inner_brief())
    # 截取"此刻的状态"到"你记得的东西"之间
    a = sys_prompt.find("# 此刻的状态")
    b = sys_prompt.find("# 你记得的东西")
    print(f"\n========== {title} ==========")
    print(sys_prompt[a:b].strip())


# 场景 1：深夜 3 点，先入睡，再被人声叫醒
rt, clk = build_rt(3.0)
rt._beat()                 # 入睡
rt._inject("user_voice", {"dur": 5})   # 被叫醒
show("场景1 · 深夜3点被叫醒", rt)

# 场景 2：主人离开约 8 小时（思念）
rt, clk = build_rt(9.0)
rt._inject("owner_depart", {})
advance(rt, clk, 8 * 60)
show("场景2 · 主人离开8小时", rt)

# 场景 3：白天陌生人出现，刚被吓一跳
rt, clk = build_rt(14.0)
rt._inject("stranger", {})
show("场景3 · 刚被陌生人惊吓", rt)
