"""窗口端 WebSocket 推送中枢（部署架构：只跑窗口进程，Web 为纯静态页）。

架构：
  窗口进程（voice CLI）内嵌本 WS 服务 → 广播生命快照/心理事件/聊天记录；
  web/monitor.html 是纯静态页面，双击直接打开（file://），
  订阅 ws://<host>:<port>/ws 即可观测，无需任何后端。

协议（服务端 → 客户端）：
  {"type":"backfill","log":[...],"chat":[...],"snapshot":{...}}   连上即补发
  {"type":"snapshot","data":{...}}                                1Hz 快照
  {"type":"event","data":{t,kind,text}}                           心理/行为事件
  {"type":"chat","data":{t,role,text}}                            聊天记录
  {"type":"busy","data":{"reason":...}}                           拒绝了远程消息

协议（客户端 → 服务端）：
  {"type":"chat","text":"..."}   远程文本聊天（与语音共享同一份记忆与上下文）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class WSHub:
    """广播型 WebSocket 服务：推送 + 可选远程聊天入口。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8848,
                 on_chat: Callable[[str], Awaitable[None]] | None = None):
        self.host = host
        self.port = port
        self.on_chat = on_chat
        self._clients: set[Any] = set()
        self._server: Any = None
        self._recent_log: deque = deque(maxlen=300)   # 事件补发缓存
        self._recent_chat: deque = deque(maxlen=100)  # 聊天补发缓存
        self._last_snapshot: dict | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        import websockets

        self._server = await websockets.serve(
            self._handler, self.host, self.port,
            max_size=1 << 20, ping_interval=20,
        )
        logger.info("WS 推送服务就绪 ws://%s:%s/ws", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ------------------------------------------------------------------
    # 发布（由生命引擎/对话侧调用，非阻塞）
    # ------------------------------------------------------------------

    def publish(self, payload: dict) -> None:
        """接收生命引擎的批量上报：events/chat/snapshot → 缓存 + 广播。"""
        events = payload.get("events") or []
        chat = payload.get("chat") or []
        snap = payload.get("snapshot")
        self._recent_log.extend(events)
        self._recent_chat.extend(chat)
        if snap:
            self._last_snapshot = snap
        for e in events:
            self.broadcast({"type": "event", "data": e})
        for c in chat:
            self.broadcast({"type": "chat", "data": c})
        if snap:
            self.broadcast({"type": "snapshot", "data": snap})

    _SEND_TIMEOUT_S = 2.0   # 单次发送超时：超时视为客户端僵死，直接剔除

    def broadcast(self, payload: dict) -> None:
        """向所有在线客户端推送。

        背压策略：上一帧还没发完的客户端本轮直接跳过（丢帧不排队）；
        发送超时的客户端判定僵死，强制剔除——保证慢/死客户端
        不会让发送任务堆积（对应审计 P0）。
        """
        if not self._clients:
            return
        text = json.dumps(payload, ensure_ascii=False)
        for ws in list(self._clients):
            if getattr(ws, "_hub_sending", False):
                continue   # 上一帧仍在发送：丢帧，不排队
            ws._hub_sending = True
            asyncio.create_task(self._safe_send(ws, text))

    @staticmethod
    async def _safe_send(ws: Any, text: str) -> None:
        try:
            await asyncio.wait_for(ws.send(text), timeout=WSHub._SEND_TIMEOUT_S)
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass
        finally:
            ws._hub_sending = False

    # ------------------------------------------------------------------
    # 客户端连接
    # ------------------------------------------------------------------

    async def _handler(self, ws: Any) -> None:
        path = getattr(ws.request, "path", "/ws") if hasattr(ws, "request") \
            else getattr(ws, "path", "/ws")
        if not str(path).startswith("/ws"):
            await ws.close(code=1008)
            return

        self._clients.add(ws)
        logger.info("监控页接入（当前 %d 个客户端）", len(self._clients))
        try:
            # 补发：最近事件/聊天 + 当前快照
            await ws.send(json.dumps({
                "type": "backfill",
                "log": list(self._recent_log)[-80:],
                "chat": list(self._recent_chat)[-60:],
                "snapshot": self._last_snapshot,
            }, ensure_ascii=False))

            async for raw in ws:
                await self._on_message(ws, raw)
        except Exception:
            pass   # 客户端断开
        finally:
            self._clients.discard(ws)
            logger.info("监控页断开（剩余 %d 个客户端）", len(self._clients))

    async def _on_message(self, ws: Any, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if msg.get("type") == "chat" and self.on_chat is not None:
            text = (msg.get("text") or "").strip()
            if text:
                await self.on_chat(text)
