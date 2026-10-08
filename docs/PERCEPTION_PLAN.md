# 感知接入生命模块（摄像头 → LifeEngine）实施计划

> 状态：已确认方向，待实施
> 目标：把现有摄像头感知接进生命模块，让皮皮待机时"看得见"，
> 为后续「自主思考」（动机/思想流）提供真实输入。
> 范围边界：本阶段**不做场景理解**（客厅/书桌识别留 G3），只做人与表情事件。

---

## 1. 背景与动机

"更像有生命、能自主思考"的前提是**有感知**：一个对世界几乎无感知的系统，
"想什么"只能从记忆和生理三轴里挤，独白必然空洞（详见自主思考五层方案的风险评估）。

现状缺口（已查代码确认）：

- `src/pipi/providers/vision/` 已有完整 pipeline：人脸检测（YuNet/MediaPipe/Haar）、
  认主（InsightFace）、表情（hsemotion/FER）、衣着颜色（ColorClothingAnalyzer），
  低频采样（默认 1.5fps），无摄像头自动降级 `MockVision`。
- **但 vision 只在对话时被 orchestrator 拍一帧**（`handle_turn` 传 `vision=...`），
  以及 proactive 触发时拍一次（旧 `web.py`，该模块现已移除）。
- **`LifeEngine` 完全没有引用 vision** —— 皮皮待机时是"瞎的"，
  摄像头存在与否、主人是否在场、有没有陌生人，生命模块一概不知。

因此：**"加个摄像头" = 把现有感知接进 LifeEngine**，不引入新硬件依赖
（无摄像头时整链路以 Mock 运行，逻辑先打通）。

---

## 2. 设计

### 2.1 总览

```
摄像头（已有，VisionPipeline.capture 异步 1.5fps，无设备→Mock）
   → 新增 感知循环（life/perception.py，常驻 asyncio 任务）
   → 事件模型（owner_arrived / owner_gone / stranger / owner_expression）
   → LifeEngine 注入点（3 个新方法）
   → physio 变化 + startle() 受惊反应 + 关心列表（L2 动机层雏形）
   → 下游：念头 / 联想链素材 / 思想流上下文（下一步）
```

### 2.2 事件模型（PerceptionEvent）

复用现有 `VisionObservation` 与 `PerceptionResult`（`capabilities/perception.py`），
感知循环按帧产出**离散事件**，只推有变化的事件（防 1Hz 刷屏）：

| 事件 | 判定来源 | 说明 |
|---|---|---|
| `owner_arrived` | 认主成功且此前不在场（`owner_newly_arrived`） | 主人回来了 |
| `owner_gone` | 在场变不在场 | 主人走了 |
| `stranger` | 检测到人脸但非主人 | 陌生人出现 |
| `owner_expression` | 表情识别 + `expression_to_comfort()` | 仅 sad/fear/angry 才推（低落需关心） |

事件去重：同一状态保持超过 N 秒不重复推送（`debounce_s`，默认 30s）。

### 2.3 LifeEngine 注入点（新增方法）

```python
def observe_vision(self, event: PerceptionEvent) -> None
```

按事件分派：

| 事件 | 生理/行为影响 | 关心列表 |
|---|---|---|
| `owner_arrived` | `physio.on_user_voice()` 同档（mood+、social 重置）| 记入："他回来了" |
| `owner_gone` | 不直接动数值；social 安静积累自然加速 | 记入："又剩我一个了"（限频） |
| `stranger` | **`self.scheduler.startle()`**（激活闲置的受惊反应）| 记入："有陌生人"（防过度，冷却内不重复） |
| `owner_expression=sad/fear/angry` | `physio.observe_dialogue_mood` 不适用（那是对话情绪），仅 mood 微降 + social 微升（更想陪） | 记入："主人好像不太开心" |

### 2.4 关心列表（L2 动机层雏形，最小形态）

新增在 `LifeEngine` 上，本阶段只做**数据载体**，不改念头生成逻辑：

```python
# life/engine.py
self.concerns: deque[Concern] = deque(maxlen=8)   # Concern{text, source, t}
```

- 感知事件写入；`_maybe_think()` 仍按现有概率生成（本阶段不改决策），
  但联想链 `_run_chain` 的上文窗口追加最近 1 条 concern 文本（低成本尝到"有感知的联想"）。
- 完整的"动机层改造"（评估关心选最在意）留减配 G1，本阶段不越界。

### 2.5 并发与生命周期

- 感知循环为独立 `asyncio.Task`（`life-perception`），与 physio/thought/scheduler 并列；
- `engine.start()` 时若注入的 vision 非 None 且 `life.perception.enabled` 则启动；
- `engine.stop()` 一并取消；
- 单帧异常只记日志（沿用现有"生命不能静默停止"原则）。

---

## 3. 配置（configs/default.yaml）

`life:` 段新增子段（总开关仍是 `vision.enabled`）：

```yaml
life:
  # ……（现有配置不变）
  perception:
    enabled: true            # 感知循环总开关（false 则退回现状：仅对话时感知）
    fps: 1.0                 # 常驻采样帧率（比对话时的 1.5fps 更省，可调）
    debounce_s: 30.0         # 同一事件状态去重窗口
    stranger_cooldown_s: 300 # 陌生人受惊冷却（防频繁吓到）
```

隐私沿用 `vision.privacy`（`save_frames: false` 默认不落盘原始帧；
`save_observations: true` 只存结构化观测、保留 7 天），本阶段不新增存储。

---

## 4. 实施步骤（任务清单）

1. **新建 `src/pipi/life/perception.py`**：`PerceptionLoop` 类
   - `__init__(vision, clock, cfg, on_event)`；`start()/stop()`
   - 循环：`capture()` → `PerceptionService.fuse()` → 事件判定（到场/离场/陌生人/表情）→ 去重 → `on_event(PerceptionEvent)`
   - 事件结构：`PerceptionEvent(t, kind, text, extra)` dataclass
2. **`LifeEngine` 扩展**（`src/pipi/life/engine.py`）
   - `__init__` 增加 `vision: Any | None = None` 参数 + `self.concerns`
   - 新增 `on_owner_arrived / on_owner_gone / on_stranger / on_owner_expression`
     内部统一走 `observe_vision(event)`
   - `start()/stop()` 管理感知任务
   - `_run_chain` 上文追加最近 concern（一行改动）
3. **装配**（`src/pipi/interfaces/cli.py`）：创建 LifeEngine 时传入 `self.app.vision`
4. **配置**：`life.perception` 段（上文 §3）
5. **测试**：新建 `tests/integration/test_perception.py`
   - MockVision 注入 owner_arrived / owner_gone / stranger / sad 表情
   - 断言：physio.mood/social 变化正确、`startle()` 被调用（打桩）、
     concerns 更新、去重窗口生效、无摄像头（None vision）时整链路可用
6. **验证与提交**：py_compile + 回归（reminders/growth/life 全过）+ git 提交

---

## 5. 验收标准

- [ ] Mock 模式：四种事件注入后，生理数值变化与 §2.3 表格一致
- [ ] 陌生人事件触发 `startle()`（打桩断言，非真机也验证）
- [ ] 同一状态 30s 内不重复推事件（去重生效）
- [ ] `vision=None`（无摄像头）时启动不报错，整链路可用
- [ ] `save_frames: false` 时无原始帧落盘
- [ ] 回归：`test_perception.py`（新增）+ `test_life_module.py` 28 项 + `test_proactive_vision.py` 48 项全过

---

## 6. 边界与后续衔接

**本阶段不做**（避免越界）：

- 场景理解（客厅/书桌/充电桩识别）——留 G3 环境记忆
- 动机层改造（评估关心选最在意）——留减配 G1
- 思想流 LLM 循环——留 G1/全量版

**衔接**：感知事件 → 关心列表，正是「自主思考五层方案」L1 自我模型
与 L2 动机层的输入源；感知底座到位后，减配 G1（动机驱动念头）才有米下锅。

---

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 常驻 1Hz 增加 CPU/电池占用 | fps 可配（默认 1.0）；无摄像头不启动循环；统一 tick 留 P2 |
| 陌生人受惊过频打扰 | `stranger_cooldown_s` 300s + 去重窗口 |
| 隐私（主人表情/在场信息被记录） | 复用 `save_frames: false`，只存结构化观测、本地 sqlite、保留 7 天 |
| 感知错误（认错人/表情误判） | 事件只影响 physio 与关心列表，不直接驱动发声；误判成本低 |

---

## 8. 评审修订（2026-10-08，实施前补）

**① 事件消费竞争（关键）**：`capture()` 的 `owner_newly_arrived` 是状态沿，
pipeline 单实例可能被多处调用。裁定：**感知循环是 capture() 的唯一消费者**，
到场/离场由循环内部对比前后帧判定（不依赖 pipeline 的沿标志）；
`loop.latest` 暴露最近一帧观测，对话注入视觉从这读，不再各自拍帧。
遗留 `web.py` 是独立进程（相机设备无法双开），无竞争，不改。（注：该模块后续已移除）

**② 与 ProactiveEngine 打招呼撞车**：`owner_arrived` 事件在 life 侧
**只记 concern + 调生理，不产生发声念头**——「你回来啦」的发声权归 proactive；
本进程内 proactive 目前无视觉驱动，无实际撞车，但边界就此固化。

**③ 表情事件压 mood**：主人持续伤心时 debounce 30s 会周期重推 → mood 被钉死。
裁定：表情事件只在**状态转变**（neutral→sad 等）时推一次，持续 sad 不重推；
debounce 只管到场/离场/陌生人。

**④ 验收测试名修正**：`test_reminders.py` 不存在（提醒当时是临时脚本验证）。
验收改为：`test_perception.py`（新增）+ `test_life_module.py` +
`test_proactive_vision.py` 全过即可。

**小改进**：
- `observe_vision` 各分支写 `self._log("vision", ...)` ——「看见主人回来」
  进监控页心理记录（感知接入最直观的可视化）
- `owner_arrived` 用新增的 `physio.on_owner_present()`（mood+/social−），
  不复用 `on_user_voice()`（避免语义过载，安静计时器与对话解耦）
