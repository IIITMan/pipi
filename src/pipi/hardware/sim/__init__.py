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

    def is_available(self) -> bool:
        try:
            import cv2
        except ImportError:
            return False
        try:
            cap = cv2.VideoCapture(self.index)
            ok = cap.isOpened()
            cap.release()
            return ok
        except Exception:
            return False

    async def open(self) -> None:
        import cv2
        self._cap = cv2.VideoCapture(self.index)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

    async def read(self):
        if self._cap is None:
            await self.open()
        ok, frame = self._cap.read()
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
