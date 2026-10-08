#!/usr/bin/env python3
"""皮皮启动脚本。

用法：
  python3 run.py                 # 文本对话（自动探测可用的模型/硬件）
  python3 run.py --sim           # 全模拟模式（零依赖，必定能跑）
  python3 run.py --voice         # 免提模式：唤醒词 + VAD 切句 + 语音播报（含计时调试）

部署形态（Docker 友好）：
  只跑窗口进程 —— 它内嵌 WebSocket 推送（默认 0.0.0.0:8848），
  监控/聊天用纯静态页面 web/monitor.html，双击打开即可，无需任何 Web 服务。

  python3 run.py --verbose       # 显示延迟与调试信息
  python3 run.py --user alice    # 指定用户 ID
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from pipi.infra import Config, setup_logging


def main():
    ap = argparse.ArgumentParser(description="皮皮 · 情感陪伴机器人")
    ap.add_argument("--sim", action="store_true",
                    help="全模拟模式（无模型/无硬件也能跑）")
    ap.add_argument("--voice", action="store_true",
                    help="免提模式：唤醒词唤醒 + 语音播报回复（含计时调试输出，需麦克风与 TTS）")
    ap.add_argument("--user", default="owner", help="用户 ID")
    ap.add_argument("--log-level", default="WARNING", help="日志级别")
    ap.add_argument("--proactive", action="store_true",
                    help="启动时触发一次主动搭话")
    ap.add_argument("--config", default="", help="额外配置文件路径")
    args = ap.parse_args()

    setup_logging(args.log_level, short=not args.voice)

    cfg = Config.default()
    if args.config:
        cfg = Config.load(
            Path(__file__).parent / "configs" / "default.yaml",
            Path(__file__).parent / "configs" / "degradation.yaml",
            args.config,
        )
    if args.sim:
        # 模拟模式强制关闭需要硬件的部分
        cfg.set("vision.enabled", False)
        cfg.set("audio.enabled", False)
        cfg.set("motion.enabled", False)

    from pipi.interfaces.cli import run_cli
    asyncio.run(run_cli(sim=args.sim, user_id=args.user,
                        verbose=args.voice and not args.sim, cfg=cfg,
                        voice=args.voice and not args.sim))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n皮皮 > 那你走吧……哼")
