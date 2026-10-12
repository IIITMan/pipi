"""成长引擎 GrowthEngine —— 事实记录 + 阶段只读派生（GROWTH_DESIGN v0.2）。

接入方式（B1）：orchestrator._post_turn 末尾异步调用 `growth.on_turn(...)`。
单向写入：只写不读、失败静默、绝不影响一轮回复（§2.4 单向性）。

状态所有权（契约 docs/ENGINE_CONTRACTS.md §3，2026-10-13 调整）：
  - 阶段 S1~S4 为**只读派生**：由 Bond.trust（关系唯一权威）+ 里程碑 + 活跃天数
    计算，引擎不再写 users.relationship_stage（派生结果仅内存展示/审计日志）。
  - 事实记录（里程碑/统计/偏好/口头禅）仍由本引擎唯一写入。
  - S4 附加门槛：trust ≥ rel.min_trust_s4（默认 60），替代旧 affection 死门槛。

核心机制：
  - 阶段机 S1~S4：硬性天数+里程碑门槛，不可跳跃；回落不低于 S2
  - 可塑性曲线：早期变化快、后期收敛（偏好演化步长系数）
  - 里程碑 M1~M7：命中即写 growth_milestones 并沉淀 commitment/story 记忆
  - 表达拾取：主人高频短句 ≥3 次入库（模仿学习数据源）
  - 全部演化事件写 growth_log 可回看
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from pipi.domain.growth import GrowthProfile, S4
from pipi.domain.memory import MemoryType

logger = logging.getLogger(__name__)

_DAY = 86400.0

# 里程碑语义表（触发在代码各处，这里只管文案与记忆类型）
MILESTONE_TEXT = {
    "M1": "我们完成了认主，从陌生变成了一一对应的关系",
    "M2": "我记住了第一个喜好",
    "M3": "我第一次主动关心你，而你回应了我",
    "M4": "我们相遇满 30 天了",
    "M5": "你授权我用昵称称呼你",
    "M6": "我们说了第 100 次晚安",
    "M7": "我第一次在发呆时想起我们的故事",
}


class GrowthEngine:
    """挂在回合结束的成长引擎（单向、异步、可关、可回看）。"""

    def __init__(self, cfg: Any, repos: Any, memory: Any, clock: Any):
        self.cfg = cfg.get("growth", {}) or {}
        self.enabled = bool(self.cfg.get("enabled", True))
        self.repos = repos                  # growth/user/profile/proactive
        self.memory = memory                # MemoryManager（写里程碑记忆）
        self.clock = clock
        self.persona_like: list[str] = []   # 人格偏好词表（app 装配时注入）
        # 关系唯一权威（Bond.trust）的读取器：由接入层注册
        # （cli 装配 PULSE 后注入 lambda uid: rt.loop.bond.trust）。
        # None → 阶段派生退化为"天数+里程碑"（S4 附加门槛不满足）。
        self.trust_provider: Callable[[str], float | None] | None = None

    # ------------------------------------------------------------------
    # 装配读取
    # ------------------------------------------------------------------

    def _rel_cfg(self) -> dict:
        return self.cfg.get("relationship", {}) or {}

    def _curve(self) -> list[float]:
        return list((self.cfg.get("plasticity", {}) or {}).get(
            "curve", [1.0, 0.6, 0.35, 0.15]))

    # ------------------------------------------------------------------

    def load(self, user_id: str) -> GrowthProfile:
        """从 users 列 + profiles.extra + milestones 表装配成长档案。"""
        urow = self.repos.user.get(user_id) or {}
        profile = self.repos.profile.get(user_id)
        extra = profile.extra or {}
        gp = GrowthProfile.from_dict({
            "user_id": user_id,
            "stage": urow.get("relationship_stage") or 1,
            "stage_since": urow.get("stage_since") or 0.0,
            "meet_date": urow.get("meet_date") or urow.get("met_at") or 0.0,
            "nickname_authorized": bool(urow.get("nickname_authorized")),
            "preferences": extra.get("preferences_runtime") or {},
            "learned_expressions": extra.get("learned_expressions") or {},
            "stats": extra.get("growth_stats") or {},
        })
        gp.milestones = [m["code"] for m in self.repos.growth.list_milestones(user_id)]
        return gp

    # ------------------------------------------------------------------
    # B1：回合结束钩子（单向、故障隔离）
    # ------------------------------------------------------------------

    async def on_turn(self, user_id: str, user_text: str,
                      emotion_state: Any | None = None,
                      now: float | None = None) -> None:
        """回合结束调用。任何异常只记日志，绝不向上抛（§8.6 单向性）。"""
        if not self.enabled:
            return
        try:
            now = now if now is not None else self.clock.now()
            gp = self.load(user_id)

            self._update_stats(gp, user_text, now)
            self._apply_derived_stage(user_id, gp, now)
            await self._check_milestones(user_id, gp, user_text, emotion_state, now)
            self._evolve_preferences(user_id, gp, user_text, emotion_state, now)
            self._learn_expressions(user_id, gp, user_text, now)
            self._persist(gp)
        except Exception as exc:   # noqa: BLE001 —— 单向性：吞掉一切
            logger.warning("成长引擎异常（已忽略，不影响对话）：%s", exc)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def snapshot(self, user_id: str, now: float | None = None) -> dict:
        """成长面板数据（E1：经 WS 快照推送，web/monitor.html 消费）。

        阶段为只读派生值（trust + 里程碑 + 天数），不读库写库。
        """
        try:
            gp = self.load(user_id)
        except Exception as exc:
            logger.warning("成长快照失败：%s", exc)
            return {"enabled": False}
        now = now if now is not None else self.clock.now()
        rel = self._rel_cfg()
        gp.stage = self._derive_stage(user_id, gp, now)   # 派生阶段（内存）
        gap = gp.next_stage_gap(rel.get("stage_days", [0, 5, 30, 90]),
                                rel.get("stage_milestones", [0, 1, 2, 3]),
                                now)
        return {
            "enabled": True,
            "stage": gp.stage,
            "stage_name": gp.stage_name,
            "plasticity": round(gp.plasticity(self._curve()), 2),
            "gap": gap,
            "milestones": self.repos.growth.list_milestones(user_id),
            "preferences": gp.preferences.get("like") or {},
            "expressions": {k: v.get("count", 0)
                            for k, v in gp.learned_expressions.items()},
            "stats": {"active_days": gp.active_days(), "turns": gp.turns(),
                      "night_wells": int(gp.stats.get("night_wells") or 0)},
        }

    def _update_stats(self, gp: GrowthProfile, user_text: str, now: float) -> None:
        stats = gp.stats
        stats["turns"] = int(stats.get("turns") or 0) + 1
        # 活跃天数：按自然日去重
        day_key = time.strftime("%Y%m%d", time.localtime(now))
        days: set = set(gp.stats.get("active_day_keys") or [])
        if day_key not in days:
            days.add(day_key)
            stats["active_day_keys"] = list(days)[-400:]
            stats["active_days"] = int(stats.get("active_days") or 0) + 1
        # M6 计数：晚安类互动
        if "晚安" in user_text:
            stats["night_wells"] = int(stats.get("night_wells") or 0) + 1
        gp.stats = stats

    # ------------------------------------------------------------------
    # 阶段机（只读派生：trust + 里程碑 + 天数；引擎不再写关系列）
    # ------------------------------------------------------------------

    def _trust(self, user_id: str) -> float | None:
        """读关系唯一权威（Bond.trust）。无提供者/异常 → None（不满足 S4 门槛）。"""
        if self.trust_provider is None:
            return None
        try:
            t = self.trust_provider(user_id)
        except Exception as exc:   # noqa: BLE001
            logger.debug("trust 读取失败（按无提供者处理）：%s", exc)
            return None
        return None if t is None else float(t)

    def _derive_stage(self, user_id: str, gp: GrowthProfile, now: float) -> int:
        """派生 S1~S4：天数 + 里程碑硬门槛（不可跳跃），S4 附加 trust 门槛。

        纯计算、不写库；回落规则（≥regress_days 不联系降一级，不低于 floor）
        同样在派生层表达。缺 trust_provider 时 S4 附加门槛恒不满足。
        """
        rel = self._rel_cfg()
        if not rel.get("enabled", True):
            return gp.stage
        stage_days = rel.get("stage_days", [0, 5, 30, 90])
        stage_ms = rel.get("stage_milestones", [0, 1, 2, 3])
        floor = int(rel.get("regress_floor", 2))
        urow = self.repos.user.get(user_id) or {}
        last_seen = float(urow.get("last_seen") or 0)
        quiet_days = (now - max(last_seen, gp.stage_since)) / _DAY
        stage = gp.stage
        if stage > floor and quiet_days >= float(rel.get("regress_days", 30)):
            stage = stage - 1
        if stage < S4:
            nxt = stage + 1
            ok_days = gp.active_days() >= int(stage_days[nxt])
            ok_ms = len(gp.milestones) >= int(stage_ms[nxt])
            trust = self._trust(user_id)
            ok_s4 = nxt != S4 or (
                trust is not None
                and trust >= float(rel.get("min_trust_s4", 60)))
            if ok_days and ok_ms and ok_s4:
                stage = nxt
        return stage

    def _apply_derived_stage(self, user_id: str, gp: GrowthProfile,
                             now: float) -> None:
        """计算派生阶段：仅更新内存 gp.stage（供 plasticity/展示）+ 审计日志。

        不写 users.relationship_stage（契约 §3：阶段无自有写者）。
        """
        derived = self._derive_stage(user_id, gp, now)
        if derived != gp.stage:
            self.repos.growth.add_log(
                user_id, "stage_change",
                {"from": gp.stage, "to": derived, "reason": "派生（trust+里程碑+天数）"},
                now)
            gp.stage = derived

    # ------------------------------------------------------------------
    # 里程碑（幂等：growth_milestones UNIQUE + 内存双保险）
    # ------------------------------------------------------------------

    async def record_milestone(self, user_id: str, code: str,
                               now: float | None = None) -> bool:
        """记录里程碑并沉淀为 commitment/story 记忆（幂等）。"""
        if not self.enabled or not (self.cfg.get("milestone", {}) or {}).get(
                "enabled", True):
            return False
        now = now if now is not None else self.clock.now()
        text = MILESTONE_TEXT.get(code, code)
        if not self.repos.growth.add_milestone(user_id, code, text, now):
            return False
        gp = self.load(user_id)
        if code not in gp.milestones:
            gp.milestones.append(code)
        # 沉淀为永不遗忘的 commitment 记忆（M7/M2 为 story 型）
        mtype = MemoryType.COMMITMENT
        if code in ("M2", "M7"):
            mtype = MemoryType.PREFERENCE if code == "M2" else MemoryType.EVENT
        try:
            await self.memory.commit(
                user_id,
                [_mk_candidate(mtype, text, code)],
                now,
            )
        except Exception as exc:   # noqa: BLE001
            logger.warning("里程碑记忆沉淀失败：%s", exc)
        self.repos.growth.add_log(user_id, "milestone",
                                  {"code": code, "text": text}, now)
        logger.info("里程碑达成：%s %s", code, text)
        return True

    async def _check_milestones(self, user_id: str, gp: GrowthProfile,
                                user_text: str, emotion_state: Any,
                                now: float) -> None:
        if not (self.cfg.get("milestone", {}) or {}).get("enabled", True):
            return
        profile = self.repos.profile.get(user_id)

        # M2：记住第一个喜好
        if not gp.has_milestone("M2") and profile.likes:
            await self.record_milestone(user_id, "M2", now)

        # M4：相遇满 30 天
        if not gp.has_milestone("M4") and gp.meet_date \
                and now - gp.meet_date >= 30 * _DAY:
            await self.record_milestone(user_id, "M4", now)

        # M5：昵称授权（必须是明确的授权句式，"我叫阿哲"这类自我介绍不算）
        if not gp.has_milestone("M5") and not gp.nickname_authorized:
            if re.search(r"(?:叫|称呼)(?:我|人家)(?:一个)?.{1,6}(?:吧|呀|就好了?|就行|可以吗)",
                         user_text):
                gp.nickname_authorized = True
                self.repos.user.update(user_id, nickname_authorized=1)
                self.repos.growth.add_log(user_id, "nickname",
                                          {"text": user_text[:50]}, now)
                await self.record_milestone(user_id, "M5", now)

        # M6：第 100 次晚安
        target = int((self.cfg.get("milestone", {}) or {}).get(
            "night_well_target", 100))
        if not gp.has_milestone("M6") and \
                int(gp.stats.get("night_wells") or 0) >= target:
            await self.record_milestone(user_id, "M6", now)

        # M3：主动关心被回应 —— 最近一次成功搭话在 10 分钟内，且本轮是用户回复
        if not gp.has_milestone("M3"):
            try:
                recent = self.repos.proactive.recent(user_id, limit=3)
                for r in recent:
                    if not r.get("suppressed") and \
                            now - float(r.get("fired_at") or 0) <= 600:
                        await self.record_milestone(user_id, "M3", now)
                        break
            except Exception:
                pass

    # ------------------------------------------------------------------
    # B2：偏好演化（可塑性曲线）
    # ------------------------------------------------------------------

    def _evolve_preferences(self, user_id: str, gp: GrowthProfile,
                            user_text: str, emotion_state: Any,
                            now: float) -> None:
        pcfg = self.cfg.get("plasticity", {}) or {}
        if not pcfg:
            return
        # 情绪极性：对话情绪为主（intensity 加权）
        polarity, strength = self._polarity(emotion_state)
        if polarity == 0:
            return
        salience_norm = min(max(strength, 0.0), 1.0)
        delta = (float(pcfg.get("base_step", 0.08))
                 * salience_norm * gp.plasticity(self._curve()) * polarity)

        prefs = gp.preferences.setdefault("like", {})
        # 目标项：文本里提到的既有人格偏好；否则落到「陪伴时光」总评
        touched = [item for item in list(prefs.keys()) + self.persona_like
                   if item and item in user_text]
        if not touched:
            touched = ["陪伴时光"]
        for item in touched:
            cur = float(prefs.get(item, 5.0))
            # 有界：向 5.0 中线演化，偏移受 cap_offset 限制由 clamp(0,10) 表达
            prefs[item] = round(min(max(cur + delta, 0.0), 10.0), 3)
        gp.preferences["like"] = prefs
        self.repos.growth.add_log(
            user_id, "preference",
            {"delta": round(delta, 4), "items": touched,
             "polarity": polarity}, now)

    @staticmethod
    def _polarity(emotion_state: Any) -> tuple[int, float]:
        """情绪极性 (+1/-1/0) 与强度（0~1）。对话情绪优先，life mood 兜底。"""
        if emotion_state is None:
            return 0, 0.0
        mood = getattr(getattr(emotion_state, "mood", None), "value", "")
        intensity = float(getattr(emotion_state, "intensity", 0.0) or 0.0)
        if mood in ("开心", "得意", "调皮"):
            return 1, max(intensity, 0.4)
        if mood in ("委屈", "生气", "吃醋"):
            return -1, max(intensity, 0.4)
        # life 三轴兜底（physio.mood 由 CLI 回灌在 emo_state 之外）
        return 0, intensity

    # ------------------------------------------------------------------
    # B3：高频表达拾取（模仿学习数据源）
    # ------------------------------------------------------------------

    def _learn_expressions(self, user_id: str, gp: GrowthProfile,
                           user_text: str, now: float) -> dict:
        ecfg = self.cfg.get("expression", {}) or {}
        if not ecfg.get("enabled", True):
            return {}
        text = user_text.strip()
        min_len, max_len = 2, int(ecfg.get("max_len", 12))
        # 粗提取：去掉标点后取 2~max_len 的整句（短句/口头禅）
        clean = re.sub(r"[，。！？、\s…~～!?.,]", "", text)
        if not (min_len <= len(clean) <= max_len):
            return {}
        learned = gp.learned_expressions
        entry = learned.get(clean) or {"count": 0, "last_used": 0.0}
        entry["count"] = int(entry["count"]) + 1
        entry["last_used"] = now
        learned[clean] = entry
        newly = None
        if entry["count"] >= int(ecfg.get("min_count", 3)) and entry["count"] - 1 \
                < int(ecfg.get("min_count", 3)):
            newly = clean
            self.repos.growth.add_log(
                user_id, "expression", {"text": clean, "count": entry["count"]},
                now)
            logger.info("学到主人的口头禅：%r（%d 次）", clean, entry["count"])
        gp.learned_expressions = learned
        return {"new": newly}

    # ------------------------------------------------------------------

    def _persist(self, gp: GrowthProfile) -> None:
        """preferences/learned/stats 回写 profiles.extra（stage 列已即时写）。"""
        profile = self.repos.profile.get(gp.user_id)
        profile.extra["preferences_runtime"] = gp.preferences
        profile.extra["learned_expressions"] = gp.learned_expressions
        profile.extra["growth_stats"] = gp.stats
        self.repos.profile.save(profile, time.time())


def _mk_candidate(mtype: MemoryType, text: str, code: str):
    """里程碑 → 记忆候选（永不遗忘，带 story 标签）。"""
    from pipi.capabilities.memory_manager import MemoryCandidate

    return MemoryCandidate(
        type=mtype,
        content=f"（{code}）{text}",
        importance=0.95,
        pinned=True,
        meta={"story": True, "milestone": code},
    )
