# 生命模块（v2.1）接入记录 —— 设计文档 vs 现有方案的决策

> 本文档记录《皮皮机器人｜生命模块 v2.1 设计文档》落地时的全部冲突裁决。
> 原则：**文档与现有对话方案冲突时，以现有为准**，本文档留痕。
> 实现范围：阶段 1（PC 原型 + Mock 动作）；阶段 2~4 见文末待办。

## 1. 目录与模块归属

| 文档（§10） | 实际落地 | 原因 |
|---|---|---|
| 顶层 `life_module/` 目录 | `src/pipi/life/` 包 | 现有工程为 `src/pipi/` 包布局，保持统一 |
| `main.py` 独立入口 | 生命引擎由 `run.py --voice`（CLI.run_voice）随会话启动/停止 | 现有入口是 `run.py`，生命模块与语音会话强耦合，单独入口无意义 |
| `llm_prompt/*.txt` | `src/pipi/prompts/thought.jinja`、`micro_expression.jinja` | 现有 Prompt 统一用 Jinja 模板 + `pipi.prompts.render` |
| `memory/memory_store.py` 三层记忆 | **复用现有 memory_manager**（SQLite 抽取式记忆，自带情感上下文） | 现有记忆系统已覆盖文档的中期摘要（异步 LLM 抽取带类型/情感）、长期稀疏存储（半衰期/钉住）、短期上下文（Session）；避免两套记忆 |

## 2. 状态机映射（§3 → 现有 InteractionSession）

文档的 5 状态与现有会话 3 状态的映射（所有权在现有会话，生命模块只读）：

| 文档状态 | 现有会话状态 | 说明 |
|---|---|---|
| `sleep`（仅 KWS） | —（合并进 `session.IDLE`） | 现有 IDLE 本来就只跑 KWS+能量门控，ASR 不常开；不单独设 sleep。疲劳 >7.5 时生命模块停止念头、注意力切 rest |
| `idle` | `session.IDLE` | VAD+KWS 协同（VAD 攒段 + sherpa 命中才识别），念头引擎在此运行 |
| `chatting` | `session.LISTEN` + `session.RESPOND`（被动应答） | 对话中念头降优先级、不抢话 |
| `muttering` | `session.RESPOND`（自发念头发声时） | 复用同一 Barge-in 通道 |
| `distracted` | 预留（未自动触发） | 无噪音/触碰传感器；`BehaviorScheduler.startle()` 已实现，待阶段 3 感官接入 |

## 3. 并发模型（§8）

文档要求后台多线程；现有工程为 asyncio 单事件循环，**线程 → 协程任务**：
- 生理更新 1Hz、念头生成 3Hz、行为调度 2Hz 均为 `asyncio.Task`（`life/engine.py`）
- 职责与文档一致；记忆摘要复用现有 `_post_turn` 异步抽取，不另建线程
- 感知主循环（VAD/KWS/ASR/Barge-in）保持在会话主协程，生命模块**不阻塞**它

## 4. 念头生成（§4）

- 3Hz 生成**不做 LLM 调用**（成本与延迟不可接受）：念头内容由规则引擎从
  lonely/bored/curiosity/want_attention 状态生成，入队（TTL 5s，§9.2）
- LLM 只在**发声念头被调度执行时**渲染为自然口语（`thought.jinja`，
  cheap 模型，12s 超时，失败回退模板句）
- 微表达（§6.3）：**有意搁置**。`micro_expression.jinja` 已就位，但接入会
  增加自发发声频率，与「减少主动念叨、本体行为为主」（2026-10 重构决策）
  冲突；待机活感已由 Web 眼睛表情 + 巡逻/探索念头承担。硬件阶段再评估
- 大量念头在 TTL 过期/冷却/概率门控中被丢弃——与文档"消除机器问答感"目标一致

## 5. 发声通道（§6.1 / §12）

- 被动应答：现有 `handle_turn_stream`（LLM SSE 流式 → 分句 TTS → 边生成边播放）
- 自发念头：`session.try_speak_spontaneous(text)` —— 文本已定，不走 LLM 应答，
  直接经 `SentenceTTSPlayer` 播报，**复用同一 Barge-in 打断机制**
- TTS：CosyVoice 方案已整体移除（官方栈仅 Linux、需 GPU，Windows/原型环境
  从未可用）；现降级链为 `ark_tts → edge_tts`，免费兜底 edge-tts

## 6. 动作层（§7）

- `ActionAdapter` 抽象 + `MockActionAdapter`（日志）+ `ROS2ActionClient`（阶段 2 占位）
- 配置开关 `life.action_backend: mock | ros2`
- 会话 `try_speak_spontaneous` 返回 False（会话忙）时，发声念头转内心/丢弃
- Barge-in 时 `action.cancel_all()` 与 TTS 停播同步触发（§7.4 规则 3）

## 7. 感官事件注入（§12.5）

| 文档事件 | 当前实现 |
|---|---|
| VAD 人声 | `session._step_listen` 响帧 → `life.observe_user_voice()`（引擎内 1s 节流） |
| Barge-in 打断 | `session._do_barge_in` → `life.on_barge_in()`（alert+，mood−，取消动作） |
| 应答完成 | `_step_respond` 自然结束 → `life.on_response_done()`（fatigue+，mood+） |
| 突发噪音 / 触碰 / 电池 | 阶段 3 感官硬件接入 |

## 8. 阶段 2~4 待办

- [ ] 阶段 2：ROS2 Action Client/Server 实装（PC 端 Mock 硬件闭环；
      动作语义名已定：patrol_route / look_around / wander_turn / slow_forward / go_home）
- [ ] 阶段 3：舵机/LED/底盘实体接入；触碰、电池状态 → 生理变量
- [ ] 阶段 4：记忆摘要带情感标签强化、记忆闪回念头概率调优、昼夜生物钟
- [ ] `micro_expression.jinja` 切换 LLM 渲染（见 §4：有意搁置，硬件阶段再评估）
- [ ] `distracted` 状态自动触发（需要噪音强度传感器数据）

## 9. 2026-10-08 审计修复留痕

- **P0 监控上报堆积**：原 `to_thread(_post)` 已随 WS 重构移除；当前风险在
  `WSHub.broadcast`——慢客户端 `ws.send` 无限阻塞 → 已加单帧 2s 超时 +
  背压丢帧（上一帧未发完跳过本轮）+ 僵死客户端强制剔除
- **P0 私有属性直访**：`physio._last_user_voice` → 公开方法
  `seconds_since_user_voice()`；`_physio_loop` 加异常保护（单次异常不再静默终止循环）
- **P1 WebLifeHost.spoken 无上限** → `deque(maxlen=50)`
- 部署形态变更：生命引擎监控数据不再 HTTP POST，改 `set_sink()` 回调 →
  窗口端内嵌 WS 推送（`interfaces/ws_server.py`），Web 为纯静态页订阅

## 10. 2026-10-08 语音链收敛 + 记忆污染修复

### 语音链（三轮收敛后的最终形态）

- 阶段 1：VAD → sherpa-onnx silero_vad（去 torch）
- 阶段 2：KWS → sherpa KeywordSpotter（zipformer 3.3M，keywords.txt 拼音 token）
- 阶段 3：流式 ASR 试验（zipformer 14M）→ 最终**移除**；ASR 只留
  sherpa SenseVoice int8（239MB，带情感/事件/ITN，单句 ~100ms）
- 降级路径全部删除：SileroVAD(torch)/EnergyVAD/SenseVoiceKWS/funasr 后端
  不复存在，模型缺失直接报错。链路 = KWS(3.3M) + VAD(silero) + ASR(239MB)，
  全 onnxruntime，无 torch/funasr（合计省 ~2GB 内存与 1.1GB 模型盘占）
- 弃用记录：`streaming-zipformer-bilingual-zh-en-2023-02-20` 在 sherpa 1.13.x
  下有复读伪影（int8/fp32/beam search 均复现），勿再用

### 记忆污染修复（"皮皮不知道用户名字"事故）

- 病根：抽取正则把「我没有告诉你我叫什么吗」抽成 identity『他叫什么吗』
  （pinned/imp=1.0），连同『皮皮不知道用户的名字』『他住在和你聊天啊』
  挤掉真记忆；且 `profiles.owner.nickname` 被污染成『什么吗』直接进 Prompt
- 修复：
  1. 名字抽取加垃圾字符门禁；住地去掉过松「我在」分支
  2. `_score_and_finalize` 统一质检门：候选与档案字段（nickname/call_name/
     city/job/living）含疑问否定语气即丢弃（覆盖规则 + LLM 两条抽取路）
  3. 存量清洗：软删 3 条垃圾记忆；profiles.owner 修复为「豆豆」
  4. `add_milestone` 改 `INSERT OR IGNORE` 幂等静默
- 待办（阶段 3 感官硬件接入时）：触碰/电池 → 生理变量；`distracted` 自动触发
