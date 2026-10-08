#!/usr/bin/env python3
"""提醒 / 闹铃 / 待办（阶段 F）验收。

用法：python tests/integration/test_reminders.py
覆盖：意图解析（创建/查询/确认）、定时触发、repeat 推进、
静默窗口顺延、发声绑定与重试、确认闭环。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pipi.app import build_app
from pipi.infra import Config

PASS = 0


def ok(cond, msg):
    global PASS
    assert cond, f"FAIL: {msg}"
    PASS += 1
    print(f"  ✓ {msg}")


class FakeClock:
    """可控时钟：只实现 ReminderService 用到的 now()。"""

    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def advance(self, s):
        self.t += s


def ts_quiet_night():
    """今天 23:30 的时间戳（静默窗口 [23,7] 内）。"""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 23, 30, 0, 0, 0, -1))


def ts_morning(hour=8, minute=0):
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))


def main():
    app = build_app(Config.default(), sim=True)
    svc = app.reminders
    ok(svc is not None, "sim 装配下 ReminderService 存在")
    ok(svc.repo is app.repos.reminder, "ReminderRepo 已接入 Repos")

    # 独立用户，避免历史数据干扰
    stamp = int(time.time() * 1000)
    uid = f"rem_test{stamp}"
    app.repos.user.ensure(uid, time.time())

    clk = FakeClock(time.time())
    svc.clock = clk   # 注入可控时钟

    # ---- 意图解析：创建提醒（5 分钟后）----
    handled, reply = svc.try_parse(uid, "提醒我 5 分钟后喝水")
    ok(handled and "喝水" in reply, "创建提醒：解析命中且回复包含内容")
    r = max(svc.repo.list_active(uid), key=lambda it: it["id"])
    ok(r["kind"] == "reminder" and abs(r["remind_at"] - (clk.now() + 300)) < 2,
       "remind_at ≈ now+5min")

    # ---- 创建闹钟（8 点）----
    handled, reply = svc.try_parse(uid, "闹钟 8 点叫我起床")
    ok(handled and "闹钟" in reply, "创建闹钟：解析命中")
    r2 = max(svc.repo.list_active(uid), key=lambda it: it["id"])
    ok(r2["kind"] == "alarm", "kind=alarm 落库")

    # ---- 创建待办（无时间 → 默认 1 小时后）----
    handled, reply = svc.try_parse(uid, "记个待办：买牛奶")
    ok(handled and "待办" in reply, "创建待办：解析命中")
    r3 = max(svc.repo.list_active(uid), key=lambda it: it["id"])
    ok(r3["kind"] == "todo" and abs(r3["remind_at"] - (clk.now() + 3600)) < 2,
       "待办默认 remind_at = now+1h")

    # ---- 查询 ----
    handled, reply = svc.try_parse(uid, "有什么提醒")
    ok(handled and "喝水" in reply and "买牛奶" in reply, "查询列出全部活跃项")

    # ---- 定时触发：once 完成 ----
    ok(svc.repo.get(r["id"])["status"] == "pending", "触发前仍是 pending")
    clk.advance(301)                      # 过 5 分钟
    svc._tick()                           # 到期触发
    fired = svc.repo.get(r["id"])
    ok(fired["status"] == "done" and fired["completed_at"] is not None,
       "once 提醒触发后标记 done + 记录完成时间")
    ok(svc.repo.get(r["id"])["fired_count"] >= 1, "触发计数 ≥1")

    # ---- repeat daily：触发后推进 24h 且仍 pending ----
    rid = svc.repo.create(uid, "reminder", "每天喝水", clk.now() + 1, "daily", clk.now())
    clk.advance(2)
    svc._tick()
    daily = svc.repo.get(rid)
    ok(daily["status"] == "pending" and abs(daily["remind_at"] - (clk.now() + 86399)) < 3,
       "daily 触发后 remind_at 推进到次日，状态保持 pending")

    # ---- 确认闭环：最近被触发的一条可由"知道了"完成 ----
    rid2 = svc.repo.create(uid, "reminder", "关煤气", clk.now(), "once", clk.now())
    svc.repo.bump_fired(rid2)
    handled, reply = svc.try_parse(uid, "知道了")
    ok(handled and "记下了" in reply, "确认闭环：知道了 → 完成最近触发项")
    ok(svc.repo.get(rid2)["status"] == "done", "闭环后状态 done")

    # ---- 确认不误吞：无触发记录时"知道了"不拦截 ----
    handled, _ = svc.try_parse(uid, "知道了")
    ok(not handled, "无触发记录时「知道了」不拦截（回落正常对话）")

    # ---- 静默窗口顺延（非闹铃）----
    svc.quiet_hours = (23, 7)
    night_clk = FakeClock(ts_quiet_night())
    svc.clock = night_clk
    uid_n = f"rem_night{stamp}"          # 独立用户，避免与主流程交叉
    app.repos.user.ensure(uid_n, time.time())
    rid3 = svc.repo.create(uid_n, "reminder", "睡前提醒", night_clk.now() + 1, "once", night_clk.now())
    night_clk.advance(2)
    svc._tick()
    p = svc.repo.get(rid3)
    ok(p["status"] == "pending" and p["remind_at"] > night_clk.now(),
       "静默窗口内非闹铃顺延到窗口结束（不响、不完成）")
    # 闹铃在静默窗口仍触发
    rid4 = svc.repo.create(uid_n, "alarm", "午夜闹钟", night_clk.now() + 1, "once", night_clk.now())
    night_clk.advance(2)
    svc._tick()
    ok(svc.repo.get(rid4)["status"] == "done", "闹铃静默窗口准点必响")
    svc.clock = clk

    # ---- 发声绑定：speak 被调用；失败进入重试 ----
    spoken = []
    def fake_speak(text):
        spoken.append(text)
        return True
    svc.bind_speak(fake_speak)
    rid5 = svc.repo.create(uid, "reminder", "通知测试", clk.now() + 1, "once", clk.now())
    clk.advance(2)
    svc._tick()
    ok(spoken and "通知测试" in spoken[-1], "绑定 speak 后到期调用发声")
    # 失败重试
    svc.bind_speak(lambda t: False)
    rid6 = svc.repo.create(uid, "alarm", "重试测试", clk.now() + 1, "once", clk.now())
    clk.advance(2)
    svc._tick()
    retrying = svc.repo.get(rid6)
    ok(retrying["status"] == "pending" and retrying["fired_count"] >= 0,
       "发声失败时记录保持 pending（进入重试）")
    svc.bind_speak(lambda t: True)
    clk.advance(60)
    svc._tick()
    ok(svc.repo.get(rid6)["status"] == "done", "重试成功后完成")

    # ---- 取消 ----
    rid7 = svc.repo.create(uid, "todo", "取消测试", None, "once", clk.now())
    handled, reply = svc.try_parse(uid, "取消提醒")
    ok(handled and svc.repo.get(rid7)["status"] == "cancelled",
       "取消：最近活跃项置为 cancelled")

    print(f"\n全部通过（{PASS} 项断言）")


if __name__ == "__main__":
    main()
