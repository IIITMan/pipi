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
    "owner_present":…, "sleeping":…, "sess_phase":"",
    "body": {"lamp":…, "eye":"正常", "pose":"松弛", "act":…},
    "behavior": {"motion":"idle", "target":"none", "zh":…},
    "last_chat_mood":"", "seed": {"boldness":…, …5 轴}
  }
}
```

页面「🫀 心搏（PULSE）」面板：呼吸 bpm/深度、天气、对话心情与正在听/说、
**身体（灯亮度/眼/姿态/动作）与行为（意图→底盘原语）**、信任、自我深度、
挣扎、种子五轴、主人在/不在；事件流实时滚动（含每日日记）。

## 5. 验证

- `pulse_runtime_smoke.py` —— 集成 smoke（8/8）；
- `pulse_body_behavior_validation.py` —— 身体/行为/统一说话（**12/12**）：
  body/behavior 进快照、action_sink 被驱动、主动发声优先 LLM、失败句库兜底；
- `pulse_runtime_validation.py` —— 接入层 14 天三组（**34/34**）；
- `weather_life_sync_validation.py` —— 真实天气 → 生命模块同步（**29/29**）；
- P0–P4 回归全绿：P0（trust 32.6 / startle 2）、P123（15/15×3）、P4（6/6）。
- 真实语音跑通：`python run.py`（`pulse.backend=legacy` 可回退）。

## 6. 真实天气 → 生命模块（weather.life_sync）

对话天气查询（WeatherService/Open-Meteo）不再只是"问一句答一句"，
外部天气类别会**同步进心搏**，让皮皮"体感"到外面：

| 通道 | 实现 | 效果 |
|---|---|---|
| 类别化 | `WeatherService.kind()`：WMO 代码 → sunny/cloudy/overcast/fog/rain/snow/thunder | 结构化喂给生命，而非一段文本 |
| 调制 | `LivingLoop.observe_weather(kind)` | 晴 +0.02；阴 -0.02；雾 -0.03；雨 -0.04 + 思念 +0.06；雪 +0.03；雷 -0.05 + 波澜 0.12 + 安全感 -0.15（幅度克制 ≤0.05，不钉死心情） |
| 周期同步 | `PulseRuntime._weather_sync_loop`（默认 30min，`weather.sync_interval_s`） | 类别变化才注入，启动即同步一次 |
| 对话触发 | `WeatherService.subscribe()` → runtime 订阅 | 用户问天气后立刻同步（类别变化才生效） |
| 可见面 | 快照 `pulse.weather_ext`（监控"外面：雨"）+ `inner_brief` 注入对话（"外面在下雨，这影响着你的心情"） | 说话/监控与生命一致，不再"两张皮" |
| 日记 | `loop.observe_weather` 记 `weather` 刻度 | 入睡成文留痕（"外面在下雨"） |

验证：`scripts/weather_life_sync_validation.py` **29/29**（映射/广播/克制幅度/变化去重/快照/内心注入/订阅触发）。

## 8. 贾维斯化 ①②（能力层，生命内核不动）

在 PULSE 之上叠加"随叫随到的应答 + 主动预警"，两者都走既有通道、失败静默降级：

**① 通用联网搜索 —— `capabilities/search.py`（SearchService）**
- 触发：`detect(text)` 事实疑问词（什么/为什么/怎么/哪里…30 个），排除天气/提醒意图与"你/我开头"短情感问；长度<5 不搜。
- 多源链 `provider: auto`：**Bing 网页搜索（cn.bing.com，零 key、国内可达）→ Wikipedia API** 依次尝试，哪个可用用哪个；缓存 1800s；失败返回 None 对话照常回答。
- 接线：orchestrator 天气优先 → `elif search.detect` 并行查，结果并入 `ctx.tool_brief`（前缀"联网查到的资料(可用可不用):"），LLM 用人格口吻回答"我哪知道" → "我查到了"。
- 实测：`黑洞是什么` → Bing 366ms 返回维基/知乎摘要。

**② 条件预警 —— `pulse/alerts.py`（AlertEngine）**
- 回家汇报：主人回来时若"今日有料"（定过提醒 / 陌生人≥stranger_min）主动开口；**汇报后消费料**（同一件事不重复打扰）；冷却 30min；无料只默默高兴（生命感>服务感）。
- 陌生人久留：当日累计 ≥2 次 → 提醒主人留意（冷却 1h）。
- 提醒触发：ReminderService 自带发声，引擎只留痕供回家汇报汇总。
- 接线：`observe_vision`(owner_arrived/stranger) + `note_event("reminder")` → `_speak_alert`；抑制规则：会话忙/睡眠中只留痕不开口；开口走统一说话（LLM 按内心渲染 + 句库兜底）。
- 配置：`configs/default.yaml` → `alerts:` 节。

验证：`scripts/jarvis_search_alert_validation.py` **19/19**（触发/排除/mock/缓存/有料才说/消费/冷却/阈值/跨天/抑制/统一说话）。

## 9. 贾维斯化 ③④（环境档案 + HUD）

**③ 环境档案 —— `capabilities/environment.py`（EnvironmentMemory）**
- 一本越养越厚的"小本本"（`data/environment.json` 落盘，零依赖），记录：
  - 人物：主人作息（多次回家钟点 → 学会"主人一般 X 点回来"）、家人/熟人称呼
  - 物品 / 地点：对话里反复提到的（次数 + 最近时间，词表克制）
  - 现场：主人在不在、今日陌生人次数
  - 世界：外面天气、今日提醒摘要
- 来源全是顺带记录：对话（orchestrator 每轮 `observe_chat`）、视觉（runtime `observe_vision`）、天气同步、提醒触发 —— 永不阻塞主流程。
- 输出：`snapshot()`（HUD 卡片）+ `home_hour_expected()`/`facts_brief()`（预警上下文与主动行为预留）。
- 配置：`configs/default.yaml` → `env:` 节（enabled / max_arrivals）。

**④ HUD —— `web/monitor.html`**
- 右列新增「🏠 环境档案」卡片：主人在不在/一般几点回来/今日陌生人次数、家/东西/家人（最近提到）、外面天气、能力层状态灯（搜索 ●/预警 ●/天气 ○）。
- 日志事件标记：回家汇报 🔔、陌生人 ⚠️、天气 🌦️、提醒 📌。
- 原三栏布局（摄像头 + 眼睛 / 聊天 + 记录 / 状态 + 心搏 + 成长 + 提醒）与 1920×1080 / 1366×768 自适应保持不变。

验证：`scripts/jarvis_env_hud_validation.py` **12/12**（词表记账/去重/作息学习/落盘重载/天气提醒入档/能力状态/snapshot 契约/对话顺带记账）。

## 7. 已知边界

| 项 | 现状 | 后续 |
|---|---|---|
| 主动说话 | 意图触发，**统一走 LLM**（带内心状态），句库兜底 | 按实测调 `vocal_prob`/冷却 |
| 身体动作 | 每帧 body/behavior 上监控，**硬件端口已预留** | 实现 ActionSink 接灯/屏/姿态/底盘 |
| 日记落库 | 推送监控/日志，不入记忆库 | `diary_memory: true` 写 identity |
| 认主关键期 | 领养日起 14 天（真实日历） | 与「成长引擎 S 阶段」合并对齐 |

## 7. 硬件接入点（后期开发，无需改生命逻辑）

心脏只负责"算出状态"，所有物理表达都经两个可注入端口：

**① 身体/底盘 —— `runtime.action_sink`**（协议见 `pulse/expression.py`）：
```python
class MyRobotSink:
    def apply(self, body, behavior):
        # body: lamp(灯亮度) / eye(眼:睁大/半闭/正常) / pose(姿态) / act(动作)
        # behavior: motion(approach/retreat/look/explore/idle) / target / zh
        ...
runtime.action_sink = MyRobotSink()
```

**② 主动说话 LLM —— `runtime.vocal_renderer`**：传入内心事件、返回一句台词；
cli 已默认注册为 `orch.handle_proactive`（云端 LLM，带 inner_brief）。
置 None 则退回固定句库（离线/省成本模式）。

**③ 对话内心 —— `orch.inner_state_provider`**：cli 已注册为 `rt.inner_brief`，
每轮主对话/主动说话都读此刻真实身心。
