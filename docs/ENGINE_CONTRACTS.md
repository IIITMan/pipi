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

## 6. 后续阶段（已批准计划，未实施）

### 6.1 阶段 2：控制车道独立 + 情绪推断伦理护栏

- 停/静音/取消走不经过 orchestrator/LLM 的独立通道；用户说话时主动让路、播放中可取消、已取消不自动恢复。
- 重设 `MOOD_TRANSITIONS` 推断规则：`idle`/`ignored` 走降频通道（更安静而非更委屈）；
  `mention_rival` 保留（显式语义）但增加"用户明确不喜欢 → 永久关闭该触发器"学习开关。
- 验收：连续 3 次被忽视后更安静而非更委屈；`/quiet` 期间 LLM 不可唤醒主动发言。

### 6.2 阶段 3：LLM 边界与确定性回放

- 对话回复（长度/结构）与记忆抽取（JSON）输出过 schema 校验，非法即弃 + 有界重试。
- "分类不授权"：LLM/规则意图分类必须过 capability allowlist 与安全检查才执行。
- 目的化上下文视图：记忆抽取/情感分析/对话生成各自使用最小必要视图。
- `replay_session.py` 支持注入 fixture LLM，Prompt 改动回归完全确定性。
