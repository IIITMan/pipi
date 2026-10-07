# 皮皮 · 成长性 + 联想链 实施任务清单

> 整合两份设计:《GROWTH_DESIGN.md v0.2》(可成长性)与《机制 1 联想链念头》。
> 目标:把皮皮升级为"可成长的数字生命体"(二白式)+ 有连续思想流(生命感)。
> 版本:1.0 · 2026-10-08 · 预估周期 7~8 周(可并行)

---

## 0. 范围与总依赖图

```
                     ┌─────────── 阶段 A · 地基(数据/配置) ───────────┐
                     │  A1 schema │ A2 配置 │ A3 GrowthProfile │ A4 认主衔接 │
                     └──────┬──────────────────────────────┬──────────┘
                            ▼                                ▼
        ┌───────────────────┴───────────┐   ┌──────────────┴──────────────┐
        │  阶段 B · 成长引擎(串行)         │   │  阶段 C · 联想链(可与 B 并行)  │
        │  B1 引擎骨架 → B2 可塑性+偏好    │   │  C1 inner_stream → C2 提示词   │
        │  → B3 表达拾取 → B4 里程碑全量   │   │  → C3 触发渲染 → C4 接 story   │
        └───────────────────┬───────────┘   └──────────────┬──────────────┘
                            ▼                                ▼
        ┌───────────────────┴───────────┐   ┌──────────────┴──────────────┐
        │  阶段 D · 表达层(依赖 B3/B4)    │   │                             │
        │  D1 场景路由 → D2 身体幽默       │   │                             │
        │  → D3 危机分支 → D4 模仿引用     │   │                             │
        └───────────────────┬───────────┘   └──────────────┬──────────────┘
                            └──────────────┬────────────────┘
                                           ▼
                   阶段 E · 成长面板 + 全量验收(依赖 B/C/D)
```

**并行策略**:阶段 B(成长引擎,`orchestration/growth.py`)与阶段 C(联想链,`life/engine.py` + prompts)**分属不同模块,可并行**;阶段 D 依赖 B 的里程碑/表达拾取,C 的 story 衔接依赖 B4。

**回归红线**:现有测试 `test_proactive_vision.py`(48/48)与 `test_dock_controller.py`(32/32)全程不得回归;每次阶段结束跑全量。

---

## 阶段 A · 地基(数据与配置)~1 周

### A1 · 数据表变更
- **目标**:成长数据有落点
- **改动**:`src/pipi/infra/db/schema.sql`
  - `users` +`relationship_stage INT DEFAULT 1`、`stage_since REAL`、`meet_date REAL`、`nickname_authorized INT DEFAULT 0`
  - 新表 `growth_milestones(id, user_id, code, text, created_at)`
  - 新表 `growth_log(id, user_id, kind, detail JSON, created_at)`
- **依赖**:无
- **验收**:`pipi.db` 重建后新表/新字段存在;旧库迁移不报错(兼容:建表语句幂等)

### A2 · 配置
- **目标**:全部成长/联想参数可配、可关
- **改动**:`configs/default.yaml`
  - `growth:` 段(relationship.enabled / plasticity 曲线表 / milestone 开关 / expression 阈值)
  - `life.chain:` 段(chain_prob=0.01 / chain_cooldown_s=90 / chain_enabled)
- **依赖**:无
- **验收**:`--sim` 冷启动加载无 KeyError;每项有默认值

### A3 · GrowthProfile 数据结构
- **目标**:成长状态有类型化载体
- **改动**:新文件 `src/pipi/domain/growth.py`(按 §2.1:stage/stage_since/meet_date/milestones/preferences/learned_expressions/stats)
- **依赖**:无
- **验收**:纯数据类,单测构造/序列化/反序列化 round-trip

### A4 · 认主衔接(M1 里程碑触发点)
- **目标**:认主成为成长第一里程碑
- **改动**:`orchestration/growth.py`(骨架)+ 认主成功路径(visual 认主回调)调用 `record_milestone("M1")`
- **依赖**:A1、A3
- **验收**:模拟认主 → `growth_milestones` 出现 M1 记录,且同步写入 commitment 记忆

---

## 阶段 B · 成长引擎 ~2 周(串行内部)

### B1 · GrowthEngine 骨架
- **目标**:引擎挂进回合结束钩子
- **改动**:新文件 `src/pipi/orchestration/growth.py`;`orchestrator._post_turn` 末尾调用 `growth.on_turn(...)`
- **依赖**:A1~A3
- **验收**:故障注入(引擎抛异常)不影响 handle_turn 结果(单向性);stats 正确累计(active_days/turns)

### B2 · 可塑性曲线 + 偏好演化
- **目标**:经历塑型偏好,受可塑性调节(§2.3)
- **改动**:`growth.py` 实现 `evolve_preferences`;`domain/persona.py` 加运行时偏好层
- **依赖**:B1
- **验收**:同一步长正向事件,S1 变化量 > S4(单调性断言);偏好有上限(基线 ±3 档);方向与情绪极性一致

### B3 · 高频表达拾取
- **目标**:模仿学习的数据源(§4.3)
- **改动**:`growth.py` `learn_expressions`(count≥3 入库);`profiles.extra.learned_expressions`
- **依赖**:B1
- **验收**:模拟 5 轮相同口头禅 → 第 3 轮后进入库;不同用户互不污染

### B4 · 里程碑全量(M1~M7)
- **目标**:七个里程碑全部可触发、可沉淀
- **改动**:`growth.py` `check_milestones`(M1 认主/M2 第一喜好/M3 主动关心被回应/M4 纪念日/M5 昵称授权/M6 100 次晚安/M7 故事闪回);`memories.meta` 支持 `story` 标签
- **依赖**:B1、B3(部分依赖)
- **验收**:逐项注入触发条件 → 里程碑落表且为 commitment/story 记忆;重复触发不重复写(幂等)

### B5 · `/growth` 命令
- **目标**:CLI 可查看成长状态
- **改动**:`interfaces/cli.py` 注册 `/growth`(阶段/里程碑/偏好/表达式统计)
- **依赖**:B1~B4
- **验收**:`--sim` 下 `/growth` 输出完整、不崩溃

---

## 阶段 C · 联想链念头 ~1 周(与 B 并行)

### C1 · inner_stream 内心时间线
- **目标**:思想流数据结构
- **改动**:`life/engine.py` +`InnerEvent`、`inner_stream`(deque maxlen=12)、`_think_event(kind, text, cause)`;`_push`/`_schedule_once`/`_speak_thought` 补写入
- **依赖**:无(独立于 B)
- **验收**:规则念头/发声/丢弃都留痕且带 cause;deque 有上限不增长

### C2 · 联想提示词
- **目标**:承接式续想的 prompt
- **改动**:新文件 `src/pipi/prompts/chain_thought.jinja`(承接上文 + 一条记忆 + 状态/触发源,≤20 字)
- **依赖**:无
- **验收**:渲染无 Jinja 错误;输出约束在 prompt 内声明

### C3 · 触发 + 渲染 + 校验回退
- **目标**:联想全链路(§触发决策流)
- **改动**:`life/engine.py` `_maybe_chain()`(同步触发:IDLE/energy>3/队列空/冷却/概率,事件注入 ×3)+ `_run_chain()`(异步:组装上下文 → LLM → 12s 超时 → 防重复 → 入队 priority=3, vocal 概率 50%)
- **依赖**:C1、C2
- **验收**(4 用例):门控完备(冷却/队列非空/犯困不触发);Mock LLM 产出入队;LLM 异常静默无噪音;前缀重叠丢弃

### C4 · 联想链接入故事(story 记忆)
- **目标**:闪回带历史脉络(与成长衔接)
- **改动**:`_run_chain` 的记忆源从"显著性最高"切为"优先 story 记忆";触发 M7 里程碑条件
- **依赖**:C3、B4
- **验收**:存在 story 记忆时联想优先引用;首次故事引用触发 M7

---

## 阶段 D · 表达层 ~2 周

### D1 · 场景路由
- **目标**:对话前置场景识别与分流(§4.1)
- **改动**:`agents/dialogue_expert.py` 前置路由(anger/crisis/complain/help 规则词典);新目录 `prompts/scenarios/`
- **依赖**:B4(解锁矩阵需要 stage)
- **验收**:命中场景走话术库;未命中回落 LLM;场景识别不误伤正常对话(回归 48/48)

### D2 · 身体幽默话术库
- **目标**:机器人身体自嘲(§4.2)
- **改动**:`prompts/persona_system.jinja` 注入身体设定;`prompts/scenarios/body_joke.jinja`(按 stage_min 解锁)
- **依赖**:D1
- **验收**:S1 不触发身体玩笑;S3+ 触发且语句符合人设

### D3 · 危机分支
- **目标**:"送走/不要我"类问题专属回应(装傻→忠诚)
- **改动**:`prompts/scenarios/crisis.jinja`(S4 解锁)
- **依赖**:D1
- **验收**:S4 命中危机分支;S1~S3 走普通 LLM 路径

### D4 · 模仿引用
- **目标**:高频表达在相关场景被皮皮引用
- **改动**:场景话术渲染时注入 `learned_expressions` 命中项(D1 路由透传)
- **依赖**:D1、B3
- **验收**:主人口头禅进入库后,相关场景回复出现引用,且有防滥用上限(每会话 ≤2 次)

---

## 阶段 E · 成长面板 + 全量验收 ~1 周

### E1 · Web 成长面板
- **目标**:成长看得见(§5)
- **改动**:`web/index.html`(静态页,双击即开)+ `interfaces/ws_server.py` 快照扩展 growth 字段:阶段进度条(还差多少天/里程碑)、里程碑时间线、可塑性数值、偏好变化、相处档案
- **依赖**:B、D 完成
- **验收**:`--web --sim` 下面板数据与 growth_log 一致;无数据时优雅降级

### E2 · 集成测试 test_growth.py
- **目标**:成长验收自动化(§8 六条 + 里程碑逐项)
- **改动**:新文件 `tests/integration/test_growth.py`
- **依赖**:B、C4、D
- **验收**:阶段推进/可塑性单调/里程碑沉淀/表达解锁/分轨隔离/单向性,全部断言通过

### E3 · 联想链测试并入
- **目标**:机制 1 的 4 用例进入测试套件
- **改动**:`tests/integration/test_life_module.py` 扩展(或并入 test_growth)
- **依赖**:C3
- **验收**:新用例全绿

### E4 · 参数调优 + 全量回归
- **目标**:成长速度符合预期(不速成、不呆滞)
- **改动**:`configs/default.yaml` 调参(可塑性曲线、chain_prob、冷却);`seed_demo_data.py` 支持 90 天成长模拟
- **依赖**:E2
- **验收**:模拟 30/90 天数据下:第 1 天不可能 S4、回落不低于 S2;现有 48/32 测试 + 新增测试全部通过

---

## 验收总纲

| 测试 | 范围 | 标准 |
|---|---|---|
| `test_proactive_vision.py` | 主动搭话 + 视觉 | 48/48 不回归 |
| `test_dock_controller.py` | 自动回充 | 32/32 不回归 |
| `test_life_module.py` | 生命模块 + 联想链 | 现有 + 新增 4 用例全绿 |
| `test_growth.py` | 成长引擎 | §8 六条 + 里程碑逐项全绿 |
| `audit_core.py` | 核心链路走查 | 通过 |

## 风险与注意

1. **场景路由误伤**:规则词典可能误识别正常对话 → D1 验收必须过回归;词典阈值保守起步
2. **联想 LLM 成本**:默认 1% 概率 + 90s 冷却,实测如过频先调参不调代码(chain_prob)
3. **可塑性数值**:0.1 版曲线是初始建议,以 E4 实测校准为准
4. **认主衔接**:A4 依赖人脸认主现有回调,声纹认主为阶段后增强(不影响本清单主线)
