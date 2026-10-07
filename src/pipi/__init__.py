"""皮皮 —— 情感陪伴机器人的软件系统。

分层：
  domain          纯数据模型
  infra           配置/数据库/事件总线/时钟
  providers       能力接口与实现（ASR/TTS/视觉/LLM/记忆/运动）
  capabilities    组合能力（记忆管理/情感引擎/表达/感知）
  agents          三 Agent 分工（情感分析/记忆检索/对话生成）
  orchestration   编排（主循环/主动引擎/降级）
  interfaces      入口（CLI/Web/语音循环）
"""

__version__ = "0.1.0"
