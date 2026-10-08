# PULSE · 心搏 —— 真实系统接入说明

> 状态：✅ 已接入（`pulse.backend: "pulse"` 默认启用；`"legacy"` 可一键回退旧 LifeEngine）
> 说明：LivingLoop 通过 `PulseRuntime`（`src/pipi/pulse/runtime.py`）适配现有系统，
> 对外接口与旧 LifeEngine 完全兼容，`session/cli` 无需改动即可切换。

## 1. 接入架构

```
┌────────────────────────── 现有系统 ──────────────────────────┐
│  InteractionSession        VoiceCLI        PerceptionLoop    │
│  (observe_user_voice…)     (cli.py)        (视觉事件)         │
│        │ 同接口                │ bind/sink        │          │
│        ▼                      ▼                  ▼          │
│  ┌────────────────── PulseRuntime（asyncio）───────────────┐ │
│  │  · 1Hz tick → LivingLoop.tick(1.0, hour)  1秒=1仿真秒    │ │
│  │  · 事件注入：会话/视觉/外部 → loop.inject(kind, minute)   │ │
│  │  · 自发发声：主导意图 × 冷却 × 概率 → try_speak_spontaneous│ │
│  │  · 跨天整理：consolidate_night() → 日记+种子漂移+认主     │ │
│  │  · 监控上报：snapshot/事件/聊天 → sink → WSHub → monitor  │ │
│  └───────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

- **时间 1:1**：1 仿真秒 = 1 真实秒 → 思念/认主/成长按真实天数演化（1 天 = 1440 拍）；
- **零 LLM**：呼吸/潮汐/预感/羁绊/日记全部本地确定性动力学，自发话语为固定句库兜底
  （生产可换 LLM 渲染，接口不变）。

## 2. 事件映射

| 来源 | 外部事件 | PULSE 事件（inject kind） | 效果 |
|---|---|---|---|
| 会话 | `observe_user_voice()` | `user_voice` | 想陪↓、心境回暖、互动+1 |
| 会话 | `on_barge_in()` | 打断 → `will.interrupt()` | 意图挂起重试，委屈波澜 |
| 会话 | `on_response_done()` | 小回暖 + `bond.interact()` | 互动计数 |
| 会话 | `observe_dialogue_mood(mood)` | `affect.push_base(±)` | 情绪回灌心境 |
| 视觉 | `owner_gone` | `owner_depart` | 出门→思念曲线启动 |
| 视觉 | `owner_arrived` | `owner_arrive` | 预期比对→惊喜/重聚分级 |
| 视觉 | `stranger` | `stranger` | 预测偏差→惊吓/好奇/警觉 |
| 视觉 | `owner_expression` | `owner_sad` | 共情→陪伴、依恋↑ |
| 外部 | 提醒触发 `note_event("reminder")` | 记入当天日记 | 生命流留痕 |

## 3. 配置（configs/default.yaml）

```yaml
pulse:
  backend: "pulse"          # pulse（LivingLoop）| legacy（旧引擎回退）
  tick_s: 1.0               # 心搏 1Hz
  vocal_cooldown_s: 90.0    # 自发开口最小间隔
  vocal_prob: 0.02          # 5s 决策的基础开口概率（克制）
  diary_memory: false       # 日记是否写入记忆库（默认仅推送监控/日志）
```

## 4. 监控（web/monitor.html）

快照兼容旧结构（state/attention/physio/activity/queue/backend）+ PULSE 专属字段：

```json
{
  "state": "idle|rest",
  "physio": {"mood":…, "energy":…, "social":…, "mood_value":"平静", "summary":…},
  "activity": {"now":…, "intent":"想陪你（attachment）"},
  "pulse": {
    "breath": {"rate":…, "depth":…}, "struggle":…,
    "weather": {"label":"多云", …}, "trust":…, "self_depth":…,
    "owner_present":…, "sleeping":…, "seed": {"boldness":…, …5 轴}
  }
}
```

页面新增「🫀 心搏（PULSE）」面板：呼吸 bpm/深度、天气、信任、自我深度、
挣扎、种子五轴、主人在/不在；「心理与行为记录」实时滚动事件（含每日日记）。

## 5. 验证

- `python scripts/pulse_runtime_smoke.py` —— 集成 smoke（8/8）：假时钟快进 2 天，
  会话/视觉/外部事件全走一遍；tick 运转、事件映射、跨天整理（self_depth=3 +
  日记成文）、快照结构、兼容接口全部通过；
- P0–P4 回归全绿：`pulse_p0_validation.py`（trust 32.6 / startle 2）、
  `pulse_p4_demo.py`（6/6）——接入层不改心脏，仅适配。
- 真实语音跑通：`python run.py`（需麦克风/声卡；`pulse.backend=legacy` 可回退）。

## 6. 已知边界

| 项 | 现状 | 后续 |
|---|---|---|
| 自发发声 | 固定句库（冷却+概率） | 可换 LLM 渲染（接口已留） |
| 身体动作 | 意图/动作文本上报监控 | 接 Body 灯/眼/姿态 + 底盘动作 |
| 日记落库 | 推送监控/日志，不入记忆库 | `diary_memory: true` 时写记忆（identity） |
| 认主关键期 | 领养日起 14 天（真实日历） | 与「成长引擎 S 阶段」合并对齐 |
