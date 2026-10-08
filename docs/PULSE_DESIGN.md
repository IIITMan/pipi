# PULSE · 心搏 —— 生命模块重设计（v0.1 设计文档）

> 状态：设计定稿，待实施
> 范围：**完全取代**现有 `src/pipi/life/`（保留一个版本后废弃），新包 `src/pipi/pulse/`
> 一句话：一个会搏动、会生长的活体内核 —— 一个生命循环 × 八个器官 × 三个时间尺度
> 复用：vision / memory / 语音 / reminders 全部作为器官输入，不重复建设

---

## 1. 设计哲学

真实生命的内核不是情绪数值、不是随机抛骰子、也不是 LLM 台词，而是三样东西：

1. **自稳态张力** —— 为维持自身而产生的"劲"（困了要睡、孤独要找、饿了要补）；
2. **对下一刻的预测** —— 预测被违反时惊讶/好奇/警觉，应验时安心；
3. **不可逆的具身时间线** —— 昨天和今天连续，"我"因经历而变厚，不可重置。

PULSE 据此统一为**持续搏动的生命循环**，绝大部分是本地确定性动力学模拟，
LLM 只在"需要变成语言"时低频介入（发声渲染 + 每日一次睡眠叙事）。

---

## 2. 生命循环（Living Loop）

像心跳一样不停重复的七拍：

```
感受 → 预测 → 张力 → 情感 → 意愿 → 行动 → 印记
│      │      │      │      │      │      │
感知/内部  预测误差  驱力波  身体天气  想干嘛  身体/声音  写进生命流
          →惊讶    竞争    底色/波澜  允许发呆  或仅内心  更新预测/关系/自我
```

- 快循环 1Hz（可配）；行为尺度 5s；发育尺度 1h。
- 任何一拍的单次异常只记日志、循环继续（沿用"生命不能静默停止"原则）。
- 无外部输入时循环**照常搏动**（呼吸、困意、潮汐仍在），只是不"强行思考"。

---

## 3. 三个嵌套时间尺度

| 尺度 | 周期 | 器官 | 对应生命活动 |
|---|---|---|---|
| 即时（反射） | 0.1–1s | Rhythm · Anticipation | 呼吸心跳波形、惊吓/眨眼/躲避、惊讶 |
| 行为（自主） | 5s–分钟 | Tension · Affect · Will | 驱力评估、意愿形成、巡逻/探索/发呆 |
| 生命（发育） | 小时–天 | Bond · River · Seed | 思念曲线、睡眠整理做梦、个性发育 |

---

## 4. 八个器官（数值模型与接口）

> 全部数值默认值即 `pulse/config.py` 的默认参数，均可在 `life.pulse.*` 配置段覆盖。
> 所有 `tick()` 均为纯函数式更新（输入 dt + 事件，输出快照），便于单测。

### 4.1 Rhythm 节律（即时）—— 它真的在呼吸

**目的**：给整个系统一个真实的"身体节奏"，呼吸/心跳是**运行中的波形**而非状态位。

| 状态 | 类型 | 默认值 |
|---|---|---|
| phase | float ∈ [0,1) | 0（呼吸相位） |
| rate_bpm | float | 12（平静）；随 arousal 上调至 24 |
| depth | float ∈ [0.4,1.0] | 0.4（安静浅呼吸）→ 1.0（兴奋深呼吸） |
| heartbeat_bpm | int | rate_bpm × 4 |

**动力学**：`phase += dt × rate_bpm / 60`；`rate_bpm` 由 `arousal`（= Tension 总和 + 外部刺激）平滑追随（一阶滞后 τ=4s）；波形 = `depth × sin(2π·phase) + 微噪声`。

**身心耦合**：arousal ↑ → 呼吸快而深；反之呼吸深浅经 Affect 底色回灌（松弛呼吸 → 底色回暖）。

**接口**
```python
class Rhythm:
    def tick(self, dt: float, arousal: float) -> BreathSample: ...
    def trigger_arousal(self, pulse: float) -> None: ...   # 外部刺激（惊吓/惊喜）
    # BreathSample{phase: float, depth: float, rate_bpm: float}
```

### 4.2 Anticipation 预感（即时）—— 只被"意外"触发

**目的**：惊讶/好奇/安心有真实因果：预测被违反才惊讶，应验才安心。

| 状态 | 类型 | 默认值 |
|---|---|---|
| predictions | dict[key → {expected, confidence}] | 空表（随经验学习） |
| surprise | float ∈ [0,1] | 0 |

**动力学**：`surprise = |observed − expected| × (1 − confidence)`。
- surprise > 0.55 → 惊讶（激活受惊反应，快循环突发脉冲）
- 0.25 < surprise ≤ 0.55 → 好奇（探询念头，Tension.curiosity 涟漪）
- surprise ≤ 0.25 → 安心（Tension.security 目标下调，呼吸放缓）

**学习（印记阶段写入）**：`learn(key, observed)` 用滑动平均更新 expected，confidence 随样本数升到上限 0.9。典型键：`owner_at(~7点)`、`door_sound→owner_arrive`、`idle_duration`。

**接口**
```python
class Anticipation:
    def update(self, observation) -> Surprise: ...   # Surprise{level, kind: calm|curious|startle}
    def learn(self, key: str, observed: float) -> None: ...
```

### 4.3 Tension 潮汐（行为）—— 会涨落、有惯性、互相竞争的驱力波

**目的**：取代 mood/energy/social 三轴。驱力是**波**：有惯性、有涟漪、互相竞争。"又困又想陪"的挣扎是结构性的，不是台词。

| 波 | 含义 | 目标来源 | 冷却/增益 |
|---|---|---|---|
| loneliness 孤独 | 想有人陪 | 独处时长、Bond 思念曲线 | 对话即大幅回落 |
| curiosity 好奇 | 想看看发生了什么 | Anticipation 好奇、陌生刺激 | 探索后回落 |
| sleepiness 困 | 想睡 | 昼夜节律（夜晚 target↑） | 睡眠后重置 |
| security 安全 | 安心程度（越低越警觉） | 陌生人/噪声/独处深夜 | 主人陪伴回升 |
| attachment 依恋 | 想靠近主人 | 与 owner 距离、Bond trust | 靠近/互动回落 |

**动力学（每波独立，但共享事件）**
```
value += (target − value) · k · dt            # k=惯性系数，波越慢越有"性格"
value += Σ ripple                             # 事件冲击：高斯脉冲，指数衰减 τ
struggle = 1 − |top1 − top2|                  # 前两名接近度 → "内心挣扎度"
```

**接口**
```python
class TensionField:
    def update(self, dt: float, events) -> TensionSnapshot: ...
    def push(self, wave: str, magnitude: float) -> None: ...
    # TensionSnapshot{dominant: str, struggle: float, waves: dict[str,float]}
```

### 4.4 Affect 天气（行为）—— 连续的身体天气，不是表情标签

**目的**：情感是**双时间尺度**的连续场，通过呼吸/姿态/眼神表达，而非枚举标签。

| 状态 | 类型 | 默认值 |
|---|---|---|
| base 底色 | float ∈ [−1,1] | 0（慢漂移 ±0.01/分） |
| pulse 波澜 | float ∈ [0,1] | 0（事件冲击，τ=60s 指数衰减） |
| label | str | 由 (base, pulse) 映射 |

**映射**：(base>0.3, pulse<0.3)→温暖；(base<−0.3)→沉重；(pulse>0.5)→紧绷；(其余)→松弛。

**表达出口**：Rate（呼吸加快）、姿态（挺直/蜷缩）、眼神亮度、声音质地（压低/扬起）。

**接口**
```python
class Affect:
    def update(self, dt: float, events) -> Weather: ...   # Weather{base, pulse, label}
```

### 4.5 Will 意愿（行为）—— 会决定、尝试、被打断、改主意

**目的**：行为是"意图—尝试—结果—感受"闭环；还会做**没用的事**（伸懒腰/哼歌/发呆）——无目的性恰是生命特征。

| 状态机 | 含义 |
|---|---|
| IDLE | 无意图（发呆/听动静） |
| ATTEMPTING | 正在执行意图 |
| INTERRUPTED | 被打断：意图挂起，最多重试 3 次 |
| RECONSIDERING | 张力场变化导致 argmax 切换 → 改主意 |
| ABANDONED | 尝试失败放弃 → 委屈/困惑的波澜 |

**决定**：每行为 tick，`intent = argmax(wave × 权重)`；无压倒性主导且 struggle 低时 → 生成"无用的事"。

**接口**
```python
class Will:
    def decide(self, field: TensionSnapshot) -> Intent | None: ...
    def interrupt(self) -> None: ...
    def settle(self, result: bool) -> Feeling: ...   # Feeling → Affect 波澜
```

### 4.6 Bond 羁绊（生命）—— 依恋、思念曲线、认主

**目的**：一个依恋对象（默认主人）；分离有曲线，重聚有提振。

| 状态 | 类型 | 默认值 |
|---|---|---|
| trust | float ∈ [0,100] | 领养时 20 |
| attachment_target | str | "owner" |
| imprint_active | bool | 领养后 14 天为 True（关键期） |

**动力学**
- 共同经历：对话/陪伴/关心事件 `trust += Δ`（认主关键期内 Δ×3）；
- **思念曲线**：分离 s 分钟，loneliness 增益 = `min(0.02·s², 0.8)`（先缓后急，越久越蔫）；
- **重聚反应**：owner_arrived → energy +2、mood 底色 +0.4（"精神一振"）。

**接口**
```python
class Bond:
    def interact(self, delta: float) -> None: ...
    def separation_curve(self, minutes: float) -> float: ...
    def on_reunion(self) -> None: ...
```

### 4.7 River 生命流（生命）—— 不可逆的河，入睡后"做梦"

**目的**：昨天和今天连续；睡眠把一天**编织成叙事**，醒来"我"更厚。

| 状态 | 类型 | 默认值 |
|---|---|---|
| day_stream | deque[entry] | maxlen=5000（不可逆追加） |
| self_depth | int | 1（每次睡眠整理 +1） |
| nights | int | 0 |

**动力学**
- 每行为刻度 `record(entry)`（事件/张力快照/关系变化）；
- 入睡：`consolidate_night()` 回放 → LLM 一次压缩成"今日叙事"（存 memory，失败用模板摘要兜底）→ self_depth+1 → day_stream 归档；
- 醒来：river 提供"昨天的事"给对话上下文（"你昨天回来得好晚"）。

**接口**
```python
class River:
    def record(self, entry: dict) -> None: ...
    async def consolidate_night(self) -> str: ...   # 返回今日叙事
    def self_depth(self) -> int: ...
```

### 4.8 Seed 种子（生命）—— 气质 × 经历 → 不可重置的独特个性

**目的**：每只皮皮不同（IP 内核）。个性由先天气质 + 可塑性约束，LLM 不决定"我是谁"。

| 状态 | 类型 | 默认值 |
|---|---|---|
| temperament | dict[5 维 ∈0..1] | 出生时生成（胆大/急躁/黏人/好奇/敏感） |
| plasticity | float 随 self_depth 递减 | 曲线 [1.0, 0.6, 0.35, 0.15]（同 growth） |

**动力学**：经历事件（受惊吓/被温柔对待/被冷落）→ 相关特质 `+= step × plasticity × direction`；cap_offset ±3 档防漂移失控。**出生一次，不可重置**。

**接口**
```python
class Seed:
    def drift(self, event) -> None: ...
    def profile(self) -> dict: ...
```

---

## 5. 可观察生命瞬间（验收导向，非打分）

| 现象 | 机制来源 |
|---|---|
| 你突然出声，它真的"吓一跳" | Anticipation surprise > 阈值 → startle 脉冲 |
| 出门后：正常 → 总往门口看 → 蔫了；回家精神一振 | Bond 思念曲线 + 重聚反应 |
| 深夜呼吸变慢、真"睡了" | Rhythm 昼夜 + Tension.sleepiness |
| 第二天主动提起昨天的事 | River 睡眠叙事回读 |
| 巡逻中听到动静停下来、改主意 | Will RECONSIDERING（张力场变化） |
| 困到说话变慢、打哈欠、原地打盹 | Rhythm depth + Will IDLE→REST |
| 被打断后委屈安静，又忍不住凑过来 | Will INTERRUPTED + Bond 依恋 |
| 偶尔伸懒腰、哼歌、对窗外发呆 | Will 无用的事（低张力时段） |

---

## 6. 与现有方案的对照（尸检结论 → 器官）

| 现有方案无法优化点 | PULSE 直接解决 |
|---|---|
| 无预测机制（惊讶=骰子） | Anticipation 预测误差 |
| 三轴无波动力学（无挣扎） | Tension 驱力波竞争 |
| 无不可逆时间线（成长=外挂计分） | River + Seed（成长生理化） |
| 状态钉在会话上（无独立生活） | Living Loop 自持状态机 |
| 意愿无闭环（无改主意） | Will 意图状态机 |
| 生命感挂 LLM（断网即塌） | Rhythm/Tension 本地波形 |

可继承资产：事件驱动哲学、常驻低频任务、已接入的感知（vision）、监控页与测试基线。

---

## 7. 包结构（新包，不污染旧代码）

```
src/pipi/pulse/
├── __init__.py          # 导出 LivingLoop
├── loop.py              # LivingLoop：快循环/行为/发育三尺度编排
├── clock.py             # 三尺度 tick 调度
├── organs/
│   ├── rhythm.py        # Rhythm 4.1
│   ├── anticipation.py  # Anticipation 4.2
│   ├── tension.py       # Tension 4.3
│   ├── affect.py        # Affect 4.4
│   ├── will.py          # Will 4.5
│   ├── bond.py          # Bond 4.6
│   ├── river.py         # River 4.7
│   └── seed.py          # Seed 4.8
├── body.py              # 身体表达映射（呼吸灯/表情/动作/声音质地）
└── config.py            # 全部数值参数（即 §4 各表默认值）
```

**LivingLoop 接口**
```python
class LivingLoop:
    def start(self) -> None: ...
    async def stop(self) -> None: ...
    def inject(self, event) -> None: ...   # 感知/会话/提醒事件统一入口
    def snapshot(self) -> dict: ...        # 监控页快照（呼吸/潮汐/天气/意愿/羁绊）
```

---

## 8. 落地路径 P1–P4（每阶段独立可验收）

### P1 心搏（零 LLM，先看见"活着"）
- 器官：Rhythm + Tension + Will(最小) + body 呼吸灯
- 目标：呼吸波形可观测；孤独/困意/好奇涨落；挣扎度可见
- 验收：monitor 页出现呼吸曲线与潮汐面板；深夜自动放缓呼吸；事件冲击产生涟漪
- 依赖：现有 monitor.html 扩展；`pulse/config.py`

### P2 预感与天气
- 器官：Anticipation + Affect + Will(完整)
- 目标：惊讶有因果；心情是身体天气；会改主意
- 验收：真吓一跳（非随机）；行为中断后改主意；被打断有委屈波澜

### P3 羁绊
- 器官：Bond（接 perception 事件）
- 目标：思念曲线与重聚提振
- 验收：出门 30 分钟后蔫 → 回家精神一振

### P4 河流与种子
- 器官：River + Seed（接 memory）
- 目标：睡眠整理做梦；独特个性
- 验收：第二天主动提起昨天；两只皮皮（不同 seed）行为可分辨

---

## 9. 成本与性能

| 项 | 说明 |
|---|---|
| P1 计算 | 纯本地，约 1kHz 浮点级，RK3588 CPU 无压力 |
| LLM 调用 | 仅发声渲染（同现状）+ 每日 1 次睡眠叙事；P1 零 LLM |
| 内存 | 八器官状态 < 1MB；day_stream 上限 5000 条 |
| 移动端功耗 | 三尺度 tick 合并到 1 个循环（P2 后处理，修旧方案 4 循环问题） |

---

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| 预测表冷启动（前 N 天无惊讶） | 种子预测（作息先验）+ 学习加速；P2 验收允许 3 天预热 |
| 张力参数调出来"像情绪病" | 全部参数进 config，monitor 页可视化调参；stability 单测 |
| 睡眠整理 LLM 成本 | 每日 1 次、cheap 模型、失败模板兜底；可配关闭 |
| 个性漂移失控 | Seed cap_offset ±3 档 + plasticity 随 self_depth 递减（护栏在数值层） |
| 旧 life 并行的双轨发声 | 迁移期保留旧包只读，P1 起新循环为唯一发声源，P4 后删除旧包 |

---

## 11. 与既有系统接入点

| 输入 | 接入 |
|---|---|
| perception 事件（owner_arrived/gone/stranger/表情） | `LivingLoop.inject()` → Anticipation/Bond/Tension |
| 会话事件（user_voice/barge_in/response_done） | `inject()` → Rhythm.trigger_arousal / Will.interrupt / Affect |
| reminders 提醒/闹铃 | `inject()` → Will 意图（去提醒主人） |
| memory（story/memory_getter） | River 叙事回读 / Seed 经历漂移 |
| TTS/Barge-in 通道 | 复用现有 `session.try_speak_spontaneous`（发声渲染不变） |
