"""结构化日志。"""

from __future__ import annotations

import logging
import sys


class _ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[41m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool = True, short: bool = False):
        fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s" if not short \
            else "%(levelname)-7s %(message)s"
        super().__init__(fmt, datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if self.use_color and record.levelno in self.COLORS:
            return f"{self.COLORS[record.levelno]}{msg}{self.RESET}"
        return msg


def setup_logging(level: str = "INFO", short: bool = False,
                  use_color: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter(use_color=use_color, short=short))
    root.addHandler(handler)
    # 第三方库降噪
    for noisy in ("urllib3", "asyncio", "PIL", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
