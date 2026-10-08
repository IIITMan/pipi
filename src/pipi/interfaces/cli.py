"""CLI 交互入口 —— 文字对话主界面。

支持：
  - 普通对话
  - /status   查看情绪状态
  - /memory   查看记忆
  - /decay    手动触发衰减巡检
  - /export   导出人格
  - /proactive 手动触发主动搭话
  - /reset    清空记忆（隐私）
  - /quit     退出
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from pipi.app import App, build_app
from pipi.infra import Config

logger = logging.getLogger(__name__)

BANNER = r"""
   ╭──────────────────────────────────╮
   │            皮  皮                │
   │      情感陪伴机器人 v0.1          │
   ╰──────────────────────────────────╯
"""

HELP = """
  /status    情绪与好感度
  /memory    查看记住的东西
  /decay     手动跑一次遗忘巡检
  /growth    查看成长档案（阶段/里程碑/偏好）
  /proactive 让皮皮主动说句话
  /reset     清空记忆（不可恢复）
  /forget N  删除第 N 条记忆（先用 /memory 看编号）
  /pin N     钉住/取消钉住第 N 条记忆（钉住=永不遗忘）
  /reload    热重载人格与配置
  /privacy   查看/切换隐私开关
  /export    导出人格档案
  /help      显示帮助
  /quit      退出
"""


class CLI:
    """命令行界面。"""

    def __init__(self, app: App, user_id: str = "owner", verbose: bool = False,
                 mic: bool = False, voice: bool = False):
        self.app = app
        self.user_id = user_id
        self.orch = app.orchestrator
        self.verbose = verbose
        self.voice_mode = False
        # 语音输入：需要 ASR 引擎 + 麦克风都可用，任一缺失自动退回键盘
        self.mic: Any | None = None
        self._vloop: Any | None = None
        self._session: Any | None = None
        self._speaker: Any | None = None
        self._hub: Any | None = None
        self._chat_lock: asyncio.Lock | None = None
        if (mic or voice) and app.asr is not None:
            try:
                from pipi.providers.asr.mic import MicCapture

                cap = MicCapture(
                    sample_rate=app.cfg.get("audio.mic.sample_rate", 16000),
                    silence_rms=app.cfg.get("audio.mic.silence_rms", "auto"),
                    noise_multiplier=app.cfg.get("audio.mic.noise_multiplier", 2.2),
                    min_silence_rms=app.cfg.get("audio.mic.min_silence_rms", 120.0),
                    gain=app.cfg.get("audio.mic.gain", 1.0),
                    pre_roll_s=app.cfg.get("audio.mic.pre_roll_s", 0.6),
                    silence_tail=app.cfg.get("audio.mic.silence_tail", 1.2),
                    max_seconds=app.cfg.get("audio.mic.max_seconds", 15),
                )
                if cap.is_available():
                    self.mic = cap
                    self.voice_mode = voice and app.tts is not None
                    if voice and app.tts is None:
                        print("  （语音输出不可用，退回按回车说话模式）")
                else:
                    print(f"  （语音输入不可用：{cap._error}，退回键盘输入）")
            except Exception as exc:
                print(f"  （语音输入初始化失败：{exc}，退回键盘输入）")

    async def run(self) -> None:
        if self.voice_mode:
            await self.run_voice()
            return
        print(BANNER)
        print(f"  {self.app.health_report()}\n")
        if self.mic:
            print("  🎤 语音模式：按回车开始说话（说完自动识别），打字可输入命令\n")
        print("  输入 /help 看命令，直接说话开始聊天\n")

        # 首次运行：领养仪式
        profile = self.orch.get_profile(self.user_id)
        if profile.is_new() and self.app.cfg.get("user.adoption_on_first_run", True):
            print("皮皮 > 诶，你是谁啊？\n")

        # 提醒/待办（阶段 F）：文本模式到点打印
        self._reminders = getattr(self.app, "reminders", None)
        if self._reminders is not None:
            def _notice(kind: str, text: str) -> None:
                if kind == "fired":
                    print(f"\n⏰ 皮皮提醒：{text}")
                elif kind == "completed":
                    print(f"  （提醒完成：{text}）")
            self._reminders.set_sink(_notice)
            self._reminders.start()
            print("  ⏰ 提醒/待办已就绪（直接说「提醒我X点…」「记个待办」「有什么提醒」）")

        while True:
            try:
                text = await asyncio.to_thread(input, "你   > ")
            except (EOFError, KeyboardInterrupt):
                print("\n皮皮 > 那你走吧……哼")
                break

            text = text.strip()
            if not text:
                if self.mic:
                    voice_text = await self._listen()
                    if voice_text:
                        await self._say(voice_text)
                continue

            if text.startswith("/"):
                if await self._handle_command(text):
                    break
                continue

            await self._say(text)

    async def run_voice(self) -> None:
        """四阶段免提交互：待机KWS → VAD切句聆听 → 流式应答+打断 → 超时回待机。"""
        from pipi.interfaces.session import InteractionSession

        print(BANNER)
        print(f"  {self.app.health_report()}\n")
        words = " / ".join(self.app.cfg.get("audio.mic.wake_words", ["皮皮"]))
        timeout = self.app.cfg.get("audio.mic.session.timeout_s", 10)
        print(f"  🎙️ 免提模式：喊「{words}」唤醒 → 连续对话（VAD 切句，不用重复唤醒）")
        print(f"     回复时可插话打断；安静 {timeout}s 自动回待机。Ctrl+C 退出\n")

        # 环境噪声校准：保持安静 1 秒
        print("  🎚️ 正在校准环境噪声（请保持安静 1 秒）…")
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self.mic.calibrate)
            print(f"     噪声底 {self.mic.noise_floor:.0f} → "
                  f"说话阈值 {self.mic.threshold:.0f}（增益 x{self.mic.gain}）\n")
        except Exception as exc:
            print(f"     校准失败（用固定阈值继续）：{exc}\n")

        self._session = InteractionSession(
            asr=self.app.asr, capture=self.mic, tts=self.app.tts,
            on_query=self._respond_stream,
            on_interrupt=self._on_barge_in,
            cfg=self.app.cfg, verbose=self.verbose,
        )

        # 提醒/待办：绑定发声通道并启动（到点由皮皮自己说）
        self._reminders = getattr(self.app, "reminders", None)
        if self._reminders is not None:
            self._reminders.bind_speak(self._session.try_speak_spontaneous)
            self._reminders.start()
            print("  ⏰ 提醒/闹铃已就绪（说「提醒我X点…」「设闹钟…」即可）")

        # 生命模块（自发念头/生理状态）——空闲时皮皮会自言自语
        self._life = None
        if self.app.cfg.get("life.enabled", True) and self.app.asr is not None:
            import random as _random

            from pipi.life import LifeEngine

            persona = self.app.persona
            bot_name = self.app.cfg.get("persona.bot_name", "皮皮")
            self._life = LifeEngine(
                cfg=self.app.cfg,
                llm=self.app.llm,
                persona_name=bot_name,
                persona_hint=f"{bot_name}，{persona.name}人格（{persona.id}），语气傲娇但内心黏人",
                memory_getter=lambda: (
                    (lambda items: _random.choice(items).content if items else None)(
                        self.app.repos.memory.list_live(self.user_id))
                ),
                session=self._session,
            )
            # C4：联想链优先引用 story 记忆（成长故事闪回）
            def _story_getter():
                items = self.app.repos.memory.list_story(self.user_id, limit=5)
                return _random.choice(items) if items else None

            self._life.story_getter = _story_getter
            self._session.life = self._life
            self._life.start()
            if self.verbose:
                import logging as _logging
                _logging.getLogger("pipi.life").setLevel(_logging.INFO)
            print("  🫀 生命模块已启动（空闲时会自发念叨、微动作）")

        # WebSocket 推送：静态监控页直接订阅，无需启动任何 Web 服务
        self._chat_lock = asyncio.Lock()
        ws_port = self.app.cfg.get("life.ws_port", 8848)
        ws_host = self.app.cfg.get("life.ws_host", "127.0.0.1")
        try:
            from pipi.interfaces.ws_server import WSHub

            self._hub = WSHub(ws_host, ws_port, on_chat=self._on_remote_chat)
            await self._hub.start()
            if self._life is not None:
                # E1：快照并入成长面板数据（growth 引擎存在时）
                growth = getattr(self.app.orchestrator, "growth", None)
                reminders_ref = getattr(self.app, "reminders", None)

                def _sink(payload: dict, _g=growth, _r=reminders_ref) -> None:
                    snap = payload.get("snapshot")
                    if snap is not None:
                        if _g is not None:
                            try:
                                snap["growth"] = _g.snapshot(self.user_id)
                            except Exception:
                                pass
                        if _r is not None:
                            try:
                                snap["reminders"] = _r.snapshot()
                            except Exception:
                                pass
                    self._hub.publish(payload)

                self._life.set_sink(_sink)
            print(f"  📡 监控数据推送 ws://{ws_host}:{ws_port}/ws"
                  f"（双击打开 web/monitor.html 即可查看）")
        except Exception as exc:
            print(f"  （WebSocket 推送不可用：{exc}）")

        try:
            await self._session.run()
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n皮皮 > 那你走吧……哼")
        finally:
            if self._reminders is not None:
                try:
                    await self._reminders.stop()
                except Exception:
                    pass
            if self._hub is not None:
                try:
                    await self._hub.stop()
                except Exception:
                    pass
            if self._life is not None:
                await self._life.stop()

    def _on_barge_in(self) -> None:
        """打断：立即停止 TTS 播放并清空缓冲。"""
        if self._speaker is not None:
            self._speaker.abort()
            self._speaker = None

    async def _respond_stream(self, text: str) -> None:
        """应答：LLM 流式 → 逐字显示 + 分句 TTS 边生成边播放。"""
        import time as _time

        if self._chat_lock is None:
            self._chat_lock = asyncio.Lock()
        async with self._chat_lock:
            await self._respond_stream_locked(text)

    async def _respond_stream_locked(self, text: str) -> None:
        import time as _time

        t0 = _time.perf_counter()
        self.app.proactive_engine.note_user_response(self.app.clock.now())
        if self._life is not None:
            self._life.log_chat("user", text)

        speaker = None
        if self.app.tts is not None:
            from pipi.interfaces.voice import SentenceTTSPlayer
            speaker = SentenceTTSPlayer(self.app.tts)
            self._speaker = speaker

        state = {"printed": False, "ttfb_ms": 0}

        async def on_chunk(delta: str) -> None:
            if not state["printed"]:
                state["ttfb_ms"] = int((_time.perf_counter() - t0) * 1000)
                print("皮皮 > ", end="", flush=True)
                state["printed"] = True
            print(delta, end="", flush=True)
            if speaker is not None:
                await speaker.feed(delta)

        result = await self.orch.handle_turn_stream(self.user_id, text,
                                                    on_chunk=on_chunk)
        if state["printed"]:
            print(flush=True)
        else:
            for b in result.bubbles:
                print(f"皮皮 > {b.text}", flush=True)
        if self._life is not None:
            for b in result.bubbles:
                self._life.log_chat("pipi", b.text)
            if result.emotion_state is not None:
                self._life.physio.observe_dialogue_mood(
                    result.emotion_state.mood.value)

        if self.verbose:
            llm = getattr(self.app.llm, "last_latency_ms", 0)
            s = self._session
            print(f"  ⏱ 语音 {s.last_utt_s:.1f}s → ASR {s.last_asr_ms}ms"
                  f" → 首响 {state['ttfb_ms']}ms"
                  f" → 编排完成 {result.latency_ms}ms"
                  f"{' [降级]' if result.degraded else ''}", flush=True)
            print(f"     记忆召回 {len(result.memories_used)} 条（写入异步落库中…）",
                  flush=True)

        if speaker is not None:
            await speaker.finish()
            self._speaker = None
        else:
            await self.orch.speak(result.bubbles)
        if self.verbose:
            total = (_time.perf_counter() - t0) * 1000
            print(f"  ⏱ 说话→回完 {total:.0f}ms ｜ 记忆已写入 "
                  f"{len(result.memories_written)} 条", flush=True)

    async def _on_remote_chat(self, text: str) -> None:
        """远程文本聊天（静态监控页发来）：与语音共享记忆，语音优先。"""
        if self._chat_lock is None:
            self._chat_lock = asyncio.Lock()
        if self._chat_lock.locked():
            if self._hub is not None:
                self._hub.broadcast({"type": "busy",
                                     "data": {"reason": "皮皮正在语音对话中，稍后再聊"}})
            return
        async with self._chat_lock:
            if self._hub is not None:
                self._hub.broadcast({"type": "thinking", "data": {}})
            if self._life is not None:
                self._life.log_chat("user", text)
            result = await self.orch.handle_turn_stream(self.user_id, text)
            if self._life is not None:
                for b in result.bubbles:
                    self._life.log_chat("pipi", b.text)
                if result.emotion_state is not None:
                    self._life.physio.observe_dialogue_mood(
                        result.emotion_state.mood.value)

    async def _listen(self) -> str:
        import time as _time

        """录一段语音并识别，返回文本（空串表示没识别到）。"""
        loop = asyncio.get_running_loop()
        print("  🎤 请说话…（说完停顿自动结束）", flush=True)
        try:
            audio = await loop.run_in_executor(None, self.mic.record_until_silence)
        except Exception as exc:
            print(f"  ✗ 录音失败：{exc}")
            return ""
        if audio.size == 0:
            print("  ✗ 没录到声音")
            return ""
        if self.mic.level_of(audio) < self.mic.threshold:
            print(f"  ✗ 音量太小（RMS={self.mic.level_of(audio):.0f} < "
                  f"阈值 {self.mic.threshold:.0f}），可调大 audio.mic.gain")
            return ""
        try:
            _t0 = _time.perf_counter()
            t = await self.app.asr.transcribe(audio.tobytes(), self.mic.sample_rate)
            asr_ms = (_time.perf_counter() - _t0) * 1000
        except Exception as exc:
            print(f"  ✗ 识别失败：{exc}")
            return ""
        if not t.text:
            print("  ✗ 没听清，再试一次？")
            return ""
        emo = t.emotion if getattr(t, "emotion", None) else "NEUTRAL"
        print(f"  🎤 （{emo}）{t.text}")
        if self.verbose:
            print(f"  ⏱ 语音 {len(audio) / self.mic.sample_rate:.1f}s"
                  f" → ASR {asr_ms:.0f}ms")
        return t.text

    async def _say(self, text: str) -> None:
        self.app.proactive_engine.note_user_response(self.app.clock.now())
        result = await self.orch.handle_turn(self.user_id, text)

        for b in result.bubbles:
            print(f"皮皮 > {b.text}")

        if self.verbose:
            llm = getattr(self.app.llm, "last_latency_ms", 0)
            print(f"  ⏱ 编排 {result.latency_ms}ms（LLM {llm}ms）"
                  f"{' [降级]' if result.degraded else ''}"
                  f" 记忆 召回{len(result.memories_used)}/写入{len(result.memories_written)}")
        print()

        # 给异步落库一点时间
        await asyncio.sleep(0.05)

    async def _handle_command(self, cmd: str) -> bool:
        """返回 True 表示退出。"""
        name = cmd.strip().lower()

        if name in ("/quit", "/exit", "/q"):
            print("皮皮 > 那你走吧……哼")
            return True

        if name == "/help":
            print(HELP)

        elif name == "/status":
            stats = self.orch.stats(self.user_id)
            print("  ┌─ 皮皮的状态 ────────────────")
            for k, v in stats.items():
                print(f"  │ {k:16s} {v}")
            print("  └─────────────────────────────")

        elif name == "/memory":
            await self._show_memory()

        elif name == "/decay":
            rep = await self.app.memory.decay(self.user_id, self.app.clock.now())
            print(f"  {rep.summary()}")

        elif name == "/growth":
            self._show_growth()

        elif name == "/proactive":
            await self._do_proactive()

        elif name == "/export":
            data = await self.app.memory_store.export(self.user_id)
            import json
            print(f"  导出 {data['count']} 条记忆：")
            print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])

        elif name == "/reset":
            n = await self.app.memory_store.forget_all(self.user_id)
            print(f"  已清空 {n} 条记忆。皮皮不记得你了。")

        elif name.startswith("/forget"):
            await self._forget_by_index(name)

        elif name.startswith("/pin"):
            await self._pin_by_index(name)

        elif name == "/reload":
            self._reload()

        elif name == "/privacy":
            self._privacy()

        else:
            print(f"  未知命令：{cmd}")

        print()
        return False

    def _current_items(self):
        """按显著性排序的当前记忆列表（/memory 的展示顺序）。"""
        now = self.app.clock.now()
        items = self.app.repos.memory.list_live(self.user_id)
        items.sort(key=lambda m: m.salience(now), reverse=True)
        return items

    def _show_growth(self) -> None:
        """B5：成长状态（阶段/里程碑/偏好/表达统计）。"""
        import time as _time

        growth = getattr(self.app.orchestrator, "growth", None)
        if growth is None:
            print("  成长系统未启用。")
            return
        gp = growth.load(self.user_id)
        rel = growth._rel_cfg()
        stage_days = rel.get("stage_days", [0, 5, 30, 90])
        stage_ms = rel.get("stage_milestones", [0, 1, 2, 3])
        curve = growth._curve()
        print("  ┌─ 成长档案 ───────────────────")
        print(f"  │ 阶段      S{gp.stage} {gp.stage_name}"
              f"（可塑性 {gp.plasticity(curve):.2f}）")
        gap = gp.next_stage_gap(stage_days, stage_ms, self.app.clock.now())
        if gap.get("done"):
            print("  │ 已到最终阶段")
        else:
            print(f"  │ 下一阶段  S{gap['next_stage']}"
                  f"（还差 {gap['need_days']} 天 / {gap['need_milestones']} 个里程碑）")
        ms = growth.repos.growth.list_milestones(self.user_id)
        if ms:
            for m in ms:
                t = _time.strftime("%m-%d", _time.localtime(m["created_at"]))
                print(f"  │ 里程碑    [{t}] {m['code']} {m['text'][:30]}")
        else:
            print("  │ 里程碑    暂无")
        prefs = gp.preferences.get("like") or {}
        for k, v in list(prefs.items())[:6]:
            bar = "█" * int(v) + "░" * (10 - int(v))
            print(f"  │ 偏好      {k} {bar} {v:.1f}")
        expr = gp.learned_expressions
        if expr:
            top = sorted(expr.items(), key=lambda kv: -kv[1]["count"])[:5]
            print(f"  │ 学到的口头禅：" + "、".join(
                f"{k}×{v['count']}" for k, v in top))
        print(f"  │ 相处      相识 {gp.active_days()} 天 · {gp.turns()} 轮对话")
        print("  └─────────────────────────────")

    async def _forget_by_index(self, name: str) -> None:
        if not self.app.cfg.get("memory.allow_user_forget", True):
            print("  隐私设置里已禁止删除记忆。")
            return
        items = self._current_items()
        idx = _parse_index(name, len(items))
        if idx is None:
            return
        m = items[idx]
        self.app.repos.memory.soft_delete(m.id, self.app.clock.now())
        print(f"  忘掉了：{m.content[:40]}")

    async def _pin_by_index(self, name: str) -> None:
        items = self._current_items()
        idx = _parse_index(name, len(items))
        if idx is None:
            return
        m = items[idx]
        m.pinned = not m.pinned
        self.app.repos.memory.update(m)
        print(f"  {'已钉住（永不遗忘）' if m.pinned else '已取消钉住'}：{m.content[:40]}")

    def _reload(self) -> None:
        """热重载：人格文件 + 配置，无需重启。"""
        from pipi.app import load_persona
        try:
            if self.app.cfg.changed_on_disk():
                self.app.cfg.reload()
            new_persona = load_persona(self.app.cfg)
            self.app.persona = new_persona
            self.orch.persona = new_persona
            self.orch.dialogue_expert.pb.persona = new_persona
            print(f"  已重载：人格={new_persona.name}({new_persona.id})"
                  f" 温度={new_persona.temperature}")
        except Exception as exc:
            print(f"  重载失败：{exc}")

    def _privacy(self) -> None:
        cfg = self.app.cfg
        print("  ┌─ 隐私开关 ──────────────────")
        print(f"  │ 记忆可删除      {cfg.get('memory.allow_user_forget', True)}")
        print(f"  │ 记忆可导出      {cfg.get('memory.export_enabled', True)}")
        print(f"  │ 摄像头开启      {cfg.get('vision.enabled', True)}")
        print(f"  │ 保存原始画面    {cfg.get('vision.privacy.save_frames', False)}")
        print(f"  │ 保存结构化观测  {cfg.get('vision.privacy.save_observations', True)}")
        print(f"  │ 留存天数        {cfg.get('vision.privacy.observation_retention_days', 7)}")
        print("  └─────────────────────────────")
        print("  （改 configs/default.yaml 里 vision.privacy 一节即可，改完 /reload）")

    async def _show_memory(self) -> None:
        now = self.app.clock.now()
        items = self._current_items()
        if not items:
            print("  （还没有记忆）")
            return
        print(f"  ┌─ 记忆（共 {len(items)} 条）──────────────")
        for i, m in enumerate(items, 1):
            sal = m.salience(now)
            flag = "📌" if m.pinned else "  "
            hl = m.half_life_hours()
            hl_s = "永不" if hl == float("inf") else f"{hl:.0f}h"
            print(f"  │ {i:2d}. {flag}[{m.type.value:10s}] {m.content[:30]:32s}")
            print(f"  │     显著性={sal:.3f} 半衰期={hl_s} 被想起={m.access_count}次")
        print("  └─────────────────────────────")
        print("  用 /forget N 删除某条，/pin N 钉住（永不遗忘）")

    async def _do_proactive(self) -> None:
        """手动触发一次主动搭话判断。"""
        profile = self.orch.get_profile(self.user_id)
        state = self.orch.get_state(self.user_id)
        decision = self.app.proactive_engine.evaluate(
            self.user_id, profile=profile, state=state,
            last_user_message_at=state.last_interaction,
        )
        if not decision.should_fire:
            print(f"  （不主动开口：{decision.suppressed_by}）")
            return
        result = await self.orch.handle_proactive(self.user_id, decision.event)
        self.app.proactive_engine.record_fired(
            self.user_id, decision.trigger, result.text
        )
        for b in result.bubbles:
            print(f"皮皮 > {b.text}")


async def run_cli(sim: bool = False, user_id: str = "owner",
                  verbose: bool = False, cfg: Config | None = None,
                  mic: bool = False, voice: bool = False) -> None:
    """启动 CLI。"""
    app = build_app(cfg, sim=sim)
    try:
        cli = CLI(app, user_id=user_id, verbose=verbose, mic=mic, voice=voice)
        await cli.run()
    finally:
        app.close()


def _parse_index(cmd: str, total: int) -> int | None:
    """从 '/forget 3' 里解析出 0-based 下标，非法则返回 None。"""
    parts = cmd.split()
    if len(parts) < 2:
        print("  用法：/forget N 或 /pin N（N 是 /memory 里的编号）")
        return None
    try:
        n = int(parts[1])
    except ValueError:
        print("  编号得是数字")
        return None
    if not (1 <= n <= total):
        print(f"  编号超范围（1~{total}）")
        return None
    return n - 1
