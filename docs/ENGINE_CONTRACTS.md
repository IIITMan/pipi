# ENGINE_CONTRACTS.md — pipi 状态契约

版本：0.1.0
状态：阶段 1（状态所有权清算）已落地；阶段 2/3 计划见 §6
对标：`D:\Work\continuity_engine\docs\ENGINE_CONTRACTS.md`（CE 契约范式）

## 1. 系统边界

pipi 的"生命与关系"由三个子系统协作，**各自的权威域必须唯一**：

- **PULSE（`src/pipi/pulse/`）**：生命循环引擎。自持心境、羁绊、驱力、意志与
  睡眠，1Hz tick 独立演化；对外只接受**事件注入**（`LivingLoop.inject`）。
- **编排层（`src/pipi/orchestration/`）**：对话回合编排。EmotionEngine/EmotionAnalyst
  只做**每轮信号抽取**，不得成为持久状态写者；GrowthEngine 只写**事实记录**
  （里程碑/统计/偏好/表达），阶段机为只读派生。
- **记忆层（`src/pipi/capabilities/memory_manager.py`）**：长期记忆的唯一写入者。

不承担（对照 CE §1）：
- 原始音频/视频感知推理（感知服务负责）。
- 实时电机/安全急停（独立控制路径，阶段 2 落地控制车道）。
- 无约束 LLM 输出执行（阶段 3 LLM_OUTPUT_CONTRACT）。
- 未经验证的事实写入长期记忆。

## 2. 核心不变量（MUST）

1. **每个持久状态字段有唯一权威写入者**（多写者防护——本契约的最高优先）。
2. 每次状态提交须通过类型/范围/时间校验；`NaN`/±inf/越界/未知枚举不得进入已提交状态。
3. 事件带稳定 kind 与 payload；同一事件不得经多个模块重复累加。
4. 当前意图不能因一次普通驱力波动而无条件改变（Will 滞回，见 PULSE_REVIEW_TODO P0）。
5. 用户取消/静音/停止必须使对应请求失效，且不自动恢复（阶段 2 控制车道）。
6. 安全监督可否决动作；驱力、情感与 LLM 输出不能覆盖否决结果。
7. LLM/网络/非关键传感器不可用不得阻塞核心状态循环。
8. 需要跨重启保留的偏好与记忆必须持久化并有删除路径。
9. 对话情绪推断不得把"沉默/离开/拒绝互动"判定为用户冷落或需要安慰
   （阶段 2 伦理护栏，见 §6.2）。

## 3. 状态所有权（对标 CE §3 格式）

### 3.1 字段级所有权表

| 状态维度 | 字段 | 唯一权威写入者 | 读取方 | 降级/退役说明 |
|---|---|---|---|---|
| 心境 | `_mood_base` / `_mood_pulse` / `_burst` / `_arousal` | **PULSE LivingLoop**（`push_mood_base` / `push_mood_pulse` / `trigger_burst`） | runtime 天气/呼吸、monitor、inner_brief | EmotionEngine 为**信号源**：经 `dialogue_mood` 事件注入（§4.1）；`_MOOD_BASE` 隐式映射已删除（2026-10-13） |
| 心境 | `_breath_rate` / `_breath_depth` / `_breath_phase` | **PULSE LivingLoop**（derive.breath_step） | body/monitor | 纯派生，无外部写者 |
| 羁绊 | `trust`（0–100） | **PULSE Bond**（重聚/冷淡/思念/认主/跨天推进） | inner_brief、monitor、growth 派生、PULSE 内部 | 无第二写者；Bond 为关系唯一权威 |
| 羁绊 | 思念曲线 / `imprinted` 认主 / `interact_days` | **PULSE Bond** | inner_brief、P123 验证 | 同上 |
| 好感 | `affection`（EmotionState，0–1000） | ~~EmotionEngine~~ → **退役为瞬时展示值** | prompt/展示 | 不再参与成长阶段判定（旧 S4 门槛恒 500<550 死门槛，随 1b 移除）；关系质量只看 trust |
| 成长 | S1–S4 阶段（`relationship_stage`） | **无**（只读派生） | growth.snapshot、monitor | 由 `trust + 里程碑 + 活跃天数 + 昵称授权` 派生（§5.2）；引擎不再写该列 |
| 成长 | 里程碑 / 活跃天数 / 晚安计数 / 偏好 / 口头禅 | **GrowthEngine**（事实记录） | 档案、记忆、monitor | 事实不是关系状态，允许 GrowthEngine 唯一写入 |
| 记忆 | MemoryManager 全库 | **MemoryManager**（commit/queue_turn） | 对话检索（memory_expert） | 日记经 `_commit_diary` **单一通道**写入（§5.3）；diary_memory 为正式接口（§5.1） |
| 对话情绪（瞬时） | `mood` / `intensity` / `companion_need` / `is_attachment` | **EmotionEngine**（每轮，回合内） | prompt、TTS 语气、动作、proactive 引擎 | 回合级瞬时信号；持久化仅为跨重启行为基线（companion_need 恢复），**非关系权威** |

### 3.2 写入路径总览

```
用户/感知/LLM 信号
   │
   ▼
EmotionEngine（信号抽取，每轮）          PULSE Bond（羁绊演化，1Hz）
   │  mood/intensity                        │  trust/思念/认主
   ▼                                        ▼
dialogue_mood 事件 ──inject──▶ LivingLoop ──▶ 心境（_mood_base/_mood_pulse）
                                        │
                                        ▼
GrowthEngine（事实记录：里程碑/统计/偏好）
   │  stage = 派生(trust, 里程碑, 天数) —— 不写库
   ▼
MemoryManager ◀──（日记单通道 _commit_diary）── River.consolidate_night
```

## 4. 事件契约

### 4.1 `dialogue_mood`（心境注入事件，2026-10-13 新增）

- **kind**：`dialogue_mood`
- **source**：EmotionEngine（每轮对话结束，经 runtime → loop）
- **payload**：`{ "mood": str, "intensity": float }`
  - `mood`：中文情绪标签（开心/得意/调皮/关心/平静/困倦/傲娇/吃醋/委屈/生气）
  - `intensity`：本回合情绪强度（0–1，当前不参与数值，供契约演进与审计）
- **处理**（`LivingLoop.inject` 显式分支，映射常量 `_MOOD_DELTA` 在 loop 内声明）：
  正面类 → `push_mood_base(+0.05…+0.18)`；负面类 → `push_mood_base(−0.05…−0.16)`；
  平静 → 无操作；未知标签 → 无操作。映射即语义规则，可审计、可单测，
  **不再是 runtime 的隐式桥接字典**。
- **禁止**：dialogue_mood 不得直接改写 Bond.trust、Tension 或意图。

### 4.2 现有事件一览（PULSE 注入面）

| kind | 来源 | 处理 |
|---|---|---|
| `owner_depart` / `owner_arrive` / `owner_ignored` / `owner_sad` | 感知 | Bond/Tension/心境 |
| `user_voice` / `barge_in` / `response_done` / `farewell` | 会话 | 互动计数/心境/意图中断 |
| `stranger` / `night_noise` | 感知 | 习惯化惊吓门控（终身一次） |
| `quiet_home` | 会话 | 安静陪伴 |
| `weather` | WeatherService | 心境微调 |
| `reminder`（note_event） | 外部模块 | 日记留痕 |

事件统一进 `loop.inject(kind, minute=…, **kw)`；未知 kind 返回空串、不抛异常。

## 5. 日记与记忆库分层

### 5.1 diary_memory 正式接口

`PulseRuntime.set_diary_memory(enabled: bool)` 为唯一开关入口；
构造时由 `config.pulse.diary_memory` 初始化，禁止散落读取。

### 5.2 分层职责

| 层 | 载体 | 职责 | 生命周期 |
|---|---|---|---|
| 当日叙事源 | `LivingLoop._diary` + `River.consolidate_night()` | 当天事件留痕、入睡成文（零 LLM）、自我深度 | 每 24h 清空重来 |
| 长期检索库 | MemoryManager（EVENT/PREFERENCE/COMMITMENT） | 跨重启的事实、偏好、承诺、里程碑 | 持久化，有删除路径 |

### 5.3 写入通道

日记→记忆库**只有一条通道**：`consolidate_night()` 产出叙事 →
`PulseRuntime._commit_diary()`（MemoryType.EVENT, importance 0.55）→ MemoryManager.commit。
`diary_memory=false` 时通道关闭，叙事只存在于事件流（监控可见）。

### 5.4 产品约束：情绪推断伦理护栏（MUST，阶段 2 已落地）

**绝不从用户的沉默、离开、拒绝互动推断"冷落/难过/需要安慰"。**
机器人可以对缺失反应作出行为性回应（更安静、降低主动频率、减少打扰），
但**不得**把缺失反应量化为委屈、伤心、被冷落等负面情绪状态，也不得在
回复中输出此类归因。

| 情境 | 允许 | 禁止 |
|---|---|---|
| 长时间未交互（idle） | `MOOD_TRANSITIONS["idle"]`→CALM；陪伴需求累积（need_delta=+0.15）；主动搭话上限减半（沉默 ≥ 2×idle_hours） | target=AFFECTED（委屈）、affection_delta<0 |
| 主动搭话被忽视（ignored） | `MOOD_TRANSITIONS["ignored"]`→CALM；`note_ignored()` 降频（≥2 次减半、≥3 次归零）；自发发声 10min 无回应 → 运行时降频 | target=AFFECTED、need/affection 惩罚 |
| 用户提到竞争对象（mention_rival） | **保留**吃醋反应（用户显式输入的语义，非推断） | 无 |
| 用户明确表示不喜欢吃醋反应 | 学习开关置位（`profile.extra.rival_trigger_disabled`）→ 永久关闭 mention_rival 触发器（不再扣好感、不进入吃醋，仅保持平静） | 继续吃醋 |
| 控制车道静音（/quiet） | 所有主动/自发/预警发声被抑制；当前播放立即终止；`/resume` 恢复 | LLM 渲染或 orchestrator 唤醒任何主动发言 |

## 6. 后续阶段

### 6.1 阶段 2：控制车道独立 + 情绪推断伦理护栏（**已落地**，提交后见 git log）

- 控制直达路径：`/quiet`（静音）/`/resume`/`/stop`（停止当前播放）在 CLI
  命令层直接处理，**不经 orchestrator/LLM**；静音门 `session.is_quiet` 在
  发声出口（`try_speak_spontaneous`）与 PULSE 发声入口
  （`_maybe_vocal`/`_speak_alert`/`_render_and_speak`）双重生效，静音期间
  LLM 渲染也不会被调用；`speaker.abort()` 终止播放，已取消不自动恢复。
- `MOOD_TRANSITIONS` 重设：`idle`/`ignored` → CALM（见 §5.4），委屈不再由
  沉默/被忽视产生；`mention_rival` 保留 + 学习开关（`check_rival_opt_out`
  识别"别吃醋/不喜欢这样"等 → `profile.extra.rival_trigger_disabled`）。
- 被忽视降频运行时接通：自发发声 10 分钟无任何回应 → `set_ignored_observer`
  回调 → `proactive_engine.note_ignored()`（上限减半/归零）。
- 验收：连续 3 次被忽视后更安静而非更委屈（ignored_streak≥3 → 上限 0）；
  `/quiet` 期间 LLM 不可唤醒任何主动发言；48/48 + 34/34 + 14/14 回归不变。

### 6.2 阶段 3：LLM 边界与确定性回放（已批准计划，未实施）

- 对话回复（长度/结构）与记忆抽取（JSON）输出过 schema 校验，非法即弃 + 有界重试。
- "分类不授权"：LLM/规则意图分类必须过 capability allowlist 与安全检查才执行。
- 目的化上下文视图：记忆抽取/情感分析/对话生成各自使用最小必要视图。
- `replay_session.py` 支持注入 fixture LLM，Prompt 改动回归完全确定性。
