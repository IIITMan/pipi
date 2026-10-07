"""Web 服务 —— FastAPI + SSE 流式对话 + 人设观测台。

【遗留模式】run.py 已不再提供 --web 入口。当前部署形态 = 只跑窗口进程
（内嵌 WebSocket 推送，见 ws_server.py），监控/聊天用纯静态页
web/monitor.html。本文件保留：记忆观测台（遗忘曲线/好感度曲线/记忆管理）
仍可经 `uvicorn pipi.interfaces.web:create_app --factory` 手动起服务使用。

设计取舍：
  - **SSE 而不是 WebSocket**：回复本质是单向流（服务端 → 浏览器），
    SSE 更简单、能自动重连、可直接走 HTTP 代理，无需额外协议开销。
  - **前端零构建**：单文件 HTML + 原生 JS，不引入 npm 工具链，
    保证"clone 下来就能跑"。
  - **观测台与对话共用一份数据**：好感度曲线、遗忘曲线、记忆列表
    直接读 SQLite，没有第二份真相来源。

启动：
    python3 run.py --web            # 默认 http://127.0.0.1:8848
    uvicorn pipi.interfaces.web:create_app --factory  # 或用 uvicorn 直接跑
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from pipi.app import App, build_app
from pipi.infra import Config
from pipi.infra.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

# 静态页面统一放在项目根 ./web 下（可直接双击打开，无需启动服务）
STATIC_DIR = PROJECT_ROOT / "web"


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------

class WebLifeHost:
    """生命模块的 Web 适配器：无语音会话时提供空闲/发声通道。

    语音 CLI 上报活跃时（remote_t 新鲜）自动转为忙——
    避免网页引擎与语音会话同时发念头。
    """

    def __init__(self):
        self.busy_fn: Callable[[], bool] = lambda: False
        self.state = None          # 无会话状态机 → 生命模块恒视为 idle

    @property
    def is_busy(self) -> bool:
        return self.busy_fn()

    def try_speak_spontaneous(self, text: str) -> bool:
        from collections import deque

        if not hasattr(self, "spoken"):
            self.spoken = deque(maxlen=50)   # 有界：长会话不无限增长
        self.spoken.append(text)
        return True


def create_app(cfg: Config | None = None, *, sim: bool = False,
               app: App | None = None) -> FastAPI:
    """创建 FastAPI 应用。"""
    pipi_app = app or build_app(cfg, sim=sim)
    if cfg is None:
        cfg = pipi_app.cfg

    server = FastAPI(title="皮皮 · 情感陪伴机器人", version="0.1.0")
    server.state.pipi = pipi_app
    server.state.chat_log: list[dict] = []          # 监控页聊天记录
    server.state.life = None
    server.state.life_host = WebLifeHost()
    server.state.remote_t: float = 0.0              # 语音 CLI 最近一次上报时间
    server.state.remote_log: list[dict] = []
    server.state.remote_snap: dict | None = None

    def _remote_fresh() -> bool:
        return time.time() - server.state.remote_t < 10.0
    server.state.remote_fresh = _remote_fresh

    # ---------------- 页面 ----------------

    @server.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @server.get("/monitor", response_class=HTMLResponse)
    async def monitor() -> HTMLResponse:
        html = (STATIC_DIR / "monitor.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    # ---------------- 对话（SSE） ----------------

    @server.post("/api/chat")
    async def chat(payload: dict[str, Any]) -> StreamingResponse:
        """流式对话。请求体：{user_id, text}。

        事件序列：
          event: meta      → 记忆命中 / 情绪状态 / trace_id
          event: bubble    → 一个气泡（逐条推送，前端逐条渲染）
          event: done      → 结束，带延迟与降级标记
        """
        user_id = payload.get("user_id") or cfg.get("user.default_user_id", "owner")
        text = (payload.get("text") or "").strip()

        async def gen() -> AsyncIterator[bytes]:
            if not text:
                yield _sse("error", {"message": "空消息"})
                return
            pipi_app.proactive_engine.note_user_response(pipi_app.clock.now())
            try:
                result = await pipi_app.orchestrator.handle_turn(user_id, text)
            except Exception as exc:  # 服务端绝不因单轮失败而挂
                logger.exception("对话失败")
                yield _sse("error", {"message": str(exc)})
                return

            # 监控页聊天记录
            log = server.state.chat_log
            log.append({"t": time.time(), "role": "user", "text": text})
            for b in result.bubbles:
                log.append({"t": time.time(), "role": "pipi", "text": b.text})
            del log[:-100]

            yield _sse("meta", {
                "memories_used": result.memories_used,
                "emotion": _emotion_payload(result.emotion_state),
                "degraded": result.degraded,
            })
            for b in result.bubbles:
                yield _sse("bubble", {"text": b.text, "emotion": b.emotion})
                await asyncio.sleep(0.35)   # 模拟"打字"节奏，逐条冒出
            yield _sse("done", {"latency_ms": result.latency_ms})
            # 给异步落库一点时间，保证刷新页面能看到最新状态
            await asyncio.sleep(0.15)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---------------- 主动搭话 ----------------

    @server.post("/api/proactive")
    async def proactive(payload: dict[str, Any]) -> JSONResponse:
        user_id = payload.get("user_id") or cfg.get("user.default_user_id", "owner")
        # 支持从 Web 手动模拟"主人回家"（没有摄像头时也能演示视觉联动）
        if payload.get("simulate_arrival"):
            from pipi.providers.vision.mock import make_arrival_observation
            vision = make_arrival_observation(
                clothing_color=payload.get("clothing", "灰色"),
                expression=payload.get("expression", "happy"),
            )
        else:
            vision = None

        profile = pipi_app.orchestrator.get_profile(user_id)
        state = pipi_app.orchestrator.get_state(user_id)
        decision = pipi_app.proactive_engine.evaluate(
            user_id, profile=profile, state=state,
            owner_present=vision is not None,
            owner_newly_arrived=vision is not None and vision.owner_newly_arrived,
            last_user_message_at=state.last_interaction,
        )
        if not decision.should_fire:
            return JSONResponse({
                "fired": False, "reason": decision.suppressed_by,
            })

        result = await pipi_app.orchestrator.handle_proactive(
            user_id, decision.event, vision=vision)
        pipi_app.proactive_engine.record_fired(
            user_id, decision.trigger, result.text)
        return JSONResponse({
            "fired": True,
            "trigger": decision.trigger,
            "bubbles": [{"text": b.text, "emotion": b.emotion}
                        for b in result.bubbles],
            "emotion": _emotion_payload(result.emotion_state),
        })

    # ---------------- 观测台 ----------------

    @server.get("/api/state")
    async def get_state(user_id: str = "") -> JSONResponse:
        uid = user_id or cfg.get("user.default_user_id", "owner")
        return JSONResponse({
            "stats": pipi_app.orchestrator.stats(uid),
            "profile": _profile_payload(pipi_app, uid),
        })

    @server.get("/api/memories")
    async def get_memories(user_id: str = "", limit: int = 200) -> JSONResponse:
        uid = user_id or cfg.get("user.default_user_id", "owner")
        now = pipi_app.clock.now()
        items = pipi_app.repos.memory.list_live(uid, limit=limit)
        items.sort(key=lambda m: m.salience(now), reverse=True)
        return JSONResponse({
            "count": len(items),
            "now": now,
            "items": [_memory_payload(m, now) for m in items],
        })

    @server.delete("/api/memories/{memory_id}")
    async def forget_memory(memory_id: str) -> JSONResponse:
        """隐私开关：用户可删除单条记忆。"""
        if not cfg.get("memory.allow_user_forget", True):
            return JSONResponse({"ok": False, "reason": "已禁用记忆删除"}, status_code=403)
        pipi_app.repos.memory.soft_delete(memory_id, pipi_app.clock.now())
        return JSONResponse({"ok": True})

    @server.post("/api/memories/{memory_id}/pin")
    async def pin_memory(memory_id: str, payload: dict[str, Any]) -> JSONResponse:
        """钉住/取消钉住 —— 钉住的记忆永不遗忘。"""
        item = pipi_app.repos.memory.get(memory_id)
        if not item:
            return JSONResponse({"ok": False, "reason": "记忆不存在"}, status_code=404)
        item.pinned = bool(payload.get("pinned", True))
        pipi_app.repos.memory.update(item)
        return JSONResponse({"ok": True, "pinned": item.pinned})

    @server.get("/api/affection_curve")
    async def affection_curve(user_id: str = "", limit: int = 500) -> JSONResponse:
        uid = user_id or cfg.get("user.default_user_id", "owner")
        hist = pipi_app.repos.emotion.history(uid, limit=limit)
        return JSONResponse({
            "points": [
                {"t": h["created_at"], "affection": h["affection"],
                 "mood": h["mood"], "intensity": h["intensity"]}
                for h in hist
            ]
        })

    @server.get("/api/decay_curve")
    async def decay_curve(user_id: str = "", limit: int = 200) -> JSONResponse:
        """遗忘曲线 —— 用当前活跃记忆反推"如果不被想起会怎么掉"。

        返回每条记忆的显著性 + 半衰期，前端画成"未来 7 天预测"。
        """
        uid = user_id or cfg.get("user.default_user_id", "owner")
        now = pipi_app.clock.now()
        items = pipi_app.repos.memory.list_live(uid, limit=limit)

        horizon_hours = 24 * 7
        curves = []
        for m in items:
            sal0 = m.salience(now)
            hl = m.half_life_hours()
            pts = []
            for h in range(0, horizon_hours + 1, 6):
                if hl == float("inf"):
                    s = sal0
                else:
                    s = sal0 * (0.5 ** (h / hl))
                pts.append({"h": h, "s": round(s, 4)})
            curves.append({
                "id": m.id, "content": m.content, "type": m.type.value,
                "pinned": m.pinned, "salience": round(sal0, 4),
                "half_life_hours": None if hl == float("inf") else round(hl, 1),
                "points": pts,
            })
        curves.sort(key=lambda c: c["salience"], reverse=True)
        return JSONResponse({"curves": curves[:20], "now": now})

    @server.get("/api/decay_reports")
    async def decay_reports(user_id: str = "") -> JSONResponse:
        uid = user_id or cfg.get("user.default_user_id", "owner")
        return JSONResponse({"reports": pipi_app.repos.decay.history(uid, limit=50)})

    @server.post("/api/decay")
    async def run_decay(payload: dict[str, Any]) -> JSONResponse:
        uid = payload.get("user_id") or cfg.get("user.default_user_id", "owner")
        rep = await pipi_app.memory.decay(uid, pipi_app.clock.now())
        pipi_app.repos.decay.save(uid, rep, pipi_app.clock.now())
        return JSONResponse({
            "checked": rep.checked, "decayed": rep.decayed,
            "forgotten": rep.forgotten, "pinned_kept": rep.pinned_kept,
            "avg_before": round(rep.avg_salience_before, 4),
            "avg_after": round(rep.avg_salience_after, 4),
            "summary": rep.summary(),
        })

    @server.get("/api/proactive_log")
    async def proactive_log(user_id: str = "", limit: int = 50) -> JSONResponse:
        uid = user_id or cfg.get("user.default_user_id", "owner")
        return JSONResponse({
            "log": pipi_app.repos.proactive.recent(uid, limit=limit),
            "today_count": pipi_app.repos.proactive.count_today(
                uid, pipi_app.proactive_engine._day_start()),
            "daily_limit": pipi_app.proactive_engine.effective_daily_limit(),
        })

    @server.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse({
            "persona": pipi_app.persona.id,
            "persona_name": pipi_app.persona.name,
            "llm": getattr(pipi_app.llm, "name", "unknown"),
            "embedder": pipi_app.memory_store.embedder.name,
            "sim_mode": pipi_app.sim_mode,
            "matrix": pipi_app.router.health_matrix(),
            "chains": pipi_app.router.chains,
        })

    @server.get("/api/life/state")
    async def life_state() -> JSONResponse:
        """监控页：生命模块快照 + 心理/行为事件日志。

        语音 CLI 在上报时（remote 新鲜），以语音会话的快照为准，
        并合并两边的事件日志。
        """
        life = server.state.life
        snap = life.snapshot() if life else {"enabled": False}
        local_log = list(life.log) if life else []

        if server.state.remote_fresh():
            remote = server.state.remote_snap or {}
            snap = {**snap, **remote, "remote": True}
            merged = sorted(local_log + server.state.remote_log,
                            key=lambda e: e.get("t", 0))
            snap["log"] = merged[-80:]
        else:
            snap["log"] = local_log[-80:]
        return JSONResponse(snap)

    @server.post("/api/life/ingest")
    async def life_ingest(payload: dict[str, Any]) -> JSONResponse:
        """语音 CLI 上报：事件日志 / 快照 / 聊天记录（追加合并）。"""
        server.state.remote_t = time.time()
        server.state.remote_log.extend(payload.get("events") or [])
        # 按时间去重排序，保留最近 300 条
        seen = set()
        merged = []
        for e in sorted(server.state.remote_log, key=lambda x: x.get("t", 0)):
            key = (e.get("t"), e.get("kind"), e.get("text"))
            if key not in seen:
                seen.add(key)
                merged.append(e)
        server.state.remote_log = merged[-300:]
        server.state.remote_snap = payload.get("snapshot")
        for c in payload.get("chat") or []:
            server.state.chat_log.append(c)
        del server.state.chat_log[:-100]
        return JSONResponse({"ok": True})

    @server.get("/api/monitor/chat")
    async def monitor_chat() -> JSONResponse:
        chat = sorted(server.state.chat_log[-60:], key=lambda m: m.get("t", 0))
        return JSONResponse({"chat": chat})

    @server.on_event("startup")
    async def _startup() -> None:
        """启动生命引擎（Web 模式：自发念头显示在监控页，不做语音）。"""
        if not cfg.get("life.enabled", True):
            return
        try:
            from pipi.life import LifeEngine

            persona = pipi_app.persona
            bot_name = cfg.get("persona.bot_name", "皮皮")
            uid = cfg.get("user.default_user_id", "owner")
            host = server.state.life_host
            host.busy_fn = lambda: server.state.remote_fresh()
            life = LifeEngine(
                cfg=cfg, llm=pipi_app.llm,
                persona_name=bot_name,
                persona_hint=f"{bot_name}，{persona.name}人格（{persona.id}），语气傲娇但内心黏人",
                memory_getter=lambda: (
                    (lambda items: random.choice(items).content if items else None)(
                        pipi_app.repos.memory.list_live(uid))
                ),
                session=host,
            )
            host.life = life
            server.state.life = life
            life.start()
        except Exception:
            logger.exception("生命引擎启动失败（不影响对话）")

    @server.on_event("shutdown")
    async def _shutdown() -> None:
        if server.state.life is not None:
            await server.state.life.stop()
        pipi_app.close()

    return server


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" \
        .encode("utf-8")


def _emotion_payload(state) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "mood": state.mood.value,
        "intensity": round(state.intensity, 2),
        "affection": round(state.affection, 1),
        "level": state.affection_level,
        "companion_need": round(state.companion_need, 2),
    }


def _profile_payload(app: App, uid: str) -> dict[str, Any]:
    p = app.orchestrator.get_profile(uid)
    return {
        "nickname": p.nickname,
        "call_name": p.call_name,
        "city": p.city,
        "living": p.living,
        "job": p.job,
        "likes": p.likes,
        "dislikes": p.dislikes,
        "rivals": p.rivals,
        "important_dates": p.important_dates,
        "is_owner": p.is_owner,
    }


def _memory_payload(m, now: float) -> dict[str, Any]:
    hl = m.half_life_hours()
    return {
        "id": m.id,
        "type": m.type.value,
        "layer": m.layer.value,
        "content": m.content,
        "importance": round(m.importance, 3),
        "salience": round(m.salience(now), 4),
        "half_life_hours": None if hl == float("inf") else round(hl, 1),
        "access_count": m.access_count,
        "pinned": m.pinned,
        "created_at": m.created_at,
        "last_access_at": m.last_access_at,
    }


def run_web(cfg: Config | None = None, *, sim: bool = False,
            host: str = "127.0.0.1", port: int = 8848) -> None:
    """启动 Web 服务（阻塞）。"""
    import uvicorn
    server = create_app(cfg, sim=sim)
    print(f"\n  皮皮 Web 界面已启动 →  http://{host}:{port}\n")
    uvicorn.run(server, host=host, port=port, log_level="warning")


__all__ = ["create_app", "run_web"]
