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
from pipi.domain.dialogue import Role
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
  /enroll    登记主人人脸（认主/回家问候的前提）
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
                 voice: bool = False):
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
        self._stream_task: Any | None = None
        self._life: Any | None = None
        self._chat_lock: asyncio.Lock | None = None
        self._vp: Any | None = None
        self._active_uid: str = user_id
        self._last_uid: str = ""
        if voice and app.asr is not None:
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
                    self.voice_mode = app.tts is not None
                    if not self.voice_mode:
                        print("  （语音输出不可用，退回键盘输入）")
                else:
                    print(f"  （语音输入不可用：{cap._error}，退回键盘输入）")
            except Exception as exc:
                print(f"  （语音输入初始化失败：{exc}，退回键盘输入）")

    async def run(self) -> None:
        # 恢复已登记人脸 —— 认主识别（owner_present/回家问候）依赖它
        rec = getattr(self.app.vision, "recognizer", None)
        if rec is not None and hasattr(rec, "load_enrolled"):
            try:
                faces = self.app.repos.face.all_faces()
                if faces:
                    rec.load_enrolled(faces)
            except Exception:
                pass
        if self.voice_mode:
            await self.run_voice()
            return
        print(BANNER)
        print(f"  {self.app.health_report()}\n")
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
                    print(f"\n皮皮提醒：{text}")
                elif kind == "completed":
                    print(f"  （提醒完成：{text}）")
            self._reminders.set_sink(_notice)
            self._reminders.start()
            print("  提醒/待办已就绪（直接说「提醒我X点…」「记个待办」「有什么提醒」）")

        while True:
            try:
                text = await asyncio.to_thread(input, "你   > ")
            except (EOFError, KeyboardInterrupt):
                print("\n皮皮 > 那你走吧……哼")
                break

            # 非 UTF-8 管道/终端可能解码出 surrogate 残留（\udcxx），
            # 不清洗会一路传到 sqlite 落库时才报错；BOM 同理会破坏命令识别
            text = text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
            text = text.lstrip("\ufeff").strip()
            if not text:
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
        print(f"  免提模式：喊「{words}」唤醒 → 连续对话（VAD 切句，不用重复唤醒）")
        print(f"     回复时可插话打断；安静 {timeout}s 自动回待机。Ctrl+C 退出\n")

        # 环境噪声校准：保持安静 1 秒
        print("  正在校准环境噪声（请保持安静 1 秒）…")
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
            on_identify=self._identify_speaker,
        )
        # 自发发声（自言自语/预警/提醒）也写入对话历史：
        # 否则下一轮 LLM 看不到皮皮自己刚说过的话，会"失忆"。
        # 带去重：handle_proactive 已记过同一句时跳过。
        self._session.history_sink = self._record_spontaneous
        self._vp = getattr(self.app, "voiceprint", None)
        self._last_uid = ""
        if self._vp is not None:
            print("  声纹识别已就绪（第一段语音自动登记为主人；之后自动区分主人/陌生人）\n")

        # 提醒/待办：绑定发声通道并启动（到点由皮皮自己说）
        self._reminders = getattr(self.app, "reminders", None)
        if self._reminders is not None:
            self._reminders.bind_speak(self._session.try_speak_spontaneous)
            self._reminders.start()
            print("  提醒/闹铃已就绪（说「提醒我X点…」「设闹钟…」即可）\n")

        # 生命模块（PULSE 心搏 / 旧 LifeEngine，可配置回退）
        #   pulse.backend: "pulse"(默认，LivingLoop) | "legacy"(旧引擎)
        self._life = None
        if self.app.cfg.get("life.enabled", True) and self.app.asr is not None:
            backend = self.app.cfg.get("pulse.backend", "pulse")
            if backend == "legacy":
                self._life = self._build_legacy_life()
            else:
                self._life = self._build_pulse_life()
            self._session.life = self._life
            self._life.start()
            if self.verbose:
                import logging as _logging
                _logging.getLogger("pipi.pulse").setLevel(_logging.INFO)
            label = "PULSE 心搏（呼吸/潮汐/预感/羁绊/日记成长）" \
                if backend != "legacy" else "生命模块"
            print(f"  {label}已启动（空闲时会自发念叨、微动作）\n")

        # WebSocket 推送：静态监控页直接订阅（无 Web 服务依赖）
        await self._setup_monitor_push()

        # 摄像头画面 → 监控页（JPEG 推流，只推不落盘）
        if (self.app.cfg.get("vision.monitor_stream", True)
                and type(self.app.vision).__name__ == "VisionPipeline"
                and self._hub is not None):
            self._stream_task = asyncio.create_task(
                self._camera_stream(), name="camera-stream")

        try:
            await self._session.run()
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n皮皮 > 那你走吧……哼")
        finally:
            if self._stream_task is not None:
                self._stream_task.cancel()
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

    def _build_pulse_life(self):
        """PULSE 心搏运行时（LivingLoop 适配层）。"""
        from pipi.pulse.runtime import PulseRuntime

        rt = PulseRuntime(
            cfg=self.app.cfg,
            session=self._session,
            vision=getattr(self.app, "vision", None),
            motion=getattr(self.app, "motion", None),
            weather=getattr(self.orch, "weather", None),
            env=getattr(self.orch, "env", None),
        )
        rt.bind_cfg(self.app.cfg)
        # 提醒触发 → 记入当天日记（生命流留痕）
        if self._reminders is not None:
            orig = getattr(self._reminders, "sink", None)

            def _sink_with_diary(kind, text, _orig=orig, _rt=rt):
                if kind == "fired":
                    try:
                        _rt.note_event("reminder", text)
                    except Exception:
                        pass
                if _orig is not None:
                    try:
                        _orig(kind, text)
                    except Exception:
                        pass
            self._reminders.sink = _sink_with_diary
        # 内心状态注入对话：每轮 LLM 读到此刻真实身心（消除"两张皮"）
        try:
            self.orch.inner_state_provider = rt.inner_brief
        except Exception:
            pass
        # 统一说话：主动发声也走 LLM（按内心事件渲染），句库在 runtime 内兜底
        async def _render_vocal(event, _cli=self):
            uid = getattr(_cli, "_active_uid", None) or "owner"
            res = await _cli.orch.handle_proactive(uid, event)
            if res.bubbles:
                return res.bubbles[0].text or None
            return getattr(res, "text", None) or None
        rt.vocal_renderer = _render_vocal
        return rt

    def _build_legacy_life(self):
        """旧 LifeEngine（pulse.backend=legacy 时使用）。"""
        import random as _random

        from pipi.life import LifeEngine

        persona = self.app.persona
        bot_name = self.app.cfg.get("persona.bot_name", "皮皮")
        life = LifeEngine(
            cfg=self.app.cfg,
            llm=self.app.llm,
            persona_name=bot_name,
            persona_hint=f"{bot_name}，{persona.name}人格（{persona.id}），语气傲娇但内心黏人",
            vision=getattr(self.app, "vision", None),
            memory_getter=lambda: (
                (lambda items: _random.choice(items).content if items else None)(
                    [m for m in self.app.repos.memory.list_live(self._active_uid)
                     if m.type.value in ("identity", "preference",
                                         "event", "commitment", "relation")])
            ),
            session=self._session,
        )

        # C4：联想链优先引用 story 记忆（成长故事闪回）
        def _story_getter():
            items = self.app.repos.memory.list_story(self.user_id, limit=5)
            return _random.choice(items) if items else None

        life.story_getter = _story_getter
        return life

    # ------------------------------------------------------------------
    # WebSocket 推送：静态监控页直接订阅，无需启动任何 Web 服务
    # ------------------------------------------------------------------
    async def _setup_monitor_push(self) -> None:
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
            print(f"  监控数据推送 ws://{ws_host}:{ws_port}/ws"
                  f"（双击打开 web/monitor.html 即可查看）\n")
        except Exception as exc:
            print(f"  （WebSocket 推送不可用：{exc}）")

    def _on_barge_in(self) -> None:
        """打断：立即停止 TTS 播放并清空缓冲。"""
        if self._speaker is not None:
            self._speaker.abort()
            self._speaker = None

    async def _camera_stream(self) -> None:
        """摄像头画面 → 监控页：JPEG base64 经 WS 推流（只推不落盘）。"""
        import base64
        import time as _time

        import cv2

        interval = 1.0 / max(float(self.app.cfg.get("vision.monitor_fps", 0.5)), 0.1)
        cam = self.app.vision.camera
        while True:
            try:
                frame = await cam.read()
                if frame is not None:
                    small = cv2.resize(frame, (320, 240))
                    ok, buf = cv2.imencode(
                        ".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
                    if ok and self._hub is not None:
                        data = "data:image/jpeg;base64," + base64.b64encode(buf).decode()
                        self._hub.broadcast({"type": "camera", "data": data})
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def _current_vision(self):
        """对话用的视觉观测：优先感知循环最新帧（≤5s），否则现场拍一帧。

        感知循环未启动（纯文本模式无竞争）才允许现场 capture；
        voice 模式抢拍会吃掉 owner_newly_arrived 状态沿（PERCEPTION_PLAN ①）。
        """
        import time as _time

        vision = getattr(self.app, "vision", None)
        if vision is None:
            return None
        life = self._life
        latest = life.latest_observation() if life is not None else None
        if latest is not None and _time.time() - latest.timestamp < 5.0:
            return latest
        try:
            # 感知循环未跑（文本模式）时现场拍一帧；推理丢线程池防阻塞
            if hasattr(vision, "camera"):
                frame = await vision.camera.read()
                if frame is None:
                    return None
                return await asyncio.to_thread(vision.process_frame, frame)
            return await vision.capture()
        except Exception:
            return None

    # 云端视觉触发词（收紧：只有真的在问画面内容才传图，
    # "星期几/几点"这类皮皮自己能答的问题不触发）
    _VLM_HINT_WORDS = ("看见", "看到", "我穿", "衣服", "表情",
                       "长什么样", "长啥样", "认出", "认得")

    def _looks_visual(self, text: str) -> bool:
        return any(k in text for k in self._VLM_HINT_WORDS)

    async def _cloud_describe(self, question: str) -> str | None:
        """把当前画面传给多模态 LLM，换回一句客观描述（复杂视觉理解上云）。"""
        vision = getattr(self.app, "vision", None)
        if vision is None or type(vision).__name__ != "VisionPipeline":
            return None
        try:
            import base64

            import cv2
            frame = await vision.camera.read()
            if frame is None:
                return None
            ok, buf = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ok:
                return None
            data_url = "data:image/jpeg;base64," + base64.b64encode(buf).decode()
            messages = [
                {"role": "system",
                 "content": "你是机器人皮皮的眼睛。用中文客观描述画面："
                            "有几个人、衣着颜色、表情与姿态。不要寒暄，"
                            "不超过 60 字。"},
                {"role": "user", "content": [
                    {"type": "text",
                     "text": question or "描述你现在看到的画面"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]},
            ]
            # 方舟要求视觉输入必须用支持视觉的模型：配置了 vlm_model
            # （视觉端点）时走独立实例，否则用主对话模型
            llm = self.app.llm
            vlm_model = self.app.cfg.get("vision.vlm_model", "")
            if vlm_model and vlm_model != getattr(llm, "model", ""):
                from pipi.providers.llm.openai_compat import OpenAICompatLLM
                probe = OpenAICompatLLM(
                    base_url=llm.base_url, api_key=llm.api_key,
                    model=vlm_model, name="vlm",
                    extra_body=getattr(llm, "extra_body", None),
                    timeout_s=getattr(llm, "timeout_s", 30))
                resp = await probe.chat(messages, temperature=0.3,
                                        max_tokens=120)
            else:
                resp = await llm.chat(messages, temperature=0.3,
                                      max_tokens=120)
            text = getattr(resp, "text", "") or ""
            return text.strip() or None
        except Exception as exc:
            logger.debug("云端视觉识别失败：%s", exc)
            return None

    async def _vision_for_turn(self, text: str):
        """对话用观测：本地检测 + 可选云端视觉理解（写入 obs.raw['_vlm']）。"""
        obs = await self._current_vision()
        if (obs is not None and obs.faces
                and self.app.cfg.get("vision.vlm", True)
                and self._looks_visual(text)):
            desc = await self._cloud_describe(text)
            if desc:
                obs.raw["_vlm"] = desc
                if self.verbose:
                    print(f"  [云端视觉] {desc}", flush=True)
        return obs

    async def _respond_stream(self, text: str) -> None:
        """应答：LLM 流式 → 逐字显示 + 分句 TTS 边生成边播放。"""
        import time as _time
        self._active_uid = getattr(self._session, "current_user_id", "") \
            or self.user_id
        if self._chat_lock is None:
            self._chat_lock = asyncio.Lock()
        async with self._chat_lock:
            await self._respond_stream_locked(text)

    def _record_spontaneous(self, text: str) -> None:
        """自发发声 → 写入对话历史（去重：最后一条 assistant 相同则跳过）。

        让 LLM 在下一轮能看到皮皮自己刚说过的话（修复"刚才说有怪声，
        转脸又说没怪事"的失忆）。
        """
        if not text:
            return
        try:
            uid = self._active_uid or "owner"
            sess = self.orch.get_session(uid)
            last = sess.context_messages(1)
            if last and last[0]["role"] == "assistant" \
                    and last[0]["content"] == text:
                return
            sess.add(Role.ASSISTANT, text)
        except Exception as exc:
            logger.debug("自发发声记历史失败（忽略）：%s", exc)

    def _identify_speaker(self, audio) -> str | None:
        """声纹识别回调：登记主人 / 区分陌生人。"""
        if self._vp is None:
            return None
        uid = self._vp.identify(audio)
        if not self._vp.enrolled and getattr(self._vp, "_enroll_buf", None):
            print(f"  声纹登记中 {len(self._vp._enroll_buf)}/"
                  f"{self._vp.enroll_target}（再说几句完成建档）", flush=True)
        if self.verbose and getattr(self._vp, "last_sim", None) is not None:
            print(f"  声纹相似度 {self._vp.last_sim:.2f}", flush=True)
        if uid and uid != self._last_uid:
            if uid == "owner":
                print("  已登记声纹：你就是主人" if not self._last_uid
                      else "  主人", flush=True)
            elif uid == "stranger":
                print("  你是陌生人吧？我们先认识一下", flush=True)
            self._last_uid = uid
            if self._reminders is not None:
                self._reminders.active_uid = uid   # 面板展示谁的待办
        return uid

    def _query_notice(self, text: str) -> str | None:
        """查询类意图 → 先应一声的提示语（查询在 handle_turn 内并行执行）。"""
        import random

        try:
            orch = self.orch
            if orch.weather is not None and orch.weather.detect(text):
                return random.choice(("等等哈，我查查天气……",
                                      "稍等，我看一眼天气……"))
            if orch.search is not None and orch.search.detect(text):
                return random.choice(("等等哈，我查查……", "稍等，我搜一下……"))
        except Exception:
            pass
        return None

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

        # 查询类问题（天气/联网搜索）耗时长：先应一声，查询随后并行跑
        notice = self._query_notice(text)
        if notice:
            if speaker is not None:
                await speaker.feed(notice)
            else:
                print(f"皮皮 > （{notice}）", flush=True)

        state = {"printed": False, "ttfb_ms": 0}
        obs = await self._vision_for_turn(text)

        async def on_chunk(delta: str) -> None:
            if not state["printed"]:
                state["ttfb_ms"] = int((_time.perf_counter() - t0) * 1000)
                print("皮皮 > ", end="", flush=True)
                state["printed"] = True
            print(delta, end="", flush=True)
            if speaker is not None:
                await speaker.feed(delta)

        result = await self.orch.handle_turn_stream(
            self._active_uid, text, on_chunk=on_chunk,
            vision=obs)
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
            print(f"  语音 {s.last_utt_s:.1f}s → ASR {s.last_asr_ms}ms"
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
            print(f"  说话→回完 {total:.0f}ms ｜ 记忆已写入 "
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
            result = await self.orch.handle_turn_stream(
                self.user_id, text, vision=await self._vision_for_turn(text))
            if self._life is not None:
                for b in result.bubbles:
                    self._life.log_chat("pipi", b.text)
                if result.emotion_state is not None:
                    self._life.physio.observe_dialogue_mood(
                        result.emotion_state.mood.value)

    async def _say(self, text: str) -> None:
        self.app.proactive_engine.note_user_response(self.app.clock.now())
        notice = self._query_notice(text)
        if notice:
            print(f"皮皮 > （{notice}）", flush=True)
        result = await self.orch.handle_turn(
            self.user_id, text, vision=await self._vision_for_turn(text))

        for b in result.bubbles:
            print(f"皮皮 > {b.text}")

        if self.verbose:
            llm = getattr(self.app.llm, "last_latency_ms", 0)
            print(f"  编排 {result.latency_ms}ms（LLM {llm}ms）"
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

        elif name == "/enroll":
            await self._do_enroll()

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
            bar = "█" * int(v) + "·" * (10 - int(v))
            print(f"  │ 偏好      {k} {bar} {v:.1f}")
        expr = gp.learned_expressions
        if expr:
            top = sorted(expr.items(), key=lambda kv: -kv[1]["count"])[:5]
            print(f"  │ 学到的口头禅：" + "、".join(
                f"{k}×{v['count']}" for k, v in top))
        print(f"  │ 相处      相识 {gp.active_days()} 天 · {gp.turns()} 轮对话")
        print("  └─────────────────────────────")

    async def _do_enroll(self) -> None:
        """登记主人人脸：抓帧 → InsightFace 提特征 → 落库并即时生效。"""
        vision = self.app.vision
        rec = getattr(vision, "recognizer", None) if vision else None
        if vision is None or rec is None or type(vision).__name__ == "MockVision":
            print("  视觉不可用（无摄像头或缺 opencv/insightface），无法登记人脸。")
            print('  安装：pip install -e ".[vision]"')
            return
        print("  请正对摄像头，保持几秒……")
        frames = []
        for _ in range(20):                     # 最多 20 拍，凑 6 张有效帧
            try:
                frame = await vision.camera.read()
            except Exception as exc:
                print(f"  摄像头读取失败：{exc}")
                return
            if frame is not None:
                frames.append(frame)
            if len(frames) >= 6:
                break
            await asyncio.sleep(0.15)
        if len(frames) < 3:
            print("  没抓到足够画面，登记取消（检查摄像头是否被其它程序占用）。")
            return
        try:
            emb = rec.enroll("owner", frames, is_owner=True)
        except Exception as exc:
            print(f"  人脸识别初始化失败：{exc}")
            print("  （InsightFace 首次运行需下载模型，检查网络后重试 /enroll）")
            return
        if not emb:
            print("  没检测到人脸 —— 靠近一点、光线亮一些，再试 /enroll。")
            return
        import time as _time

        self.app.repos.face.enroll(
            "owner", self.user_id, "主人", emb,
            model=getattr(rec, "model_name", "insightface"),
            now=_time.time(), is_owner=True,
        )
        print(f"  √ 主人人脸已登记（{len(frames)} 帧）。回家问候、'看向你'都会认得你。")

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
            flag = "★" if m.pinned else "  "
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
                  voice: bool = False) -> None:
    """启动 CLI。"""
    app = build_app(cfg, sim=sim)
    try:
        cli = CLI(app, user_id=user_id, verbose=verbose, voice=voice)
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
