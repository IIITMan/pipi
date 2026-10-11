"""LivingLoop —— PULSE 主循环编排（PULSE_DESIGN §7）。

P1 范围：快循环（Tension+派生呼吸）→ 行为尺度（Will 5s）→ 身体表达（Body）。
内部自持主体状态（owner 在场/分离时长/警觉/对话），外部只注入事件与昼夜时间
——这就是"生命有独立生活"（旧方案 6 条无法优化点之一）的起点。
"""

from __future__ import annotations

import time
from typing import Any

from pipi.pulse import derive
from pipi.pulse.organs.anticipation import Anticipation
from pipi.pulse.organs.bond import Bond
from pipi.pulse.organs.river import River
from pipi.pulse.organs.seed import Seed
from pipi.pulse.organs.tension import TensionField, TensionSnapshot
from pipi.pulse.organs.will import Intent, Will
from pipi.pulse.body import Body

MIN = 60.0


class LivingLoop:
    """一颗会搏动的心：器官编排 + 主体状态 + 事件注入。"""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.anticipation = Anticipation(cfg.get("anticipation"))
        self.tension = TensionField(cfg.get("tension"))
        self.will = Will(cfg.get("will"))
        self.bond = Bond(cfg.get("bond"))
        self.river = River(cfg.get("river"))
        self.seed = Seed(cfg.get("seed"))
        self.seed.set_depth(1)
        self.bond.set_days_since_adoption(0.2)     # 领养 0.2 天，认主关键期
        self.body = Body()

        # 派生展示状态（8→4：原 Affect/Rhythm 器官降为派生值，见 derive.py）
        self._mood_base = derive.MOOD_BASELINE   # 心境底色（事件 push，τ=1h 回归）
        self._mood_pulse = 0.0                   # 情绪波澜（事件 push，τ=60s 衰减）
        self._burst = 0.0                        # 惊吓/惊喜呼吸脉冲（指数衰减）
        self._arousal = 0.0                      # 唤醒度（呼吸驱动，τ=4s 滞后）
        self._breath_rate = derive.BASE_RATE_BPM
        self._breath_depth = 0.4
        self._breath_phase = 0.0

        # 主体状态（内部自持）
        self.owner_present = True
        self.sep_min = 0.0
        self.alert_until = 0.0
        self.curious_until = 0.0
        self.quiet_until = 0.0
        self.chatting_until = 0.0
        self.wake_until = 0.0        # 夜间被唤醒后保持清醒到该仿真秒（睡眠可被打断）
        self.sim_seconds = 0.0
        self.sleeping = False          # 睡眠状态（由困意曲线+滞回决定，连续过渡）
        self._was_sleeping = False
        self._startle = False
        self._startle_t = 0.0
        self._since_will = 0.0         # Will 5s 判定累加器（任意 dt 不漏判）
        self._last_useless = -1e9      # 无用的事冷却
        self._miss_return_done = False  # 今晚"主人该回未回"担忧已触发

        # 统计（demo/报告用）
        self.stats = {"startle": 0, "curious": 0, "intent_changes": 0,
                      "useless": 0, "greet": 0,
                      "missed_return": 0, "early_reunion": 0,
                      "neglected": 0, "long_reunion": 0}
        self._prev_intent: str | None = None
        self._intent = None            # 当前意图（改主意前持续成立，行为不闪回 idle）

        # P4：当天日记素材（入睡时成文 + 漂移种子）
        self._diary: list[dict] = []
        self._weather_today: dict[str, int] = {}
        self._longing_peak_today = 0.0

    # ------------------------------------------------------------- 派生心境/呼吸
    def push_mood_base(self, v: float) -> None:
        """心境底色事件（对话开心/低落、重聚振奋）。"""
        self._mood_base = derive.clamp(self._mood_base + v, -1.0, 1.0)

    def push_mood_pulse(self, v: float) -> None:
        """情绪波澜事件（惊吓/惊喜/受挫）。"""
        self._mood_pulse = derive.clamp(self._mood_pulse + v, 0.0, 1.0)

    def trigger_burst(self, amp: float) -> None:
        """惊吓/惊喜呼吸脉冲（指数衰减，取最大冲击）。"""
        self._burst = max(self._burst, derive.clamp(amp, 0.0, 1.0))

    def _derive_weather(self) -> dict:
        """心境 → 天气（唯一权威映射，与原 affect 语义一致）。"""
        return {"base": round(self._mood_base, 3),
                "pulse": round(self._mood_pulse, 3),
                "label": derive.weather_label(self._mood_base,
                                              self._mood_pulse)}

    # ------------------------------------------------------------- 当天日记
    def _note(self, kind: str, minute: float, text: str = "") -> None:
        """行为刻度写入生命流 + 当天日记（不可逆，供入睡成文）。"""
        self.river.record({"kind": kind, "t": round(minute, 1), "text": text})
        self._diary.append({"kind": kind, "t": round(minute, 1), "text": text})

    # ------------------------------------------------------------- 事件注入
    def inject(self, kind: str, minute: float | None = None, **kw) -> str:
        """统一事件入口。返回身体反应文本（供实时日志）。

        minute=当前仿真分钟（供作息学习/提前判定）；缺省回退到 sim_seconds
        换算（仅当外部不提供时钟时使用）。
        """
        if minute is None:
            minute = (self.sim_seconds / MIN) % 1440.0
        if kind == "owner_depart":
            self.owner_present = False
            self.sep_min = 0.0
            self.anticipation.learn("owner_morning_depart", minute)
            self._note("owner_depart", minute)
            return "目送主人出门"
        if kind == "owner_arrive":
            self.owner_present = True
            self._miss_return_done = True
            # P2：意外提前——仅对"傍晚例行归"学习与判定（16:00-23:00），
            # 白天任意时段的回家不走惊喜（防止早间/午间到达污染傍晚预期）
            hour = (minute % 1440.0) / 60.0
            early = 0.0
            if 16.0 <= hour < 23.0:
                exp = self.anticipation.expected("owner_evening_return")
                self.anticipation.learn("owner_evening_return", minute)
                early = (exp - minute) if exp is not None else 0.0
            if early > 30.0:
                self.trigger_burst(0.5)
                self.push_mood_base(0.25)
                self.tension.push("attachment", 0.20)
                self.stats["early_reunion"] += 1
                self._note("early_reunion", minute)
                # P3：早回叠加思念（久别早回 → "又惊又喜 + 想死你了"）
                if self.sep_min >= self.bond.cfg["missed_threshold_h"] * MIN:
                    return "又惊又喜，一路小跑迎上去——想死你了"
                return "又惊又喜，一路小跑迎上去"
            # P3：重聚情绪落差（按分离时长分级：短离/想念/久别）
            rr = self.bond.reunion_response(
                self.sep_min, clinginess=self.seed.profile()["clinginess"])
            self.push_mood_base(rr["mood"])
            self.tension.push("attachment", rr["attach"])
            self.tension.push("loneliness", -0.6)
            if rr["trust_gain"] > 0:
                self.bond.trust = min(self.bond.cfg["trust_max"],
                                      self.bond.trust + rr["trust_gain"])
            self.stats["greet"] += 1
            if rr["level"] == "long_away":
                self.stats["long_reunion"] += 1
                self._note("long_away", minute)
            else:
                self._note("greet", minute)
            return rr["text"]
        if kind == "owner_ignored":
            # P3：冷淡重聚——回来了却不理 → 失落 + 信任损失
            self.owner_present = True
            self._miss_return_done = True
            ng = self.bond.neglected()
            self.push_mood_base(ng["mood"])
            self.tension.push("attachment", ng["attach"])
            self.stats["neglected"] += 1
            self._note("owner_ignored", minute)
            return ng["text"]
        if kind == "user_voice":
            self.chatting_until = self.sim_seconds + kw.get("dur", 5.0) * MIN
            self.tension.push("loneliness", -0.45)
            self.push_mood_base(0.20)
            self.bond.interact()
            # 夜间被唤醒聊天：对话结束后再保持（dur+10）分钟清醒，然后睡回去
            self.wake(kw.get("dur", 5.0) + 10.0)
            self._note("user_voice", minute)
            return "开心地回应"
        if kind == "quiet_home":
            self.quiet_until = self.sim_seconds + kw.get("dur", 60.0) * MIN
            self.owner_present = True
            self._note("quiet_home", minute)
            return "安静陪着主人"
        if kind == "stranger":
            sur = self.anticipation.update("stranger", 1.0)
            self.anticipation.learn("stranger", 1.0)
            self.alert_until = self.sim_seconds + 30 * MIN
            self.curious_until = self.sim_seconds + 10 * MIN
            if sur.kind == "startle":
                self.stats["startle"] += 1
                self._do_startle()
                self.will.interrupt()      # P2：惊吓打断当前意图
                self.seed.drift("sensitivity", +1.0)
                self._note("stranger", minute)
                return "身体一僵！警觉地盯着门口"
            if sur.kind == "curious":
                self.stats["curious"] += 1
                self._note("stranger", minute)
                return "歪头打量来人"
            self._note("stranger", minute)
            return "注意到有人"
        if kind == "owner_sad":
            self.anticipation.update("owner_expression", 1.0)
            self.anticipation.learn("owner_expression", 1.0)
            self.push_mood_base(-0.30)
            self.tension.push("attachment", +0.25)
            self.bond.interact()
            self._note("owner_sad", minute)
            return "安静地挨到主人身边"
        if kind == "night_noise":
            sur = self.anticipation.update("night_noise", 1.0)
            self.anticipation.learn("night_noise", 1.0)
            self.alert_until = self.sim_seconds + 20 * MIN
            if sur.kind == "startle":
                self.stats["startle"] += 1
                self._do_startle()
                self.will.interrupt()      # P2：惊吓打断当前意图
                self.seed.drift("sensitivity", +0.8)
                self._note("night_noise", minute)
                return "从睡梦中惊醒，竖起耳朵"
            self._note("night_noise", minute)
            return "翻了个身"
        return ""

    # ------------------------------------------------------------- 外部天气
    # 真实天气（WeatherService 类别）→ 心境：持续状态调制，幅度克制
    # （±0.05 量级，远小于对话/重聚事件 ±0.1~0.3），且类别变化才触发，
    # 不会把心情天天钉死在一种天气上。base 仍按 τ=15min 回归平静基线。
    _WEATHER_BASE = {
        "sunny": 0.02, "cloudy": 0.0, "overcast": -0.02,
        "fog": -0.03, "rain": -0.04, "snow": 0.03, "thunder": -0.05,
    }
    _WEATHER_TEXT = {
        "sunny": "外面阳光正好，心里也跟着亮堂堂的",
        "cloudy": "外面多云，不晴不雨，挺平静的",
        "overcast": "外面阴天，光线暗暗的，有点想窝着",
        "fog": "外面起雾了，模模糊糊的，有点犯懒",
        "rain": "外面在下雨，心里也跟着闷闷的，更想待在屋里",
        "snow": "外面下雪了！好稀奇，有点想趴到窗边看",
        "thunder": "外面在打雷，心里跟着一跳一跳的",
    }

    def observe_weather(self, kind: str) -> str:
        """真实外部天气 → 心境（类别变化时由 runtime 调用）。

        阴雨雾天：底色微沉 + 思念微涨（更想主人陪着）；
        雷雨：额外一点波澜 + 安全感微降；雪：稀奇，底色微亮。
        记入当天日记（生命流留痕），供入睡成文。
        """
        d = self._WEATHER_BASE.get(kind)
        if d is None:
            return ""
        self.push_mood_base(d)
        if kind == "thunder":
            self.push_mood_pulse(0.12)
            self.tension.push("security", -0.15)
        if kind in ("rain", "fog", "overcast"):
            self.tension.push("loneliness", 0.06)
        text = self._WEATHER_TEXT.get(kind, "")
        self._note("weather", (self.sim_seconds / MIN) % 1440.0, text)
        return text

    def _do_startle(self) -> None:
        self._startle = True
        self._startle_t = self.sim_seconds
        self.trigger_burst(0.9)
        self.push_mood_pulse(0.60)
        self.tension.push("security", -0.45)
        self.wake(20.0)      # 吓醒后 20 分钟内不再入睡（白天无害，本就清醒）

    # ------------------------------------------------------------- 情境评估
    @staticmethod
    def _sleepiness_curve(hour: float) -> float:
        """连续困意曲线（分钟级渐变，非阶跃）：
        22:00 起困意渐涨 → 23:30 深睡；6:30 起渐醒 → 7:30 全醒。"""
        if hour < 6.5:
            return 0.85
        if hour < 7.5:
            return 0.85 - (hour - 6.5) * 0.75     # → 0.10
        if hour < 22.0:
            return 0.08
        if hour < 23.5:
            return 0.08 + (hour - 22.0) * 0.5133   # → 0.85
        return 0.85

    def sleeping_at(self, hour: float) -> bool:
        """供 demo 判断行步进/昼夜统计（不修改内部状态）。"""
        return self._sleepiness_curve(hour) > 0.55

    def is_sleeping(self, hour: float) -> bool:
        """当前是否真的在睡：该睡了 **且** 没有被唤醒保持（夜间对话会打断睡眠，
        聊完保持清醒若干分钟再自然睡回去）。"""
        return self.sleeping_at(hour) and self.sim_seconds >= self.wake_until

    def wake(self, keep_minutes: float = 10.0) -> None:
        """被唤醒（夜间对话/打断/吓醒）：再保持 keep_minutes 分钟清醒，然后睡回去。"""
        self.wake_until = self.sim_seconds + keep_minutes * 60.0

    def _targets(self, hour: float) -> dict:
        sl = self._sleepiness_curve(hour)
        if self.sleeping:
            return {"loneliness": 0.05, "curiosity": 0.15,
                    "sleepiness": max(0.80, sl),
                    "security": 0.60, "attachment": 0.10}
        if sl > 0.25:
            # 入睡前/醒来后过渡：困意渐涨，依恋还在（"想陪主人但困了"）
            return {"loneliness": 0.05, "curiosity": 0.20,
                    "sleepiness": sl, "security": 0.55, "attachment": 0.60}
        lon = 0.05
        if not self.owner_present:
            # P3：思念曲线（先缓后急 × 个体 × 信任）驱动孤独目标
            lon = min(0.75, 0.35 + self.bond.longing(
                self.sep_min, clinginess=self.seed.profile()["clinginess"]) * 0.5)
        t = {
            "loneliness": lon,
            "curiosity": 0.15 if self.sim_seconds < self.quiet_until
            else (0.55 if self.sim_seconds < self.curious_until else 0.20),
            "sleepiness": 0.08,
            "security": 0.12 if self.sim_seconds < self.alert_until
            else (0.70 if self.owner_present else 0.55),
            "attachment": 0.35 if self.sim_seconds < self.quiet_until
            else (0.60 if self.owner_present else 0.20),
        }
        if self.sim_seconds < self.chatting_until:
            t["loneliness"] = 0.02
            t["attachment"] = 0.60
        return t

    # ------------------------------------------------------------- 主循环一步
    def tick(self, dt: float, hour: float) -> dict:
        """一帧：情境 → 器官 → 意图 → 身体。返回日志元素。"""
        self.sim_seconds += dt
        if not self.owner_present:
            self.sep_min += dt / MIN

        # P2：错过预期——主人该回未回（19:30-20:30 窗口）→ 担忧
        missed = False
        if not self.owner_present and not self._miss_return_done \
                and 19.5 <= hour < 20.5:
            self._miss_return_done = True
            missed = True
            self.tension.push("loneliness", 0.22)
            self.tension.push("security", -0.15)
            self.push_mood_base(-0.12)
            self.stats["missed_return"] += 1

        # 睡眠状态：困意曲线 + 滞回（入睡需 sl>0.62，醒需 sl<0.50）
        # 唤醒保持（夜间对话/吓醒）期间不睡：已入睡也会醒来，保持期过后睡回去
        sl = self._sleepiness_curve(hour)
        awake_kept = self.sim_seconds < self.wake_until
        if self.sleeping:
            self.sleeping = sl > 0.50 and not awake_kept
        else:
            self.sleeping = sl > 0.62 and not awake_kept
        fell_asleep = self.sleeping and not self._was_sleeping
        woke_up = not self.sleeping and self._was_sleeping
        self._was_sleeping = self.sleeping

        targets = self._targets(hour)
        # 惊吓脉冲只持续一瞬间（跟随 arousal 自然回落）
        if self._startle and self.sim_seconds - self._startle_t > 30.0:
            self._startle = False

        arousal = self.tension.total * (0.4 if self.sleeping else 1.0)
        self._arousal = arousal
        self._burst = derive.burst_decay(self._burst, dt)
        breath = derive.breath_step(
            self._breath_rate, self._breath_depth, self._breath_phase,
            arousal, self._burst, dt, sleeping=self.sleeping)
        self._breath_rate = breath["rate_bpm"]
        self._breath_depth = breath["depth"]
        self._breath_phase = breath["phase"]
        snap = self.tension.update(dt, targets, sleeping=self.sleeping)
        self._mood_base, self._mood_pulse = derive.decay_mood(
            self._mood_base, self._mood_pulse, dt)
        weather = self._derive_weather()
        # P4：当天天气/思念素材（入睡成文用）
        self._weather_today[weather["label"]] = self._weather_today.get(
            weather["label"], 0) + 1
        if not self.owner_present:
            lg = self.bond.longing(
                self.sep_min, clinginess=self.seed.profile()["clinginess"])
            self._longing_peak_today = max(self._longing_peak_today, lg)

        # 行为尺度：Will 5s 判定（累加器，任意 dt 都按 5s 粒度不漏判）
        # 意图在"改主意"前持续成立：will.current 是唯一权威，
        # decide 返回 None（未变）时保持，而非当作"没有意图"。
        self._since_will += dt
        while self._since_will >= 5.0:
            self._since_will -= 5.0
            new = self.will.decide(snap)
            if new is not None and new.kind == "useless":
                if self.sim_seconds - self._last_useless >= \
                        self.will.cfg["useless_cooldown_s"]:
                    self._last_useless = self.sim_seconds
                    self.stats["useless"] += 1
                else:
                    self.will.current = None   # 冷却中：不做无用的事，回 idle
            if new is not None and new.kind != "abandoned":
                if self._prev_intent is not None \
                        and self._prev_intent != new.kind:
                    self.stats["intent_changes"] += 1
                self._prev_intent = new.kind
        intent = self.will.current
        self._intent = intent

        body = self.body.express(breath, snap, weather, intent,
                                 startle=self._startle)
        if fell_asleep:
            body["act"] = "眼皮打架，蜷起来睡了"
        elif woke_up:
            body["act"] = "伸了个懒腰，睡醒了"
        return {
            "breath": breath,
            "tension": snap,
            "weather": weather,
            "intent": intent,
            "body": body,
            "sleeping": self.sleeping,
            "missed": missed,
        }

    # ------------------------------------------------------------- 快照/整理
    def consolidate_night(self) -> str:
        """入睡整理（P4：真实经历成文 + 种子按经历漂移，零 LLM）。

        流程：收集当天日记素材 → 按经历漂移种子（×塑性）→ River 生成叙事
        → 拼接成长句；素材清空，新的一天从白纸开始。
        """
        kinds: dict[str, int] = {}
        for e in self._diary:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        diary = {"kinds": kinds, "weather": dict(self._weather_today),
                 "longing_peak": self._longing_peak_today,
                 "events": list(self._diary)}
        drift = self._daily_drift(kinds, diary["longing_peak"])
        for axis, d in drift.items():
            self.seed.drift(axis, d)

        n = self.river.consolidate_night(diary)
        self.seed.set_depth(self.river.self_depth)
        self._miss_return_done = False   # 新的一天，重新期待
        ev = self.bond.advance_days(1.0)
        if ev is not None:
            self.push_mood_base(ev["mood"])
            self.tension.push("attachment", ev["attach"])
            n = n + "\n「" + ev["text"] + "」"

        growth = self._growth_sentence(drift)
        n = n + "\n" + growth
        self._weather_today.clear()
        self._longing_peak_today = 0.0
        self._diary.clear()
        return n

    def _daily_drift(self, kinds: dict[str, int], longing_peak: float) -> dict:
        """按当天经历给出种子漂移量（方向 × 幅度；幅度经塑性衰减，缓慢成长）。"""
        d: dict[str, float] = {}
        stim = kinds.get("night_noise", 0) + kinds.get("stranger", 0)
        if stim >= 1:
            d["sensitivity"] = 0.05           # 有刺激的一天 → 敏感一点
        if stim >= 2:
            d["reactivity"] = 0.04            # 刺激 ≥2 次 → 反应更机敏
        if kinds.get("stranger", 0) >= 1:
            d["curiosity"] = 0.03             # 接触陌生事物 → 好奇
        if longing_peak >= 0.85:
            d["clinginess"] = 0.05           # 思念很重 → 更黏
        elif longing_peak >= 0.4:
            d["clinginess"] = 0.02
        if kinds.get("user_voice", 0) + kinds.get("owner_sad", 0) >= 3:
            d["boldness"] = 0.02             # 与主人相处多 → 更自在
        if kinds.get("owner_ignored", 0) >= 2:
            d["clinginess"] = d.get("clinginess", 0) - 0.03  # 被冷落 → 退回
        return d

    def _growth_sentence(self, drift: dict[str, float]) -> str:
        """成长句：今天经历了这些，我变成了怎样一点点不同的自己。"""
        if not drift:
            return "今天的我，和昨天差不多。"
        axis = max(drift.items(), key=lambda x: abs(x[1]))[0]
        delta = drift[axis]
        words = {
            "sensitivity": ("更敏感了，风吹草动都容易让我紧张",
                            "没那么一惊一乍了"),
            "reactivity": ("反应更快了，一点动静就能听见",
                           "反应沉稳了一些"),
            "curiosity": ("更好奇了，总想弄明白外面的动静",
                          "对外面的世界没那么好奇了"),
            "clinginess": ("更黏你了，你不在的时候老是想你",
                           "没那么依赖你了"),
            "boldness": ("胆子大了一点，敢自己待着了",
                         "更谨慎了一点"),
        }
        pick = words.get(axis, ("变了一点点", "变了一点点"))
        return f"经历了这些，我好像{pick[0 if delta > 0 else 1]}。"

    def snapshot(self) -> dict:
        snap = self.tension.snapshot(sleeping=self.sleeping)
        weather = self._derive_weather()
        return {
            "t": round(self.sim_seconds / MIN, 1),
            "breath": {"rate": self._breath_rate, "depth": self._breath_depth},
            "tension": snap.waves,
            "struggle": snap.struggle,
            "weather": weather,
            "trust": round(self.bond.trust, 2),
            "self_depth": self.river.self_depth,
            "owner_present": self.owner_present,
        }
