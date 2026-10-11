# 皮皮 · 情感陪伴机器人 — 架构文档

> 一个可以部署到实体机器人的情感对话系统。核心不是"更聪明的问答"，
> 而是**有人格、会遗忘、会主动、能看见你**。
>
> 参考了 Giftia（稀疏记忆 / 多 Agent 分工）+ SenseVoice（语音情感理解）
> + Qwen（对话大脑）的思路，但所有外部依赖都是**可插拔**的：
> 任何一个缺失都能自动降级，`--sim` 模式下零模型、零硬件、零网络也能完整跑通。

---

## 一、设计哲学

皮皮和普通 Chatbot 的区别，不在模型能力，在**四件事**：

| | 普通 Chatbot | 皮皮 |
|---|---|---|
| **记忆** | 全量上下文，或全忘 | 稀疏记忆：会衰减、会强化、有身份锚点 |
| **情绪** | 每次现算，无状态 | 有状态的情绪机：会累积、会回落、会吃醋 |
| **主动性** | 只能被动应答 | 视觉触发 + 时间触发，会主动开口 |
| **躯体** | 纯文本 | 有"看到你"的输入，有"肢体动作"的输出 |

而**避免 AI 味**是所有这些的地基。方法论见附录 A。

---

## 二、分层架构

```
┌───────────────────────────────────────────────────────────────────┐
│ L5  交互层                                                         │
│   CLI(窗口端,内嵌WS推送) · 静态监控页 · 语音循环 · 主动触发器          │
├───────────────────────────────────────────────────────────────────┤
│ L4  编排层                                                         │
│   ConversationOrchestrator                                        │
│     ├─[并行] EmotionAnalyst    情感分析                            │
│     ├─[并行] MemoryExpert      记忆检索 + 强化                      │
│     ├─ DialogueExpert          组装 Prompt → LLM → 原始回复         │
│     ├─ ExpressionService       去 Markdown / 拆气泡 / 长度熔断      │
│     └─[异步] 记忆写入 + 情绪落库 + 对话持久化                        │
│   ProactiveEngine（何时该主动开口）· GrowthEngine（关系成长 S1-S4）
│   CapabilityRouter（降级决策）· DockController（自动回充）
├───────────────────────────────────────────────────────────────────┤
│ L3  能力层（"接口 + 多实现"，全部可 mock 可替换）                     │
│   感知  PerceptionService · ASRProvider · VisionProvider            │
│   认知  LLMProvider · MemoryStore · PersonaSpec · EmotionEngine      │
│   表达  TTSProvider · ExpressionService                             │
│   执行  MotionProvider                                              │
├───────────────────────────────────────────────────────────────────┤
│ L2  领域层（纯数据 + 纯算法，无 IO 无框架）                           │
│   UserProfile · MemoryItem · EmotionState/Mood · PersonaSpec/Traits  │
│   VisionObservation · Transcript · MotionCommand · DialogueTurn      │
├───────────────────────────────────────────────────────────────────┤
│ L1  基础层                                                         │
│   Config(热重载) · SQLite + Repositories · EventBus · Clock(可注入) │
│   Logging · Metrics · trace_id                                      │
├───────────────────────────────────────────────────────────────────┤
│ L0  硬件层                                                         │
│   Camera / Mic / Speaker / MCU-Bridge                              │
│   hardware/sim/（当前） hardware/rockchip/（预留）                        │
└───────────────────────────────────────────────────────────────────┘
```

### 核心原则

> **感知是持续的背景流，对话是事件驱动流。**

摄像头每秒采 1.5 帧，不断产生 `VisionObservation`；但这**不等于**要每帧说话。
感知结果只喂给两处：① 当前对话的 system message 上下文；② 主动引擎的触发判断。
说话永远是事件驱动的 —— 要么用户开口，要么主动引擎判定"该开口了"。

### 依赖方向

严格单向：`L5 → L4 → L3 → L2 → L1`，L0 只被 L3 通过 Protocol 引用。
**L2 领域层不 import 任何 L3/L4/L5 的东西**，因此所有核心算法
（衰减、检索排序、情绪转移）都能在纯内存里单测，不需要数据库、不需要模型。

---

## 三、目录结构

```
/workspace/
├── README.md                     # 快速开始
├── docs/                         # 全部设计文档（含本文档）
│   ├── ARCHITECTURE.md           # 软件架构
│   ├── HARDWARE.md / CHASSIS.md  # 硬件选型 / 底盘机械设计
│   ├── LLM_SELECTION.md          # 模型选型
│   ├── GROWTH_DESIGN.md          # 成长系统设计（含验收标准）
│   ├── PULSE_DESIGN.md          # PULSE 心搏设计（8→4 精简后现行状态）
│   ├── PULSE_RUNTIME_INTEGRATION.md # PULSE 接入说明（配置/事件映射/监控）
│   ├── PULSE_VALIDATION_REPORT.md   # PULSE P0–P4 验证总报告
│   ├── CHASSIS_REPORT.html       # 底盘可视化报告
│   └── 皮皮人格Prompt方案.md      # 人格 Prompt 设计
├── pyproject.toml                # 依赖分组（核心依赖为空 —— 不装也能跑）
├── web/monitor.html              # 纯静态监控页（WS 订阅，双击即开）
├── run.py                        # 统一入口（--sim / --mic / --voice；内嵌 WS 推送）
├── configs/
│   ├── default.yaml              # 主配置（含主动搭话/隐私开关）
│   ├── degradation.yaml          # 降级链定义
│   └── personas/                 # 傲娇/话痨/粘人/毒舌 四套人格
├── scripts/
│   ├── download_models.py        # 可选模型下载（失败自动降级）
│   ├── seed_demo_data.py         # 一键造出"相处 60 天"的记忆现场
│   ├── mic_test.py               # 麦克风 → SenseVoice 单链路测试
│   └── replay_session.py         # 会话回放（回归验证 Prompt 改动）
├── tests/
│   └── integration/
│       └── test_proactive_vision.py   # 阶段 5+6 验收（36 项断言）
├── src/pipi/
│   ├── app.py                    # 装配工厂 + 健康矩阵
│   ├── domain/                   # L2 领域模型（零依赖）
│   │   ├── memory.py             #   MemoryItem / 衰减算法 / 检索权重
│   │   ├── emotion.py            #   EmotionState / Mood / 转移表
│   │   ├── persona.py            #   PersonaSpec / Traits（人格→行为参数）
│   │   ├── profile.py            #   UserProfile（档案→自然语言记忆块）
│   │   ├── vision.py             #   VisionObservation（→ 自然语言 hint）
│   │   ├── audio.py              #   Transcript（ASR 一次拿全文本+情感+事件）
│   │   ├── motion.py             #   Gesture / GESTURE_SEQUENCES
│   │   └── dialogue.py           #   Session / DialogueTurn / Bubble
│   ├── infra/                    # L1 基础设施
│   │   ├── config.py             #   点号取值 + 环境变量覆盖 + 热重载
│   │   ├── clock.py              #   SystemClock / FakeClock（时间旅行）
│   │   ├── event_bus.py          #   asyncio pub/sub
│   │   └── db/
│   │       ├── schema.sql        #   完整建表（含 emotion_history/decay_reports）
│   │       └── repositories.py   #   9 个 Repo，embedding 用 struct 打包
│   ├── providers/                # L3 各能力的多实现
│   │   ├── asr/                  #   sensevoice | cloud_dashscope | mock
│   │   ├── tts/                  #   ark_tts | edge_tts | mock
│   │   ├── vision/               #   pipeline + detector/expression/clothing + mock
│   │   ├── llm/                  #   openai_compat(Qwen/DeepSeek/...) | mock
│   │   ├── motion/               #   mock | wheeltec(C30D 单板·ROS2 话题)
│   │   └── memory/               #   sqlite_store · embedder(bge|tfidf|hash)
│   ├── capabilities/             # L3 跨 Provider 的业务能力
│   │   ├── memory_manager.py     #   抽取(双路) / 去重 / 检索 / 衰减 / 强化
│   │   ├── emotion_engine.py     #   信号→转移→状态
│   │   ├── expression.py         #   去 Markdown / 去 AI 腔 / 拆气泡
│   │   └── perception.py         #   多源感知融合
│   ├── agents/                   # L4 三 Agent 分工
│   │   ├── emotion_analyst.py    #   情感分析（并行）
│   │   ├── memory_expert.py      #   记忆检索（并行）
│   │   ├── dialogue_expert.py    #   对话生成
│   │   └── router.py             #   CapabilityRouter + CircuitBreaker
│   ├── orchestration/            # L4 主编排
│   │   ├── orchestrator.py       #   handle_turn / handle_proactive
│   │   ├── proactive.py          #   ProactiveEngine（频率/冷却/静默）
│   │   ├── growth.py             #   GrowthEngine（关系成长 S1-S4/里程碑/偏好）
│   │   └── dock.py               #   DockController（自动回充三段对齐）
│   ├── hardware/                 # L0
│   │   ├── sim/                  #   模拟摄像头
│   │   └── rockchip/             #   RK3588 实现（预留）
│   ├── pulse/                   # PULSE 心搏（生命模块，8→4 精简后唯一引擎）
│   │   ├── loop.py              #   LivingLoop（驱力/意愿/羁绊/气质/习惯化）
│   │   ├── runtime.py           #   PulseRuntime（asyncio 适配：tick/事件/发声/监控）
│   │   ├── derive.py            #   派生展示层（天气/呼吸，Affect/Rhythm 精简去向）
│   │   ├── perception.py        #   感知循环（摄像头→离散事件，自旧 life 迁入）
│   │   └── organs/              #   tension · will · bond · river
│   ├── interfaces/               # L5
│   │   ├── cli.py                #   命令行
│   │   └── web.py                #   FastAPI + SSE（遗留模式，已移除）
│   │   └── ws_server.py          #   窗口端 WebSocket 推送中枢
│   └── web/                       #   静态页面（双击即开，订阅 WS）
│   └── prompts/                  # *.jinja 模板
└── data/                         # 运行时数据（DB / 模型 / 人脸）
```

---

## 四、核心算法

### 4.1 稀疏记忆（艾宾浩斯遗忘曲线）

这是整个系统最核心的机制。**不是所有记忆都该被平等对待。**

```
显著性 salience = importance × exp(−λ · Δt_hours)

衰减率 λ = λ_base(type)
           × (1 − 0.8·importance)        # 越重要，掉得越慢
           × 1/(1 + 0.3·log(1+access_count))  # 被想起越多，掉得越慢
```

各类型的 `λ_base` 设计：

| 类型 | 半衰期 | 设计意图 |
|---|---|---|
| `IDENTITY`（身份） | **永不** | 认主不可丢 —— 名字、住哪、做什么 |
| `COMMITMENT`（约定） | **永不** | "我答应你早点睡"这种必须记住 |
| `PREFERENCE`（喜好） | ≈ 40 天 | 喜欢什么/讨厌什么，长期有效 |
| `EMOTION`（情感印记） | ≈ 3 天 | 那天他很难过，值得记一阵 |
| `EVENT`（事件） | ≈ 9 天 | 下周三出差 —— 一周内有用 |
| `RELATION`（关系） | ≈ 14 天 | 他养了只猫 |
| `CHITCHAT`（闲聊） | ≈ 18 小时 | **隔天就该淡忘** —— 这是关键 |

**为什么闲聊要快速遗忘？**
因为"记得每句闲聊"恰恰是最像机器人的行为。人会自然遗忘寒暄，
只在有意义的时刻才调取记忆。皮皮在 30 天后忘记"今天天气不错"，
但在 40 天后依然记得"他喜欢美式"—— 这个不对称
就是"像人"的来源。

**回忆强化**（每次被检索命中）：

```
access_count += 1
stability   ×= 1.4          # 稳定性提升
λ           随 access_count 下降（见公式）
salience    回升
```

**淘汰**：`salience < 0.05` 且 `access_count < 2` 且非 pinned → 软删除
（`deleted_at` 打标记，可恢复，避免误删）。

**pinned 机制**：`IDENTITY` / `COMMITMENT` 天然 pinned，永不衰减。
用户也可以手动 `/pin` 任何一条记忆。

#### 检索排序（五因子加权）

```
score = 0.45 · 语义相关度      # 向量余弦；无向量时降级 TF-IDF
      + 0.25 · 显著性           # 当前 salience
      + 0.10 · 新近度           # recency_score，指数衰减
      + 0.12 · 重要性           # importance
      + 0.08 · pinned 加成      # 身份/约定优先
```

同类记忆最多召回 **2 条**（`max_per_type`）—— 防止"他喜欢A、他喜欢B、
他喜欢C..."刷屏，也防止同一主题挤占上下文。

#### 记忆抽取（双路）

| 路径 | 触发条件 | 特点 |
|---|---|---|
| **LLM 抽取** | 配置了 api_key | 准，能理解"我最近在琢磨怎么把咖啡做得更好喝" |
| **规则抽取** | 无 key / sim 模式 / LLM 失败 | 快、免费、零依赖，靠正则+词表 |

规则抽取的两个关键细节（都是踩过坑才加的）：

1. **否定式优先抹除**：先匹配 `不喜欢X`，再从原文抹去，避免
   "我不喜欢开会"被抽成"他喜欢开会"。
2. **形容词陷阱**：`爱` 作为单字动词要加否定断言 `(?<![可疼怜宠])`，
   否则"你比我家布丁**可爱**多了"会被抽成"他喜欢多了"。

### 4.2 情感状态机

情绪不是每轮现算的，是**有记忆的状态**：

```python
EmotionState:
    mood: Mood             # 平静/开心/关心/吃醋/委屈/得意/傲娇/生气/困倦/调皮
    intensity: float       # 0~1，每轮 ×0.85 自然回落
    affection: float       # 0~1000，长期累积
    companion_need: float  # 0~1，累积后推动主动搭话
    is_attachment: bool    # 是否认主（决定好感度底价）
```

**转移表**（`MOOD_TRANSITIONS`）把"什么情况下会怎么想"集中定义，
不散落在业务代码里：

| 触发 | 目标情绪 | 好感度 Δ | 陪伴需求 Δ |
|---|---|---|---|
| `mention_rival` 提到猫/狗/别的机器人 | 吃醋 | −5 | +0.10 |
| `praise` 夸奖/致谢 | 得意 | +5 | −0.10 |
| `negative_emotion` 吐露负面情绪 | 关心 | +1 | +0.20 |
| `idle` 长时间没交互 | 委屈 | −3/时 | +0.15/时 |
| `owner_arrived` 主人回家（视觉） | 开心 | +3 | −0.30 |
| `warm_return` 久别后重新联系 | 开心 | **+12** | −0.40 |
| `casual_warmth` 日常闲聊的陪伴感 | 平静 | +0.6 | −0.05 |
| `ignored` 主动搭话被忽视 | 委屈 | −2 | +0.10 |

#### 好感度衰减的两个反直觉修正

初版实现里，60 天不联系会让好感度从 500 掉到 **0（陌生）**—— 这不对。

```
# 错误写法：线性叠加，无下界
affection -= 3.0 * hours

# 正确写法：sqrt 压缩 + 关系底价
affection -= 3.0 * sqrt(hours)     # 第一天掉得最狠，之后钝化
affection = max(affection_floor(), affection)   # 认主 → 底价 300
```

两个理由：
1. **sqrt 压缩**符合真实心理 —— "你怎么不理我"的委屈在第一天最强，
   之后逐渐钝化，而不是每天都同等疼痛。
2. **关系底价**—— 认过主的人不该因为出差一个月就变成陌生人。
   陌生人（`is_attachment=False`）不设底价，可以彻底遗忘。

### 4.3 视觉 → 语言翻译

**绝对不要把原始 JSON 塞给 LLM**，否则模型会用"字段感"的语气回复，
AI 味立刻暴露。

```python
# × 错误
system += '{"owner_present":true,"owner_newly_arrived":true,"clothing":{"owner":{"color_names":["灰色"]}}}'

# √ 正确（VisionObservation.to_hint）
system += "[你看到的] 主人刚回来了，他现在穿着灰色的衣服，看起来挺高兴"
```

翻译规则：
- `owner_newly_arrived=True` → "主人刚回来了"
- `clothing.primary_color()` → "他现在穿着灰色的衣服"
- `expression.label != "neutral"` → "看起来挺高兴"（查中文映射表）
- `extra_person_count > 0` → "屋里还有 N 个别人"

**衣着夸赞**是"像人"的重要细节 —— 人能注意到你今天换了件衣服，
机器人只说"你好"就显得空洞。

### 4.4 主动引擎

模型不会自己说话，必须由程序"戳"它。主动引擎就是那个戳的人。

**关键约束（否则陪伴变骚扰）**：

| 约束 | 实现 |
|---|---|
| 每日上限 | `persona.proactive_daily_limit`（撕葱人格 5 次，高冷人格 2 次） |
| 同类冷却 | 回家 30min / 关心 60min / 重要日子 1440min |
| 夜间静默 | 23:00–07:00 一律不开口 |
| 被忽视降频 | 连续被忽视 2 次 → 上限减半；3 次 → 完全静默 |

**触发规则与优先级**：

| 触发器 | 优先级 | 条件 | 冷却 |
|---|---|---|---|
| `return_home` | 90 | 视觉检测到主人刚回家 | 30 min |
| `concern_followup` | 85 | `companion_need ≥ 0.5` | 60 min |
| `important_date` | 80 | 明天是重要日子 | 1440 min |
| `idle` | 50 | 超过 `idle_hours` 没理（粘人的人格阈值自动降低） | 180 min |
| `morning` | 40 | 07:00–09:00 | 720 min |
| `night_sleep` | 40 | 22:00–23:00 | 720 min |
| `random` | 10 | 随机撒娇 | 240 min |

评估流程：`夜间静默 → 每日上限 → 收集候选 → 按优先级排序 → 逐个检查冷却`。
高优先级压过低优先级（同时"回家"和"闲置"满足时，回家赢）。

### 4.5 人格数值化

人格不是一段文字，是**驱动行为的参数**。`Traits` 里的每个数值都有
对应的行为后果：

```yaml
# configs/personas/tsundere.yaml
traits:
  clinginess: 0.55       # 粘人度 → 主动上限 = round(3 + 4×0.55) = 5 次/天
                         #          → 闲置阈值 = 2h × (1.3 − 0.6×0.55) = 1.94h
  verbosity: 0.6         # 话多度 → max_tokens = 200 + 300×0.6 = 380
  tsundere_degree: 0.8   # 傲娇度 → 话术模板选型
  sharpness: 0.5         # 毒舌度
```

`PersonaSpec` 的派生属性把 traits 翻译成运行时参数：
- `proactive_daily_limit` ← `clinginess`
- `max_tokens` / `temperature` ← `verbosity`
- `jealousy_affection_delta` / `praise_affection_delta` ← `tsundere_degree`

人格文件支持**热重载**：改完 YAML 在 CLI 里 `/reload` 立即生效，无需重启。

### 4.6 可注入时钟

记忆衰减、主动搭话的闲置判定都依赖时间。直接调 `time.time()`
就没法测试"过了一天会发生什么"。

```python
FakeClock(start).advance(hours=5)   # 快进 5 小时
FakeClock(start).jump_to_hour(2)    # 跳到凌晨 2 点（测夜间静默）
```

`FakeClock` 让所有时间相关逻辑都可测 —— 验收脚本里
"闲置 5 小时→想他"、"明早 8 点→早间问候"、"明天生日→提前提醒"
全都是同一个 `settrace` 快进出来的。

---

## 五、三 Agent 分工（参考 Giftia / Go+Eino）

```
用户输入
   │
   ├──[并行] EmotionAnalyst ──┐
   │        （情感分析）       │  asyncio.gather
   ├──[并行] MemoryExpert  ───┤
   │        （记忆检索+强化）  │
   │                          ▼
   └──► DialogueExpert ── 组装 Prompt → LLM → 原始回复
              │
              ▼
        ExpressionService ── 去 Markdown → 去 AI 腔 → 拆气泡 → 长度熔断
              │
              ├──► TTS（可选）
              ├──► Motion（可选，由情绪映射到动作）
              │
              └──[异步] 记忆抽取+写入 / 情绪落库 / 对话持久化
```

**为什么情感和记忆要并行？** 它们互不依赖，串行会让首字延迟翻倍。
`asyncio.gather(return_exceptions=True)` 保证任一路失败不影响另一路。

**为什么每轮重注入完整 System Prompt？**
长对话里模型会"人格漂移"——聊到第 20 轮就变得客气礼貌。
每轮把完整人设 + 当前情绪 + 记忆 + 视觉上下文重新注入，
是防漂移最有效的手段（代价是 token 多一些，但值得）。

---

## 六、降级设计（"任何环境都能跑通"）

这是整个工程最重要的可靠性保证。**每一条能力都有降级链**：

| 缺失 | 降级路径 | 后果 |
|---|---|---|
| 无 GPU | ONNX CPU + int8 / edge-tts / 云 API | 略慢 |
| 无摄像头 | MockVision 读本地图片 + 脚本触发 OwnerArrived | 主动搭话仍可演示 |
| sherpa 模型缺失 | KWS/VAD/ASR **无降级，启动报错**（2026-10 起语音链收敛为 sherpa 唯一方案） | 语音模式不可用，`--sim`/文本不受影响 |
| TTS 装不上 | ark_tts → edge-tts → MockTTS(打印) | 语音变文字 |
| InsightFace 装不上 | 保留人脸检测，"主人/陌生人"二分 | 认主精度降 |
| 表情模型装不上 | 用 ASR 的情感标签替代视觉表情 | 略钝 |
| 无 embedding 模型 | bge-small → **TF-IDF** → Hash | 语义检索变关键词 |
| LLM 断网 | 重试 → MockLLM 规则回复 + 安抚话术 | 皮皮仍不沉默 |
| **全部缺失** | `--sim`：全 mock + 脚本化用户行为序列 | **零依赖端到端演示** |

启动时的健康矩阵就是这些链的实时状态：

```
能力健康矩阵：
  asr          sensevoice×
  tts          ark_tts× → edge_tts× → mock√
  llm          doubao× → deepseek× → mock√
  vision       full√ → detect_only√ → mock√
  embedding    bge_small√ → tfidf√
  voiceprint   sherpa×
  motion       wheeltec× → mock√
```

**熔断器**：某个 Provider 连续失败 3 次后熔断 60 秒（半开重试），
避免在坏掉的路径上反复超时。

**降级不等于静默失败**：`TurnResult.degraded=True` 会一路传到前端，
CLI 显示"降级"标记，Web 显示提示。用户永远知道系统在什么状态下。

---

## 七、硬件部署架构

### 目标形态

```
┌────────────────────────────┐   USB 921600    ┌──────────────────────────┐
│ 主控 N100/N5015/RK3588      │ ──────────────► │ 轮趣 C30D（单板下位机）    │
│                            │  micro-ROS      │ STM32F407 + micro-ROS     │
│  · 皮皮主程序               │                 │                          │
│  · SenseVoice / 人脸识别     │ ◄────────────── │  · 麦轮解算 + 4 路 PID    │
│  · ROS2 节点 pipi_base      │  /odom /cmd_vel │  · 2 路 50Hz 舵机 PWM     │
│  · micro-ROS agent          │  /pipi/head     │  · WS2812 灯带时序         │
│  · 摄像头 USB                │                 │  · 硬件级急停看门狗         │
└────────────────────────────┘                 └───────────┬──────────────┘
      │                                                    │
      ▼                                          ┌─────────┼─────────┬──────────┐
  屏幕/喇叭                                       ▼         ▼         ▼          ▼
  （皮皮的脸/声音）                          头部左右舵机  俯仰舵机  表情灯带  4×麦轮电机
```

**为什么下位机只留一块（C30D）？**
主控跑 Linux，调度抖动可达几十毫秒 —— 直接驱舵机会抽搐。
所以实时控制必须下沉到 MCU，但**不需要两块 MCU**：
C30D 本身就是 STM32F407，定时器通道足够同时产生两路 50Hz 舵机 PWM、
一路 WS2812 时序，并驱动 4 路编码器电机的 PID 闭环。
把头部也交给它，省掉一条 UART 线、一颗要单独烧固件的芯片、
以及两套固件版本对不齐的风险。主控只下发**语义指令**
（"点头"/"看向说话人"/"往前走"），经 ROS2 话题到达 C30D。

### ROS2 话题契约（稳定接口，替代私有串口协议）

| 方向 | 话题 | 类型 | 内容 |
|---|---|---|---|
| 主控 → C30D | `/cmd_vel` | `geometry_msgs/Twist` | 底盘速度：`linear.x` 前后、`linear.y` 横移、`angular.z` 自转 |
| 主控 → C30D | `/pipi/head` | `std_msgs/Float32MultiArray` | 头部角度：`[pan°, tilt°, led(0..1)]`，**顺序必须与固件一致** |
| C30D → 主控 | `/odom` | `nav_msgs/Odometry` | 编码器里程计回传 |

**为什么不用串口私有协议**：协议会随固件版本漂移，而 ROS2 话题是稳定契约；
而且 DDS 自带发现与 QoS，以后加雷达（SLAM）、手柄遥控、导航栈，
只是在话题图里加节点，皮皮代码一行不用改。

### 两套语义（容易踩的坑）

| | 底盘三轴 | 头部两轴 + LED |
|---|---|---|
| 语义 | **速度**（−1..1 → ±0.5 m/s 等） | **角度**（−1..1 → ±120° / ±45°） |
| 超时会怎样 | 一直开走 → **必须看门狗** | 停在该角度，无风险 |
| 软件看门狗 | 500ms 无指令自动补发全零 Twist | **不设**（位置语义不需要） |
| 急停行为 | 归零 | **刻意保留角度**（突然垂头会砸到人），只关灯 |

### 语义动作库（`GESTURE_SEQUENCES`）

| 动作 | 中文 | 触发场景 |
|---|---|---|
| `NOD` | 点头 | 认可、开心 |
| `SHAKE_HEAD` | 摇头 | 否认、吃醋 |
| `LOOK_AT` | 看向说话人 | 用户开口（云台跟人） |
| `LEAN_IN` | 探身 | 关心、认真听 |
| `TURN_AWAY` | 扭头 | 生气、傲娇 |
| `PERK_UP` | 竖起身子 | 主人回家 |
| `SLEEP` | 垂头 | 困倦、夜间 |
| `HAPPY_WIGGLE` | 开心晃动 | 被夸 |
| `FOLLOW_OWNER` | 跟随主人 | 主人在屋里走动 |
| `SIDE_STEP` | 侧身滑开 | 傲娇/吃醋（麦轮横移，底盘专属） |

情绪 → 动作的映射在 `EmotionEngine.mood_to_gesture()`，
编排器每轮自动调用（有 MotionProvider 时生效）。
单板方案下整条序列都由 C30D 消化，不再需要轴级路由。

### 部署清单

```bash
# 主控（x86 推荐 N100/N5015）
sudo apt install python3-opencv portaudio19-dev
pip install -e ".[speech,vision-lite,web,hardware,llm]"
python3 scripts/download_models.py --only asr

# ROS2 + micro-ROS agent
sudo apt install ros-humble-ros-base ros-humble-micro-ros-agent
ros2 run micro_ros_agent micro_ros_agent serial --dev /dev/ttyUSB0 -b 921600

# C30D：在官方固件基础上加 micro-ROS app（订阅 /cmd_vel + /pipi/head，
#       发布 /odom；详见 HARDWARE.md §8.3）
# configs/default.yaml:
#   motion: { enabled: true, provider: wheeltec }
python3 run.py --voice    # 窗口进程（WS 默认监听 0.0.0.0:8848）
```

### 底盘机械设计（移动形态）

软件只关心 `/cmd_vel` 的语义，**底盘长什么样、电池怎么选** 是独立的一层。
这部分单独成文：**[`CHASSIS.md`](CHASSIS.md)**。

| 关键结论 | 值 |
|---|---|
| 布局 | **真·十字形**（4 轮 90° 均布于圆柱壳，贴合陪伴机器人形态） |
| 轮距 / 轴距 | 240 × 240 mm（十字对称） |
| 车体外接圆 | Ø300 mm |
| 整机重量 | 3.29 kg |
| 重心离地 | 55.2 mm（**电池下挂**压低 19%） |
| 电池 | **18650 3S2P 7.0Ah**（¥167，活跃续航 2.5h，兼容 21700/26650） |
| 侧翻裕度 | 21.4× |
| **自动回充** | **AprilTag(远) + 双红外(近)，三段解耦 `vx`/`vy`/`wz`，桩 ¥80~150** |
| 参数化模型 | `hardware/*.scad`（已从仓库移除，git 历史可取回） |

> **为什么十字而不是方形**：皮皮是圆柱壳，四轮 90° 均布天然贴合、旋转时车体扫过即自身轮廓，
> 不会像方形底盘那样「甩角」蹭家具。代价是绕 Z 力矩臂小 29.3%，靠 footprint 从 170mm 放大到 240mm 补回。
> 麦轮必须按 **L-R-R-L**（对角同向）装成 X 型，否则不能横移。

> **为什么自动回充对麦轮是"送分题"**：差速底盘只有 `(vx, wz)`，距离与偏角耦合，
> 只能倒 S 形曲线；麦轮有 `(vx, vy, wz)` 三个正交自由度，可把对接**解耦**成
> 「缩距 / 调心偏 / 调角偏」三段独立闭环，**C30D 固件一行不用改**。
> 编排层新增 `orchestration/dock.py` 的 `DockController`（纯逻辑、可单测），
> provider 只加 `dock_velocity()` 限速发布。详见 [`CHASSIS.md`](CHASSIS.md) §12。

---

## 八、隐私设计

| 开关 | 默认 | 说明 |
|---|---|---|
| `memory.allow_user_forget` | true | 用户可查看/删除**任何**记忆 |
| `vision.enabled` | true | 整块摄像头能力可一键关闭 |
| `vision.privacy.save_frames` | **false** | 原始画面绝不落盘 |
| `vision.privacy.save_observations` | true | 只存结构化观测（"穿着灰色的衣服"） |
| `vision.privacy.observation_retention_days` | 7 | 观测自动过期 |

**原则：原始数据（音频、画面）绝不持久化，只留结构化、可读、可删的结论。**

CLI 里 `/privacy` 查看开关，`/forget N` 删单条，`/reset` 清空全部。
（旧 Web 观测台的删除/钉住按钮已随该模块移除，功能由 CLI 命令与 web/monitor.html 承接。）

---

## 九、可观测性

- **trace_id**：每个会话有 trace_id，贯穿 ASR → LLM → TTS → 落库
- **健康矩阵**：启动时打印，随时 `GET /api/health`
- **遗忘报告**：每次 decay pass 记录 `checked/decayed/forgotten/pinned_kept`
- **主动日志**：每次开口/被压制都记原因（`proactive_log` 表）
- **会话回放**：`scripts/replay_session.py` 把历史对话重演，
  用于验证 Prompt 改动是否真的变好（而不是凭感觉）
- **成本控制**：`llm.routing` 让简单寒暄（<8 字且无复杂意图）走便宜模型；
  记忆抽取/情感分析默认走 `cheap_model`。
  **但真正的省钱杠杆不是换小模型**：
  ① 人格 Prompt 是每轮不变的前缀 → 放 messages 最前可命中**前缀缓存**
  （输入成本降 70%+）；② 辅助任务与对话任务分开计费。
  实测闲聊约 **¥0.15/千轮**（Flash 档，≈66 万轮/百元），成本不是瓶颈，
  **延迟与人格一致性才是**。详见 [`LLM_SELECTION.md`](LLM_SELECTION.md)。
- **LLM 降级链**：`qwen → deepseek → mock`（**纯云端方案，不做本地部署**）。
  两家不同云厂商是刻意的——物理隔离、互为灾备，避免单厂商故障/限流全灭。
  全挂时由 `mock` 用规则话术兜住基本陪伴，不引入本地推理环境
  （本地跑 3B 会与视觉、ROS2 抢 N100 的 CPU，性价比不成立）。
- **平台选择**：⚠️ 皮皮的真正开销在**语音**（TTS 占全链路 81%、ASR 18%），
  **LLM 只占 0.8%** —— 所以选平台的第一优先级是"哪家 ASR/TTS 好用"，
  而不是"哪家 LLM 便宜"。推荐 **LLM 走阿里百炼（延迟低、额度实在），
  ASR/TTS 走火山引擎（情感语气标签 + 声线复刻）**；怕麻烦全押火山也完全够用。
  详见 [`LLM_SELECTION.md`](LLM_SELECTION.md) §2.5。
- **语音链路架构**：当前为**级联**（ASR → LLM → TTS）。端到端 S2S（火山
  Speech2Speech）的混合架构方案已评估未采纳（S2S 黑盒无中间文本，工具调用
  插不进去；预留配置已于 2026-10-10 移除），分析见
  [`LLM_SELECTION.md`](LLM_SELECTION.md) §9。

---

## 十、验证与测试

| 验证项 | 方式 | 结果 |
|---|---|---|
| 稀疏衰减 | 虚拟时钟快进 30 天 | 身份 sal=1.0 不变；闲聊 18h 半衰期；30 天后淘汰 √ |
| 检索强化 | 反复提及同一事实 | access_count 累积，"他叫阿哲"被想起 134 次 √ |
| 人格 5 问 | e2e 脚本 | 拒绝写报告 / 接情绪 / 吃醋 / 否认是 AI √ |
| 视觉→语言 | 单测 | 输出自然语言，无 JSON 泄漏 √ |
| 回家打招呼 | 集成测试 | 触发 return_home，冷却期内不重复 √ |
| 频率控制 | 集成测试 | 上限/静默/降频/恢复全部生效 √ |
| 触发器 | FakeClock 快进 | idle/morning/night_sleep/重要日子/情绪跟进 全通过 √ |
| 优先级 | 集成测试 | 回家(90) 压过 闲置(50) √ |
| 运动层 | 集成测试 | Mock 语义动作 + C30D 单板（头部/灯/麦轮六轴）√ |
| **合计** | `tests/integration/test_proactive_vision.py` | **48/48 通过** |

遗忘曲线的实测数据（`scripts/demo_forgetting_curve.py` 输出）：

```
类型 / 内容                          初始      7天     30天      半衰期
  chitchat 他今天说天气不错…          0.300   0.001   0.000      18h
📌commitment 他和你约定过：早点睡      0.900   0.900   0.900       永不
  event 他提到周末的事               0.650   0.290   0.021     144h
📌identity 他叫阿哲                  1.000   1.000   1.000       永不
  preference 他喜欢喝美式            0.750   0.671   0.466    1047h
```

30 天后巡检：淘汰 3 条（2 条闲聊 + 1 条过期事件），
保留身份 / 约定 / 喜好 5 条 —— **不对称性成立**。

运行全部验证：

```bash
python3 tests/integration/test_proactive_vision.py   # 36 项断言
python3 scripts/demo_forgetting_curve.py             # 遗忘曲线实测
python3 scripts/seed_demo_data.py --clean --days 60  # 造 60 天记忆
python3 scripts/replay_session.py --limit 20         # 会话回放
python3 run.py --sim                                 # 端到端演示（监控页 web/monitor.html）
```

---

## 十一、扩展点

| 想加的能力 | 要做什么 |
|---|---|
| 换 LLM | `providers/llm/` 加实现，`PRESETS` 加预设 |
| 换 TTS/音色 | `providers/tts/` 加实现，链上换名 |
| 唤醒词 | `interfaces/` 加 `wake.py`（配置里已预留 `wake_word` 段） |
| 新人格 | `configs/personas/` 加 YAML，`persona.active` 切换 |
| 新记忆类型 | `domain/memory.py` 的 `MemoryType` + λ_base 表 |
| 新情绪 | `domain/emotion.py` 的 `Mood` + `MOOD_TRANSITIONS` |
| 新触发动 | `orchestration/proactive.py` 的 `default_rules` + 候选收集 |
| 多模态输入 | 加 Provider，在 `PerceptionService.fuse()` 里融合 |
| 人格迁移（人格 OS） | `memory_store.export()` 已可导出人格档案 + 记忆 |

---

## 附录 A：避免 AI 味的方法论

（源自 `皮皮人格Prompt方案.md`，此处为架构落地）

**四条硬规则**（在 `capabilities/expression.py` 里强制执行）：

1. **禁止 Markdown**：`strip_markdown()` 去掉 `**加粗**`、`# 标题`、`- 列表`。
   人会说话，不会说话带格式。
2. **禁止 AI 腔**：`strip_ai_phrases()` 删掉"作为AI助手"、"很高兴为您服务"、
   "希望这个回答对您有帮助"这类模板句。
3. **长度熔断**：`enforce_length()` 按 `persona.verbosity` 截断 ——
   真人聊天是一两句，不是一段论述。
4. **拆气泡**：`split_bubbles()` 把一段话按自然停顿拆成多条短消息，
   模拟真人"分几次发"的节奏。

**一个关键洞察**：AI 味往往不在**用词**，而在**信息密度**。
真人回复一条消息，通常只承接对方一句话里的一个点，剩下的是留白。
所以皮皮的 Prompt 里明确写了"不要面面俱到地回应每一点"，
expression 层再做长度熔断兜底。

**四大机制**（对应到架构）：

| 机制 | 架构落点 |
|---|---|
| 没有 AI 味 | `expression.py` + Prompt 硬规则 |
| 主动关心 | `ProactiveEngine` + `companion_need` 累积 |
| 会吃醋告状 | `MOOD_TRANSITIONS[mention_rival]` + `profile.rivals` |
| 记忆认主 | 稀疏记忆 `IDENTITY.pinned` + `FaceRepo` 人脸识别 |

---

## 附录 B：关键设计决策记录（ADR 摘要）

| 决策 | 选择 | 理由 |
|---|---|---|
| 语言 | Python | 感知模型（SenseVoice/InsightFace）全在 Python 生态；Go+Eino 只借鉴三 Agent 思想 |
| 语音 | 可插拔 Provider | 本地/云/Mock 三档，用户按硬件条件选 |
| 存储 | SQLite + struct 打包向量 | 单文件、零运维；embedding 打包比 JSON 小 4 倍 |
| 向量 | 三级降级 bge→TF-IDF→Hash | 保证无模型也能语义检索（退化到关键词） |
| 记忆 | 稀疏 + pinned | 全忘/全记都不像人；身份必须锚定 |
| 交互 | 监控走 WebSocket（ws_server.py 内嵌窗口进程），静态页双击即开 | 部署形态只有一个进程 + 一个端口，Docker 友好 |
| 前端 | 单文件 HTML 零构建 | "clone 下来就能跑"比工程化更重要 |
| 时钟 | 可注入 | 时间相关逻辑不测就等于没实现 |
| 好感度 | sqrt 压缩 + 关系底价 | 线性衰减会让长期关系归零，不符合真实心理 |
| 视觉输入 | 翻译成自然语言 | 直接塞 JSON 会让模型露出"字段感" |
| 硬件 | 主控（N100/N5015/RK3588）+ 轮趣 C30D 单板下位机 | Linux 调度抖动会毁掉舵机动作质量；单板省一条 UART 与一套固件 |
| 生命模块 | PULSE 8→4 精简（消融驱动：Tension/Will/Bond 核心保留，Affect/Rhythm 派生化、Anticipation 习惯化、Seed 内嵌、legacy 删除） | 八器官状态过多、难以解释；消融实验证明四核心即维持全部行为保真 |

---

*文档版本 0.1 · 配套代码见 `src/pipi/`*
