"""模拟硬件驱动 —— 无实体设备时使用。"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class OpenCVCamera:
    """真实摄像头（也兼容模拟：无设备时 is_available 返回 False）。"""

    name = "opencv_camera"

    def __init__(self, index: int = 0, width: int = 640, height: int = 480):
        self.index = index
        self.width = width
        self.height = height
        self._cap: Any = None
        self._alock: Any = None   # asyncio.Lock（惰性创建）：open/read/close 全串行

    def _ensure_lock(self):
        """asyncio.Lock 惰性创建（构造时可能尚无事件循环）。"""
        if self._alock is None:
            import asyncio
            self._alock = asyncio.Lock()
        return self._alock

    @staticmethod
    def _quiet_cv2(cv2_mod) -> None:
        """压掉 OpenCV 的 stderr 警告（DSHOW 探测重试时的
        "can't be used to capture by index" / C++ exception 噪音）。"""
        try:
            cv2_mod.setLogLevel(cv2_mod.LOG_LEVEL_ERROR)
        except Exception:
            pass

    _PROBE_SNIPPET = (
        "import cv2,sys\n"
        "cap = cv2.VideoCapture(int(sys.argv[1]), cv2.CAP_DSHOW)\n"
        "ok = cap.isOpened()\n"
        "cap.release()\n"
        "sys.exit(0 if ok else 1)")

    def is_available(self) -> bool:
        """子进程探测 —— 隔离 C++ 层崩溃。

        坏驱动/被占用的摄像头在 DSHOW 下可能触发 cv2 C++ abort：
        Python 的 try 捕不住，进程无 traceback 直接死（2026-10 实测）。
        探测放独立子进程，失败就永久走 mock，主进程不再碰 cv2。
        """
        try:
            import subprocess
            import sys as _sys

            r = subprocess.run(
                [_sys.executable, "-c", self._PROBE_SNIPPET, str(self.index)],
                capture_output=True, timeout=20)
            if r.returncode == 0:
                return True
            logger.warning("摄像头子进程探测失败（exit=%s）—— 视觉走 mock",
                           r.returncode)
            return False
        except Exception as exc:
            logger.warning("摄像头子进程探测异常：%s", exc)
            return False

    def _open_sync(self):
        """阻塞的 VideoCapture 构造（DSHOW 初始化 1~3s），只允许在工作线程跑。

        cv2 构造可能抛 C++ exception（驱动/占用），必须捕获 —— 否则会
        穿透感知循环导致主程序退出（2026-10 实测）。
        """
        import cv2
        self._quiet_cv2(cv2)
        cap = None
        for attempt in range(5):
            try:
                cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                if cap.isOpened():
                    return cap
                cap.release()
            except Exception as exc:
                logger.warning("摄像头打开异常（第 %d 次）：%s", attempt + 1, exc)
                try:
                    if cap is not None:
                        cap.release()
                except Exception:
                    pass
            # 句柄释放有延迟（上一个进程刚退出可拖到 ~10s），退避重试
            import time
            time.sleep(1.0 + attempt)
        return None

    async def open(self) -> None:
        import asyncio
        async with self._ensure_lock():
            if self._cap is not None:
                return
            self._cap = await asyncio.to_thread(self._open_sync)

    async def read(self):
        """抓一帧。cv2 全部操作经 asyncio.Lock 串行化 —— VideoCapture 的
        并发 open 会让 DSHOW 在 C++ 层 access violation（进程无 traceback
        直接死，2026-10 faulthandler 实锤：感知循环与监控推流并发 open）。
        设备不可用/被占用 → 返回 None（调用方走自愈重开），绝不抛异常。"""
        import asyncio
        async with self._ensure_lock():
            if self._cap is None:
                self._cap = await asyncio.to_thread(self._open_sync)
            if self._cap is None:
                return None
            try:
                ok, frame = await asyncio.to_thread(self._cap.read)
            except Exception:
                # 设备中途被拔/驱动崩溃 → 丢弃句柄，让自愈机制重建
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None
                return None
            return frame if ok else None

    async def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class SimCamera:
    """模拟摄像头 —— 从本地图片目录轮播，或生成纯色占位帧。"""

    name = "sim_camera"

    def __init__(self, image_dir: str = "", loop: bool = True):
        self.image_dir = image_dir
        self.loop = loop
        self._frames: list[Any] = []
        self._idx = 0

    def is_available(self) -> bool:
        return True

    async def open(self) -> None:
        import glob
        import os
        if self.image_dir and os.path.isdir(self.image_dir):
            try:
                import cv2
                paths = sorted(glob.glob(os.path.join(self.image_dir, "*")))
                self._frames = [cv2.imread(p) for p in paths]
                self._frames = [f for f in self._frames if f is not None]
            except Exception as exc:
                logger.debug("加载模拟图片失败：%s", exc)

    async def read(self):
        if self._frames:
            frame = self._frames[self._idx % len(self._frames)]
            self._idx += 1
            return frame
        return self._synthetic()

    @staticmethod
    def _synthetic():
        try:
            import numpy as np
            return np.full((480, 640, 3), 200, dtype=np.uint8)
        except ImportError:
            return None

    async def close(self) -> None:
        self._frames.clear()


class SimMic:
    """模拟麦克风 —— 无输入时返回静音。"""

    name = "sim_mic"

    def is_available(self) -> bool:
        return True

    async def start(self) -> None:
        pass

    async def read_chunk(self) -> bytes:
        import asyncio
        await asyncio.sleep(0.03)
        return b"\x00" * 960   # 30ms @ 16kHz 16bit

    async def stop(self) -> None:
        pass


class SimSpeaker:
    """模拟扬声器 —— 只记录播放内容。"""

    name = "sim_speaker"

    def __init__(self):
        self.played: list[bytes] = []

    def is_available(self) -> bool:
        return True

    async def play(self, audio: bytes, sample_rate: int) -> None:
        self.played.append(audio)

    async def stop(self) -> None:
        pass


class SimMCUBridge:
    """模拟下位机桥 —— 打印指令而非真的发话题。"""

    name = "sim_mcu"

    def __init__(self, log: bool = True):
        self.log = log
        self.history: list[dict] = []
        self._state = {
            "head_pan": 0.0, "head_tilt": 0.0,
            "base_forward": 0.0, "base_rotate": 0.0,
        }

    def is_available(self) -> bool:
        return True

    async def send(self, payload: dict) -> dict:
        self.history.append(payload)
        axis = payload.get("axis")
        if axis and axis in self._state:
            self._state[axis] = payload.get("value", 0.0)
        if self.log:
            logger.debug("[MCU] %s", payload)
        return {"ack": payload.get("cmd", ""), "ok": True, "state": dict(self._state)}

    async def query_state(self) -> dict:
        return dict(self._state)

    async def close(self) -> None:
        pass
