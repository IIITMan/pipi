# 皮皮 · 情感陪伴机器人

> 一个有人格、会遗忘、会主动、能看见你的情感对话系统。
> 可以部署到实体机器人，也可以先在电脑上跑起来。

**参考方案**：Giftia（稀疏记忆 / 多 Agent）+ SenseVoice（语音情感）+ Qwen（对话大脑）。
`--sim` 模式下**零模型、零硬件、零网络**也能完整跑通。

**部署形态**：只跑一个窗口进程（`run.py --voice` / `--mic` / 文本）。
它内嵌 WebSocket 推送（默认 `0.0.0.0:8848`），监控/远程聊天用纯静态页
`web/monitor.html`（双击即开），**不需要任何 Web 服务**——Docker 部署
只需暴露这一个端口。

**语音链路（全 sherpa-onnx，纯 CPU，无 torch）**：

```
待机  KWS 流式扫唤醒词（zipformer 3.3M，keywords.txt 定义「皮皮/小皮」）
唤醒  SenseVoice int8（239MB）整段识别，剥离唤醒词拿正文 + 情感 + 事件
聆听  VAD 切句（silero_vad.onnx）→ SenseVoice 整段识别 → 应答
应答  LLM 流式 → 分句 TTS 边生成边播；说话可随时打断（Barge-in）
```

---

## 30 秒跑起来

```bash
cd /workspace

# ① 最小演示：不需要任何模型和密钥
python3 run.py --sim
```

```
   ╭──────────────────────────────────╮
   │            皮  皮                │
   │      情感陪伴机器人 v0.1          │
   ╰──────────────────────────────────╯
  ...
能力健康矩阵：
  asr          sensevoice×
  tts          ark_tts× → edge_tts× → mock√
  llm          doubao× → deepseek× → mock√
  vision       full√ → detect_only√ → mock√
  embedding    bge_small√ → tfidf√
  voiceprint   sherpa×
  motion       wheeltec× → mock√

皮皮 > 诶，你是谁啊？
你   > 我叫阿哲，我喜欢喝美式
皮皮 > 阿哲……这名字还行吧。哼，记住了。
```

注意：**你不需要配任何东西**。缺的部分自动走 mock，
矩阵里的 √ 就是"这条链上有东西能用"。

### ② 监控台（纯静态页，双击即开）

```bash
python3 scripts/seed_demo_data.py --clean --days 60   # 先造一段"相处 60 天"
python3 run.py --sim                                  # 窗口进程（内嵌 WS 推送）
# 然后双击打开 web/monitor.html
```

监控页有：**两只大眼睛**（形状即状态：待机圆眼眨眼 / 笑眼 / 委屈 / 哭泪 /
说活弹跳 / 困了闭眼）、三轴内在状态（心情/精力/想陪）、**正在做什么**
（巡逻/张望/打盹 + 心里想着什么）、心理事件流，以及**远程文字聊天**
（与语音共享记忆；语音对话中会收到「稍后再聊」）。

### ③ 接上真模型（Qwen）

```bash
export PIPI_LLM__API_KEY=sk-xxxxxxxx

# 用环境变量覆盖配置（前缀 PIPI_，双下划线表示层级）
python3 run.py --voice
```

不想每次 `export`？两个更省事的入口（**都进不了 git**）：

```bash
cp configs/default.yaml configs/local.yaml   # 优先级高于 default.yaml；已在 .gitignore
vim configs/local.yaml                       # 在里面填 llm.api_key 等
# 或：cp .env.example .env
```

> 注意：**不要把密钥写进 `configs/default.yaml`** —— 它会被版本追踪。
> 该文件里的 `api_key` 保持为空，程序会自动降级到 MockLLM。

支持 Qwen / DeepSeek / 豆包 / OpenAI / Moonshot，在 `llm.provider` 里切换。

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| **情感对话** | 有状态的情绪机 —— 会累积、会回落、会吃醋、会委屈 |
| **稀疏记忆** | 按艾宾浩斯曲线衰减：闲聊隔天就淡，喜好记 40 天，身份永不丢 |
| **人格设定** | 4 套预设（傲娇/话痨/粘人/毒舌），数值化驱动行为，支持热重载 |
| **主动关心** | 视觉触发（你回家了）+ 时间触发（你怎么不理我），带频率红线 |
| **视觉感知** | 人脸识别认主 + 表情 + 衣着颜色，结果翻译成自然语言注入对话 |
| **语音** | 全 sherpa-onnx 本地链：KWS 唤醒 + SenseVoice 识别（带情感）+ Silero VAD 切句；TTS 豆包流式（可插拔） |
| **肢体动作** | 10 种语义动作（点头/摇头/看向你/探身/侧身滑开…），情绪自动映射 |
| **硬件预留** | N100/N5015/RK3588 主控 + 轮趣 C30D 单板（头部舵机 + 灯带 + 麦轮底盘，micro-ROS） |

---

## 常用命令

### CLI 里

```
/status    情绪与好感度
/memory    查看记住的东西（带编号）
/decay     手动跑一次遗忘巡检
/proactive 让皮皮主动说句话
/forget N  删除第 N 条记忆
/pin N     钉住/取消钉住第 N 条（钉住 = 永不遗忘）
/reload    热重载人格与配置
/privacy   查看隐私开关
/export    导出人格档案
/reset     清空记忆（不可恢复）
/quit      退出
```

### 脚本

```bash
# 造演示数据（默认模拟相处 60 天）
python3 scripts/seed_demo_data.py --clean --days 60

# 遗忘曲线演示 —— 证明"忘记闲聊、记得喜好"的不对称性
python3 scripts/demo_forgetting_curve.py

# 会话回放 —— 验证 Prompt 改动是否真的变好
python3 scripts/replay_session.py --limit 20 --export out.json

# 下载可选的本地模型（失败不影响运行）
python3 scripts/download_models.py --list
python3 scripts/download_models.py --only asr

# 集成验收（48 项断言）
python3 tests/integration/test_proactive_vision.py

# 核心链路走查（对话/情感/记忆 端到端冒烟 + 改名一致性）
python3 scripts/audit_core.py
```

`demo_forgetting_curve.py` 的输出长这样：

```
类型 / 内容                                       初始      7天     30天      半衰期
------------------------------------------------------------------------------
  chitchat 他今天说天气不错，下楼晒了会儿太阳           0.300   0.001   0.000      18h
📌commitment 他和你约定过：我答应你，周末一定早点睡       0.900   0.900   0.900       永不
📌identity 他叫阿哲                            1.000   1.000   1.000       永不
  preference 他喜欢喝美式                        0.750   0.671   0.466    1047h
```

30 天后的巡检结果：闲聊和过期事件被淘汰，身份 / 约定 / 喜好全部留下。

### 启动参数

```bash
python3 run.py                 # 文本对话（自动探测可用的模型/硬件）
python3 run.py --sim           # 全模拟（零依赖，必定能跑）
python3 run.py --voice         # 免提模式：唤醒词 + 连续对话 + 语音播报（含计时调试）
python3 run.py --user alice    # 指定用户（多用户隔离）
python3 run.py --proactive     # 启动时先让皮皮主动说一句
```

---

## 项目结构

```
src/pipi/
├── app.py            # 装配工厂（把一切接起来 + 健康矩阵）
├── domain/           # 领域模型（零依赖的纯算法层）
├── infra/            # 配置 / 时钟 / 事件总线 / 数据库
├── providers/        # 各能力的实现（asr/tts/vision/llm/motion/memory）
├── capabilities/     # 跨 Provider 的业务能力（记忆/情绪/表达/感知）
├── agents/           # 三 Agent 分工 + 能力路由器
├── orchestration/    # 主编排 + 主动/成长/回充引擎
├── pulse/            # PULSE 心搏：驱力/意愿/羁绊/日记（8→4 精简，见 docs/PULSE_DESIGN.md）
├── interfaces/       # CLI（窗口端，内嵌 WS 推送）
└── prompts/          # 人格与任务模板
web/monitor.html      # 纯静态监控页（WS 订阅，双击即开）
```

详见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 安装可选依赖

核心依赖是**空的** —— 不装任何东西也能 `--sim` 跑。
按需要加：

```bash
# 接真模型需要 YAML 解析
pip install -e ".[llm]"

# 语音链路（KWS/VAD/ASR 全 sherpa-onnx；模型约 280MB 放 data/models/）
pip install sherpa-onnx sounddevice soundfile

# 监控推送（--voice 模式内嵌 WS 服务，web/monitor.html 订阅用）
pip install -e ".[monitor]"

# 语音合成（豆包流式走 HTTP，无额外依赖；备选 edge-tts）
pip install -e ".[tts-cloud]"

# 视觉（人脸检测 + 识别）
pip install -e ".[vision-lite]"     # 只检测
pip install -e ".[vision]"          # 完整（含 InsightFace 认主）

# 记忆向量检索（不装则自动用 TF-IDF）
pip install -e ".[vector]"

# 接轮趣 C30D（走 ROS2 话题，需先装 ROS2 + micro-ROS agent）
pip install -e ".[hardware]"
```

---

## 设计上的三个关键点

### 1. 为什么闲聊要快速遗忘？

因为"记得每句闲聊"恰恰是最像机器人的行为。人会自然遗忘寒暄，
只在有意义的时刻才调取记忆。

皮皮在 30 天后忘记"今天天气不错"，但在 40 天后依然记得
"他喜欢喝美式"—— 这个**不对称**就是"像人"的来源。

| 记忆类型 | 半衰期 |
|---|---|
| 身份（名字/住哪） | **永不** |
| 约定（"我答应你"） | **永不** |
| 喜好 | ≈ 40 天 |
| 事件（下周三出差） | ≈ 9 天 |
| 闲聊 | ≈ 18 小时 |

### 2. 为什么不把视觉 JSON 塞给模型？

```python
# × 模型会变得像在读传感器日志
'{"owner_present":true,"clothing":{"owner":{"color_names":["灰色"]}}}'

# √ 模型会像真的看见了你
'[你看到的] 主人刚回来了，他现在穿着灰色的衣服，看起来挺高兴'
```

同一个信息，不同的表述，决定了回复是"检测到您已归家"还是
"哟，回来啦？今天穿这件还挺好看"。

### 3. 为什么好感度不会掉到 0？

初版实现里，60 天不联系会让好感度从 500 掉到 0（陌生）。
这不对 —— 认过主的人不该因为出差一个月就变成陌生人。

修正后：
```python
affection -= 3.0 * sqrt(hours)      # sqrt 压缩：第一天掉得最狠，之后钝化
affection = max(300, affection)     # 关系底价：认主的人不会归零
```

---

## 硬件部署（预留）

```
┌─ N100 / N5015 / RK3588 主控（皮皮主程序 + ROS2）────────────┐
│   pub /cmd_vel（Twist，底盘）· pub /pipi/head（Float32MultiArray，头部+灯）│
│   sub /odom（里程计）                                        │
│                        │ USB 921600                         │
│                        ▼                                     │
│              轮趣 C30D（micro-ROS 固件，单板下位机）           │
│                  ┌──────┼──────┬─────────┐                   │
│                  ▼      ▼      ▼         ▼                   │
│              50Hz PWM  50Hz PWM  WS2812   4× PWM + 4× 编码器  │
│                  │      │      │         │                   │
│              头部左右  头部俯仰  表情灯   4× 麦克纳姆轮        │
└──────────────────────────────────────────────────────────────┘
```

一块下位机管全机：C30D 的定时器通道同时输出两路 50Hz 舵机 PWM、
一路 WS2812 时序，并驱动 4 路编码器电机。**不再需要 ESP32**
（少一条 UART 线、少一套要单独烧的固件）。

Linux 用户态的调度抖动会毁掉机械动作，所以运动控制全部下沉：
- **头部（点头/摇头/看向你）+ 表情灯** → 皮皮发 `/pipi/head` 角度话题
- **麦克纳姆轮底盘** → 皮皮发 `/cmd_vel` 速度话题
  —— 麦轮带来独占自由度 `BASE_STRAFE`（横移）和动作 `SIDE_STEP`（傲娇地侧身滑开）

`providers/motion/wheeltec.py` 已实现全部六轴，接上真机改配置即可：

```yaml
motion:
  enabled: true
  provider: wheeltec           # mock | wheeltec
  wheeltec:
    topic_cmd: "/cmd_vel"      # 底盘速度（Twist）
    topic_head: "/pipi/head"   # 头部角度 + 灯（Float32MultiArray）
    topic_odom: "/odom"        # 里程计回传
```

**完整的硬件选型与组装方案见 [`docs/HARDWARE.md`](docs/HARDWARE.md)** ——
含淘宝实价 BOM、三档预算（¥565 / ¥1688 / ¥3027）、
ROS2+micro-ROS 部署步骤、C30D 固件要点、引脚映射表与避坑清单。

**移动版底盘的完整设计见 [`docs/CHASSIS.md`](docs/CHASSIS.md)** ——
真·十字形布局推导、麦轮 L/R 布置、**电池选型对照表（18650 / 21700 / 26650）**、
重心分层与稳定性核算、**自动回充与充电桩（含无线充电可行性对比）**。
（OpenSCAD 参数化模型与 `.stl` / `.dxf` 导出件已从仓库移除，需要时从 git 历史取回
`git checkout 4d21d2e -- hardware/`，或按 CHASSIS.md §导出命令自行生成。）

**对话模型选型见 [`docs/LLM_SELECTION.md`](docs/LLM_SELECTION.md)** ——
从 0.5B 到旗舰的五个档位、**各模型闲聊成本实算**、三档模型分工配置、
人格稳定性写法（比选模型更重要）；含降级链 `qwen → deepseek → mock`（纯云端、跨厂商灾备）的设计理由。

---

## 验证状态

```
tests/integration/test_proactive_vision.py  ←  48/48 通过

  √ 视觉→语言翻译（无 JSON 泄漏）
  √ 回家打招呼闭环（视觉→主动引擎→生成）
  √ 冷却期不重复开口
  √ 每日上限 / 夜间静默 / 被忽视降频 / 回应后恢复
  √ 虚拟时钟快进：idle / morning / night_sleep / 重要日子 / 情绪跟进
  √ 优先级：回家(90) 压过 闲置(50)
  √ MockMotion 语义动作 + 轮趣 C30D 单板（头部 / 灯 / 麦轮六轴）

tests/integration/test_dock_controller.py  ←  32/32 通过

  √ 回充触发（低电 / 闲置 / 到点；充电中不重复触发）
  √ 低电提醒话术（先说一声再走，不默默消失）
  √ 三段式对齐 5 拍内收敛（vx 缩距 / vy 调心偏 / wz 调角偏）
  √ vy 与 wz 正交性（纯旋转不动位置，纯平移不动航向）
  √ 未对正时禁止前进（防歪着撞桩）
  √ 超时 → 冷却重试 → 超次数 FAIL
  √ 有人靠近(<0.8m)立即暂停让路
  √ 触点闭合无电流 10s → FAULT；有电流 3s → CHARGING
  √ 远程视觉段 → 近程红外段切换；视觉丢失退回 SEEK

scripts/audit_core.py  ←  核心链路走查（对话/情感/记忆/改名一致性）全部通过
```

---

## 隐私

- 原始画面 **绝不落盘**（`save_frames: false`）
- 原始音频 **不持久化**
- 只保留结构化、可读、可删的结论（"他穿着灰色的衣服"）
- 用户可删除任意记忆（`/forget N`）、钉住重要记忆（`/pin N`）、一键清空（`/reset`）
- 摄像头可整体关闭（`vision.enabled: false`）

---

## License

MIT

---

## 文档索引

| 文件 | 内容 |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 软件架构、模块划分、数据流 |
| [`docs/PULSE_DESIGN.md`](docs/PULSE_DESIGN.md) | PULSE 心搏设计（8→4 精简后现行状态） |
| [`docs/PULSE_RUNTIME_INTEGRATION.md`](docs/PULSE_RUNTIME_INTEGRATION.md) | PULSE 真实系统接入说明（配置/事件映射/监控） |
| [`docs/PULSE_VALIDATION_REPORT.md`](docs/PULSE_VALIDATION_REPORT.md) | PULSE P0–P4 验证总报告 |
| [`docs/ENGINE_CONTRACTS.md`](docs/ENGINE_CONTRACTS.md) | 状态契约：唯一权威写入者 / 事件契约 / 日记与记忆分层（阶段 1 已落地） |
| [`docs/VALIDATION_MATRIX.md`](docs/VALIDATION_MATRIX.md) | 验证证据总矩阵：全部验收脚本/断言数/结果/关联提交 |
| [`docs/GROWTH_DESIGN.md`](docs/GROWTH_DESIGN.md) | 成长系统设计（关系阶段/里程碑/偏好演化） |
| [`docs/HARDWARE.md`](docs/HARDWARE.md) | 硬件选型、BOM、三档预算方案 |
| [`docs/CHASSIS.md`](docs/CHASSIS.md) | 麦轮底盘机械设计（含自动回充与充电桩） |
| [`docs/LLM_SELECTION.md`](docs/LLM_SELECTION.md) | 对话模型选型 + 语音链路架构 |
| [`docs/CHASSIS_REPORT.html`](docs/CHASSIS_REPORT.html) | 底盘可视化报告（浏览器直接打开） |
| [`docs/皮皮人格Prompt方案.md`](docs/皮皮人格Prompt方案.md) | 人格 Prompt 设计与五段式规范 |

---

## 验证状态汇总

| 项目 | 结果 |
|---|---|
| 主动搭话 + 视觉集成测试 | **48/48 通过** |
| 自动回充控制器测试 | **32/32 通过** |
| 核心链路走查 | **全部通过** |
| 配置加载冒烟（全链路） | **通过** |
| OpenSCAD 几何自检 | **通过**（含爬电距离安全红线） |
| HTML 报告结构校验 | div 40/40、table 21/21、svg 4/4、锚点无悬空 |

