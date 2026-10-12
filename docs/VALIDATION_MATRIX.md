# VALIDATION_MATRIX.md — pipi 验证证据总矩阵

> 用途：PULSE_REVIEW_TODO P1 附加要求的验证证据归档——把分散在多个
> 验收脚本/文档里的断言结果汇总成一张矩阵表，作为完整验证证据。
> 状态：2026-10-13 核验，工作树 HEAD = `604d124`（契约三阶段 + tick_idle 残留修复）。

## 总矩阵

| 脚本 / 测试 | 断言数 | 覆盖内容 | 最近结果 | 关联提交 |
|---|---|---|---|---|
| `scripts/phase2_validation.py` | 32 | 控制车道（/quiet 静音门、LLM 不唤醒）、情绪护栏（idle/ignored/tick_idle 不委屈、吃醋学习开关）、降频（被忽视 3 次归零/沉默减半）、降频回调接通 | ✅ 32/32 | `69293b2`、`604d124` |
| `scripts/phase3_validation.py` | 17 | LLM 输出契约：对话回复校验、记忆抽取 schema、畸形 JSON 不落库+有界重试、分类不授权 allowlist、目的化视图截断 | ✅ 17/17 | `71f07e4` |
| `tests/integration/test_proactive_vision.py` | 48 | 主动搭话（视觉触发/频率上限/冷却/夜间静默/被忽视降频）+ 视觉联动 + MockMotion 动作（ROS2 驱动头灯麦轮） | ✅ 48/48 | 阶段 5+6 基线、`69293b2`、`604d124` |
| `scripts/pulse_runtime_validation.py` | 34（含对照） | PULSE 接入层 14 天模拟：视觉事件映射、自发发声、监控快照、跨天整理、认主对照、心脏回归 | ✅ 34/34 | `ded8cb0` 及之前 |
| `tests/integration/test_growth.py` | 14 | 成长系统：trust 唯一权威、S1-S4 只读派生、里程碑/偏好演化、growth_log 审计 | ✅ 14/14 | `ded8cb0` |
| `scripts/pulse_runtime_smoke.py` | 8 | PulseRuntime 集成冒烟（配置/发声/事件/监控） | ✅ 8/8 | `ded8cb0` |
| `tests/integration/test_reminders.py` | 22 | 提醒/闹铃/待办：意图解析、定时触发、repeat 推进、静默窗口顺延、发声重试、确认闭环、取消 | ✅ 22/22 | 本次修复（静默窗口脆弱性） |
| `tests/integration/test_dock_controller.py` | 32 | 回充控制器：状态机、传感器故障、人距避让、暂停恢复 | ✅ 32/32 | 历史基线 |
| `scripts/pulse_p0_validation.py` 等历史脚本 | — | P0–P4 器官验证、天气同步、搜索预警（历史基线，详见对应文档） | 历史通过 | 见各验证文档 |

## 行为验收（用户指定口径，非断言数）

| 验收项 | 标准 | 证据 |
|---|---|---|
| 3 次被忽视后更安静而非更委屈 | ignored_streak≥3 → 主动上限归零；mood 保持 CALM | phase2 第 3 节 + tick_idle 断言 |
| /quiet 期间 LLM 不可唤醒主动发言 | 静音门在发声出口 + PULSE 三入口，渲染不执行 | phase2 第 5 节（rendered==0） |
| 畸形 JSON 不落库不执行 | 记忆抽取重试后仍空 → 空结果；对话非法 → 兜底 | phase3 第 3 节 |
| 同一会话回放两次逐字节一致 | FixtureLLM × 2 遍，除 latency_ms 外逐字节一致 | `replay_session.py --fixture`（5 轮 × 2 遍） |
| 连续 3 次被忽视不累计委屈 | 衰减通道（tick_idle 3h/48h）均保持 CALM | phase2 新增 4 条断言 |

## 运行方式

```powershell
# 契约/护栏/LLM 边界专项验收（每阶段独立）
.\.venv\Scripts\python.exe scripts\phase2_validation.py
.\.venv\Scripts\python.exe scripts\phase3_validation.py
# 全量回归
.\.venv\Scripts\python.exe tests\integration\test_proactive_vision.py
.\.venv\Scripts\python.exe tests\integration\test_growth.py
.\.venv\Scripts\python.exe scripts\pulse_runtime_validation.py
.\.venv\Scripts\python.exe scripts\pulse_runtime_smoke.py
.\.venv\Scripts\python.exe tests\integration\test_reminders.py
.\.venv\Scripts\python.exe tests\integration\test_dock_controller.py
# 确定性回放
.\.venv\Scripts\python.exe scripts\replay_session.py --fixture
```

注意：`test_reminders.py` 的 FakeClock 基准已固定在今天 12:00（避开静默窗口
[23,7]），任何时刻运行结果一致——2026-10-13 修复的测试脆弱性（原实现用
真实时刻做基准，凌晨运行时 once 提醒被静默窗口顺延导致误失败）。
