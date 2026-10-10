# 导航任务 · TODO 归档（"去主卧看看里面有没有人"）

> 状态：📋 规划完成，待实现（P0 可在当前环境直接开工）
> 设计依据：对话方案「一条指令 = 一个可取消的具身任务」
> 硬约束：不用深度/ToF（激光雷达+超声波+单目视觉）· 部署首选 RK3588 · 零新增依赖优先 · 失败静默降级 · PULSE 灵魂层不动

---

## 主线设计（一句话）

语音 → orchestrator 解析为 `NavigationTask(destination, check, report)` → 任务状态机（可取消）→ 导航层（ROS2/Nav2 + 三层安全）→ 到达后感知扫描 → 统一说话汇报 → 结果注入生命流。

```
IDLE → NAVIGATING → ARRIVED → SCANNING → REPORTING → (RETURN) → IDLE
失败：重试 1 次 → 汇报"到不了/没看清" · 全程可被语音打断取消
```

---

## P0 · 原型（当前环境即可做，无 ROS）

| # | 任务 | 关键点 / 验收 |
|---|---|---|
| 1 | 地点词表扩展 | `capabilities/environment.py` 的 `_PLACES` 补：主卧/次卧/书房/餐厅/卫生间/阳台/玄关/门口…；不在点位表的地点 → 回复"还没学过" |
| 2 | NavigationService 新建 | `capabilities/navigation.py`，与 weather/search 同构：`detect(text)` + `parse(text)` + 任务状态机 |
| 3 | 任务状态机 | IDLE/NAVIGATING/ARRIVED/SCANNING/REPORTING；每步超时与失败转移；可取消接口 |
| 4 | mock 导航 provider | 无 ROS 时模拟"已到达/扫描结果"，整链路可验证；降级链 `nav:mock` |
| 5 | orchestrator 工具链接入 | `handle_turn`/`handle_turn_stream` 加 `nav` 分支（优先级：提醒 > 天气 > 导航 > 搜索）；`self.nav` 装配 + configs `nav:` 节 |
| 6 | 到达后扫描 | 复用现有视觉 pipeline（YuNet 人脸 + InsightFace 识别）；抓 3~5 帧；结果 有人/没人/不确定 |
| 7 | 人形检测接口预留 | 新轻量人体检测模型（YOLO 系小模型 onnx，纯 CPU）作为可选能力，失败降级为仅人脸检测；本轮只留接口不接模型 |
| 8 | 汇报走统一说话 | 任务结果经 `_render_and_speak`（LLM 按内心渲染 + 句库兜底）；执行期**暂停自发发声** |
| 9 | PULSE 联动 | 任务完成/失败 → `note_event("nav_done", …)` 进生命流/日记；精力消耗联动留扩展 |
| 10 | 监控台可视化 | 任务状态进入行为栏/事件流（NAVIGATING 中显示"去主卧的路上…"）；不新增独立页 |
| 11 | 验证脚本 | `scripts/nav_task_validation.py`：意图解析/点位拒绝/成功/无人/失败重试/超时/取消，全分支覆盖 |

**P0 验收**：语音（或文本）说"去主卧看看里面有没有人"→ 监控台看到任务流转 → 收到汇报；无 ROS、无摄像头时 mock 全通。

---

## P1 · 接真实底盘（ROS2）

| # | 任务 | 关键点 / 验收 |
|---|---|---|
| 1 | 点位表 yaml | `configs/waypoints.yaml`：主卧/次卧/书房/门口…（坐标 + 朝向 + 到达半径） |
| 2 | ROS 桥接 | `action_backend: "ros2"`（配置已预留 mock\|ros2）；Nav2 goal 下发 / 状态反馈 / 取消 |
| 3 | 三层安全 | 激光雷达：2D 定位 + 路径规划（Nav2）；超声波：贴脸紧急停（距离 <阈值 急停）；单目视觉：人形动态避让 |
| 4 | 到达判定 | 距目标 <0.3m 且线速度≈0；超时/卡死检测（原地打转 N 秒 → 放弃重试） |
| 5 | 语音打断取消 | 复用现有 Barge-in；任务执行中喊停止 → 取消 goal → 回待机/返回 |
| 6 | 硬件联调 | RK3588 上验证：底盘 + 雷达 + 声呐 + 摄像头同跑；CPU 占用、时延达标 |

---

## P2 · 扩展

| # | 任务 | 关键点 |
|---|---|---|
| 1 | 多任务类型 | 全屋巡检 / 找人 / 查电器状态（如"去看看热水器烧好没"） |
| 2 | 任务记忆 | 结果进环境档案：地点活跃时段（"主卧晚上经常没人"）、上次巡检时间 |
| 3 | 精力联动 | 长任务消耗精力 → 影响 PULSE 自发行为（疲惫不想动） |
| 4 | 多点路径 | 一键多房间巡检（A→B→C→回来汇报） |

---

## 接口约定（实现时遵守）

- 新增工具一律与 weather/search/env 同构：`detect()` + 可注入 + 失败返回 None 静默降级 + configs 开关。
- 发声/汇报一律走 runtime 统一说话（`vocal_renderer` LLM 渲染 + 句库兜底），不新开通道。
- 任务执行不阻塞生命主循环；PULSE 只接收事件（`note_event`），不反向控制任务。
- 所有新依赖必须可省（缺依赖自动降级），模型文件放 `data/models/`。

---

## 关联现有资产

- 动作端口预留：`runtime.action_sink`（协议 `pulse/expression.py`）
- 配置预留：`action_backend: mock|ros2`（`configs/default.yaml`）
- 感知循环：`life/perception.py`（PerceptionLoop，1fps，事件去重）
- 视觉管线：`providers/vision/pipeline.py`（YuNet + InsightFace + 表情）
- 环境档案：`capabilities/environment.py`（地点词表在此扩展）
- 统一说话：`pulse/runtime.py::_render_and_speak`
