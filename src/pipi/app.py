"""应用装配 —— 把所有模块接起来。

这是整个系统的"总装车间"：
  - 读配置
  - 建数据库连接与仓储
  - 按降级链创建各 Provider
  - 装配 Agent 与编排器
  - 打印能力健康矩阵

`--sim` 模式：全部用 mock，零硬件/零网络/零模型也能端到端跑通。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pipi.agents.dialogue_expert import DialogueExpert
from pipi.agents.emotion_analyst import EmotionAnalyst
from pipi.agents.memory_expert import MemoryExpert
from pipi.agents.router import CapabilityRouter, CircuitBreaker
from pipi.capabilities.emotion_engine import EmotionEngine
from pipi.capabilities.memory_manager import MemoryManager
from pipi.domain.persona import PersonaSpec
from pipi.infra.clock import Clock, SystemClock
from pipi.infra.config import PROJECT_ROOT, Config
from pipi.infra.db import (
    Database,
    DecayRepo,
    EmotionRepo,
    FaceRepo,
    KVRepo,
    MemoryRepo,
    ProfileRepo,
    ProactiveRepo,
    ReminderRepo,
    TurnRepo,
    UserRepo,
)
from pipi.infra.event_bus import EventBus
from pipi.orchestration.orchestrator import ConversationOrchestrator
from pipi.orchestration.proactive import ProactiveEngine
from pipi.providers.llm import MockLLM, create_llm
from pipi.providers.llm.prompt_builder import PromptBuilder
from pipi.providers.memory import SqliteMemoryStore, create_embedder

logger = logging.getLogger(__name__)


@dataclass
class Repos:
    """所有仓储的集合，方便传递。"""

    user: UserRepo
    profile: ProfileRepo
    memory: MemoryRepo
    turn: TurnRepo
    emotion: EmotionRepo
    proactive: ProactiveRepo
    decay: DecayRepo
    face: FaceRepo
    kv: KVRepo
    growth: "GrowthRepo"
    reminder: ReminderRepo


@dataclass
class App:
    """装配完成的应用对象。"""

    cfg: Config
    db: Database
    repos: Repos
    clock: Clock
    bus: EventBus
    persona: PersonaSpec
    llm: Any
    memory: MemoryManager
    memory_store: SqliteMemoryStore
    orchestrator: ConversationOrchestrator
    proactive_engine: ProactiveEngine
    router: CapabilityRouter
    sim_mode: bool = False
    tts: Any | None = None
    vision: Any | None = None
    motion: Any | None = None
    asr: Any | None = None
    reminders: Any | None = None
    voiceprint: Any | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def health_report(self) -> str:
        lines = [
            "=" * 56,
            f"  皮皮 · 启动自检{'（模拟模式）' if self.sim_mode else ''}",
            "=" * 56,
            f"  人格        {self.persona.name} ({self.persona.id})",
            f"  LLM         {getattr(self.llm, 'name', 'unknown')}",
            f"  向量模型    {self.memory_store.embedder.name}",
            f"  数据库      {self.db.path}",
            "",
            self.router.summary(),
            "",
            f"  记忆条数    {self.repos.memory.count(self.cfg.get('user.default_user_id', 'owner'))}",
            "=" * 56,
        ]
        if self.asr:
            lines.insert(5, f"  语音输入    {getattr(self.asr, 'name', 'unknown')} + 麦克风")
        if self.tts:
            lines.insert(6, f"  语音输出    {getattr(self.tts, 'name', 'unknown')}")
        return "\n".join(lines)

    def close(self) -> None:
        self.db.close()
        self.bus.close()


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

def load_persona(cfg: Config) -> PersonaSpec:
    """按配置加载人格 YAML。"""
    pid = cfg.get("persona.active", "tsundere")
    pdir = PROJECT_ROOT / cfg.get("persona.personas_dir", "configs/personas")
    path = pdir / f"{pid}.yaml"
    if not path.exists():
        logger.warning("未找到人格文件 %s，回退默认", path)
        return PersonaSpec(id="default", name="皮皮")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PersonaSpec.from_dict(data)


def build_repos(db: Database) -> Repos:
    from pipi.infra.db.repositories import GrowthRepo

    return Repos(
        user=UserRepo(db),
        profile=ProfileRepo(db),
        memory=MemoryRepo(db),
        turn=TurnRepo(db),
        emotion=EmotionRepo(db),
        proactive=ProactiveRepo(db),
        decay=DecayRepo(db),
        face=FaceRepo(db),
        kv=KVRepo(db),
        growth=GrowthRepo(db),
        reminder=ReminderRepo(db),
    )


def build_llm(cfg: Config, sim: bool) -> Any:
    """按配置创建 LLM，失败时降级到 Mock。"""
    if sim:
        return MockLLM()

    provider = cfg.get("llm.provider", "qwen")
    api_key = cfg.get("llm.api_key", "") or ""
    if not api_key:
        logger.warning("未配置 LLM api_key，使用 MockLLM（规则回复）")
        return MockLLM()

    try:
        llm = create_llm(
            provider=provider,
            api_key=api_key,
            base_url=cfg.get("llm.base_url", ""),
            model=cfg.get("llm.model", ""),
            cheap_model=cfg.get("llm.cheap_model", ""),
            timeout_s=cfg.get("llm.timeout_s", 30),
            max_retries=cfg.get("llm.max_retries", 2),
            extra_body=cfg.get("llm.extra_body", None),
        )
        if llm.is_available():
            return llm
    except Exception as exc:
        logger.warning("LLM 初始化失败，降级 Mock：%s", exc)

    return MockLLM()


def _register_fallbacks(cfg: Config, sim: bool, router: CapabilityRouter) -> None:
    """注册降级链末端的 mock Provider —— 保证永远有兜底可用。

    注意：注册名必须与 degradation.yaml 里降级链中的名字**完全一致**
    （如 "mock"），否则链上找不到它。
    """
    from pipi.providers.llm import MockLLM

    # LLM：mock 总是可用（链上是 "mock"）
    router.register("mock", MockLLM(), available=True)

    # 说明：本方案为**纯云端**，不注册本地模型档。
    # 云端两家（qwen / deepseek）互为灾备，全挂时由上面的 mock 用规则话术兜住。

    # 视觉链上是 "full" / "detect_only"，需要显式注册这两个降级档位，
    # 否则链走到底会找不到 provider（健康矩阵显示 ✗）。
    try:
        from pipi.providers.vision.mock import MockVision
        from pipi.providers.vision.pipeline import VisionPipeline

        if sim:
            router.register("vision:full", MockVision(), available=True)
            router.register("vision:detect_only", MockVision(), available=True)
        else:
            # 实机：先尝试建完整 pipeline；建不起来就退到纯 mock
            try:
                from pipi.providers.vision import create_vision
                full = create_vision(cfg)
            except Exception:
                full = None
            if full is not None:
                router.register("vision:full", full,
                                available=bool(getattr(full, "is_available", lambda: True)()))
            router.register("vision:detect_only", MockVision(), available=True)
    except Exception as exc:
        logger.debug("视觉降级档位注册失败：%s", exc)

    # ASR / TTS / Vision / Motion 的通用 mock 兜底（链末尾都写 "mock"）
    # 同名的 "mock" 会互相覆盖，所以一律用 "{cap}:mock" 注册，
    # 由 CapabilityRouter.choose(cap) 按能力前缀查找。
    for cap, module, factory in (
        ("asr", "pipi.providers.asr", "create_mock_asr"),
        ("tts", "pipi.providers.tts", "create_mock_tts"),
        ("vision", "pipi.providers.vision", "create_mock_vision"),
        ("motion", "pipi.providers.motion", "create_mock_motion"),
    ):
        try:
            mod = __import__(module, fromlist=[factory])
            provider = getattr(mod, factory)()
            if provider is not None:
                router.register(f"{cap}:mock", provider, available=True)
        except Exception as exc:
            logger.debug("%s mock 不可用：%s", cap, exc)


def _pick(router: CapabilityRouter, capability: str, now: float,
          app_ctx: dict) -> Any | None:
    """从降级链挑一个可用 provider；已在 app_ctx 里的优先。"""
    if app_ctx.get(capability):
        return app_ctx[capability]
    provider, decision = router.choose(capability, now)
    if provider is None:
        logger.debug("能力 %s 无可用 Provider（%s）", capability, decision.reason)
    return provider


def _build_optional_providers(cfg: Config, app_ctx: dict, sim: bool,
                              router: CapabilityRouter) -> None:
    """构建可选 Provider（语音/视觉/运动），失败不阻塞启动。"""
    # ---- ASR（语音输入：麦克风 → 识别引擎）----
    if cfg.get("audio.enabled", True) and not sim:
        try:
            from pipi.providers.asr import create_asr

            for name in cfg.get("capabilities.asr.chain", []):
                if name == "mock":
                    break  # mock 是键盘输入，不算语音输入
                provider = create_asr(name, cfg)
                if provider is not None:
                    app_ctx["asr"] = provider
                    router.register(f"asr:{name}", provider, available=True)
                    # 启动即后台预热模型（SenseVoice 首次加载约 4s，
                    # 不预热的话第一句唤醒会明显卡顿）
                    if hasattr(provider, "_ensure_model"):
                        import threading

                        threading.Thread(
                            target=provider._ensure_model, daemon=True
                        ).start()
                    break
        except Exception as exc:
            logger.info("ASR 不可用（不影响文本模式）：%s", exc)

    # ---- TTS（语音输出）----
    if cfg.get("audio.enabled", True) and not sim:
        try:
            from pipi.providers.tts import create_tts

            for name in cfg.get("capabilities.tts.chain", []):
                if name == "mock":
                    break  # mock 只打印，不算语音输出
                provider = create_tts(name, cfg)
                if provider is not None:
                    app_ctx["tts"] = provider
                    router.register(f"tts:{name}", provider, available=True)
                    break
        except Exception as exc:
            logger.info("TTS 不可用（不影响文本模式）：%s", exc)

    # ---- 视觉 ----
    if cfg.get("vision.enabled", True) and not sim:
        try:
            from pipi.providers.vision import create_vision
            vis = create_vision(cfg)
            if vis and (not hasattr(vis, "is_available") or vis.is_available()):
                app_ctx["vision"] = vis
                router.register(f"vision:{vis.name}", vis, available=True)
        except Exception as exc:
            logger.info("视觉不可用（不影响对话）：%s", exc)

    # ---- 运动 ----
    if cfg.get("motion.enabled", False):
        try:
            from pipi.providers.motion import create_motion
            mot = create_motion(cfg)
            if mot:
                app_ctx["motion"] = mot
                router.register(f"motion:{mot.name}", mot)
        except Exception as exc:
            logger.info("运动控制不可用：%s", exc)


def build_app(cfg: Config | None = None, *, sim: bool = False,
              clock: Clock | None = None) -> App:
    """装配应用。

    Args:
        cfg: 配置，None 时加载默认配置
        sim: 模拟模式（全 mock，零依赖）
        clock: 可注入时钟（测试用）
    """
    cfg = cfg or Config.default()
    now = (clock or SystemClock()).now()

    # 数据库
    data_dir = PROJECT_ROOT / cfg.get("app.data_dir", "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = cfg.get("memory.db_path", "data/pipi.db")
    if not Path(db_path).is_absolute():
        db_path = PROJECT_ROOT / db_path
    db = Database(db_path)

    repos = build_repos(db)
    bus = EventBus()
    clk = clock or SystemClock(cfg.get("app.timezone", "Asia/Shanghai"))

    # 确保默认用户存在
    default_uid = cfg.get("user.default_user_id", "owner")
    repos.user.ensure(default_uid, now)

    # 人格
    persona = load_persona(cfg)

    # LLM
    llm = build_llm(cfg, sim)

    # 向量 + 记忆
    embedder = create_embedder(
        cfg.get("memory.embedder", "auto"),
        cfg.get("memory.embed_model_path", ""),
    )
    memory_store = SqliteMemoryStore(
        repos.memory, embedder,
        min_salience=0.05,
    )
    memory_mgr = MemoryManager(
        memory_store, embedder,
        llm=None if sim else llm,     # sim 模式走规则抽取
        top_k=cfg.get("memory.top_k", 5),
        max_per_type=cfg.get("memory.max_per_type", 2),
    )

    # 路由与降级
    breaker_cfg = cfg.section("circuit_breaker")
    router = CapabilityRouter(
        chains={
            "asr": cfg.get("capabilities.asr.chain", []),
            "tts": cfg.get("capabilities.tts.chain", []),
            "llm": cfg.get("capabilities.llm.chain", []),
            "vision": cfg.get("capabilities.vision.chain", []),
            "embedding": cfg.get("capabilities.embedding.chain", []),
            "voiceprint": cfg.get("capabilities.voiceprint.chain", []),
            "motion": cfg.get("capabilities.motion.chain", []),
        },
        breaker=CircuitBreaker(
            failure_threshold=breaker_cfg.get("failure_threshold", 3),
            recovery_timeout_s=breaker_cfg.get("recovery_timeout_s", 60),
        ),
    )
    router.register("llm:" + getattr(llm, "name", "llm"), llm)
    # embedding：链上写 "bge_small"/"tfidf"，两种名字都注册，避免漏匹配
    router.register(f"embedding:{embedder.name}", embedder, available=True)
    router.register(embedder.name, embedder, available=True)
    # 让另一条链名也能命中（实际加载的是哪个，就用哪个顶上）
    for alias in ("bge_small", "tfidf"):
        if alias != embedder.name:
            router.register(f"embedding:{alias}", embedder, available=True)
            router.register(alias, embedder, available=True)

    # 注册 mock 兜底 Provider —— 保证降级链末端永远可用
    _register_fallbacks(cfg, sim, router)

    # 可选 Provider
    app_ctx: dict[str, Any] = {}
    _build_optional_providers(cfg, app_ctx, sim, router)

    # 通过路由挑选可用的 TTS / 视觉 / 运动（含 mock 兜底）
    now_ts = clk.now()
    if tts_provider := _pick(router, "tts", now_ts, app_ctx):
        app_ctx["tts"] = tts_provider
    if motion_provider := _pick(router, "motion", now_ts, app_ctx):
        app_ctx["motion"] = motion_provider
    if vision_provider := _pick(router, "vision", now_ts, app_ctx):
        app_ctx["vision"] = vision_provider

    # 情感引擎
    engine = EmotionEngine(persona, llm=None if sim else llm)

    # Prompt 组装
    profile = repos.profile.get(default_uid)
    owner_name = profile.display_name if not profile.is_new() else "主人"
    pb = PromptBuilder(persona, owner_name=owner_name)

    # 成长引擎（可成长性；关闭时 orchestrator.growth 为 None）
    growth_engine = None
    if cfg.get("growth.enabled", True):
        from pipi.orchestration.growth import GrowthEngine

        growth_engine = GrowthEngine(cfg, repos, memory_mgr, clk)
        growth_engine.persona_like = list(persona.speech_style.catchphrases or [])
        growth_engine.persona_like += list(getattr(persona, "likes", []) or [])

    # Agent
    emotion_analyst = EmotionAnalyst(engine)
    memory_expert = MemoryExpert(memory_mgr, top_k=cfg.get("memory.top_k", 5))
    dialogue_expert = DialogueExpert(
        llm, pb,
        use_cheap_routing=cfg.get("llm.routing.enabled", True),
        simple_max_chars=cfg.get("llm.routing.simple_max_chars", 8),
        growth=growth_engine,
    )

    # 编排器
    orchestrator = ConversationOrchestrator(
        llm=llm, persona=persona, memory=memory_mgr,
        emotion_analyst=emotion_analyst,
        memory_expert=memory_expert,
        dialogue_expert=dialogue_expert,
        clock=clk,
        user_repo=repos.user, profile_repo=repos.profile,
        turn_repo=repos.turn, emotion_repo=repos.emotion,
        tts=app_ctx.get("tts"), motion=app_ctx.get("motion"),
        owner_name=owner_name,
        growth=growth_engine,
    )

    # 提醒 / 闹铃 / 待办（阶段 F）
    reminder_service = None
    if cfg.get("reminders.enabled", True):
        from pipi.orchestration.reminders import ReminderService

        reminder_service = ReminderService(repos.reminder, clk, cfg=cfg)
        orchestrator.reminders = reminder_service

    # 声纹识别（主人 / 陌生人分轨，非 sim 且有麦克风时启用）
    voiceprint_service = None
    vp_cfg = cfg.get("audio.mic.voiceprint", {}) or {}
    if not sim and vp_cfg.get("enabled", True):
        try:
            from pipi.capabilities.voiceprint import VoicePrintManager

            voiceprint_service = VoicePrintManager(
                vp_cfg.get(
                    "model",
                    "data/models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"),
                kv_repo=repos.kv,
                threshold=vp_cfg.get("threshold", 0.5),
                auto_enroll=vp_cfg.get("auto_enroll", True),
                reject_threshold=vp_cfg.get("reject_threshold", 0.35),
            )
            router.register("voiceprint:sherpa", voiceprint_service,
                            available=True)
        except Exception as exc:
            logger.warning("声纹识别不可用（%s），所有语音按主人处理", exc)

    # 主动引擎
    proactive_engine = ProactiveEngine(
        clock=clk,
        repo=repos.proactive,
        daily_limit=persona.proactive_daily_limit,
        idle_hours=cfg.get("proactive.idle_hours", 2.0),
        quiet_hours=tuple(cfg.get("proactive.quiet_hours", [23, 7])),
        return_cooldown_min=cfg.get("proactive.return_greeting_cooldown_min", 30),
        triggers=cfg.get("proactive.triggers", {}),
        persona_clinginess=persona.traits.clinginess,
        cooldowns=cfg.get("proactive.cooldowns", {}),
    )

    return App(
        cfg=cfg, db=db, repos=repos, clock=clk, bus=bus,
        persona=persona, llm=llm, memory=memory_mgr,
        memory_store=memory_store, orchestrator=orchestrator,
        proactive_engine=proactive_engine, router=router,
        sim_mode=sim,
        tts=app_ctx.get("tts"), vision=app_ctx.get("vision"),
        motion=app_ctx.get("motion"), asr=app_ctx.get("asr"),
        reminders=reminder_service,
        voiceprint=voiceprint_service,
    )
