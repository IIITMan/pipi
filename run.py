#!/usr/bin/env python3
"""皮皮启动脚本。

用法：
  python3 run.py                 # 文本对话（自动探测可用的模型/硬件）
  python3 run.py --sim           # 全模拟模式（零依赖，必定能跑）
  python3 run.py --web           # Web 界面（对话 + 人设观测台）
  python3 run.py --web --sim     # Web 界面 + 全模拟（无需任何模型/密钥）
  python3 run.py --verbose       # 显示延迟与调试信息
  python3 run.py --user alice    # 指定用户 ID
  python3 run.py --proactive     # 启动时先让皮皮主动搭话一次
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
    ap.add_argument("--mic", action="store_true",
                    help="启用麦克风语音输入（需 funasr + sounddevice，缺依赖自动退回键盘）")
    ap.add_argument("--voice", action="store_true",
                    help="免提模式：唤醒词唤醒 + 语音播报回复（含 --mic 能力，需 TTS 可用）")
    ap.add_argument("--web", action="store_true",
                    help="启动 Web 界面（对话 + 记忆/好感度观测台）")
    ap.add_argument("--host", default="127.0.0.1", help="Web 监听地址")
    ap.add_argument("--port", type=int, default=8848, help="Web 端口")
    ap.add_argument("--user", default="owner", help="用户 ID")
    ap.add_argument("--verbose", "-v", action="store_true", help="显示调试信息")
    ap.add_argument("--log-level", default="WARNING", help="日志级别")
    ap.add_argument("--proactive", action="store_true",
                    help="启动时触发一次主动搭话")
    ap.add_argument("--config", default="", help="额外配置文件路径")
    args = ap.parse_args()

    setup_logging(args.log_level, short=not args.verbose)

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

    if args.web:
        from pipi.interfaces.web import run_web
        port = args.port or cfg.get("interface.web.port", 8848)
        host = args.host or cfg.get("interface.web.host", "127.0.0.1")
        run_web(cfg, sim=args.sim, host=host, port=port)
        return

    from pipi.interfaces.cli import run_cli
    asyncio.run(run_cli(sim=args.sim, user_id=args.user,
                        verbose=args.verbose, cfg=cfg,
                        mic=(args.mic or args.voice) and not args.sim,
                        voice=args.voice and not args.sim))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n皮皮 > 那你走吧……哼")
