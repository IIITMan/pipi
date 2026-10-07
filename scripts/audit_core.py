#!/usr/bin/env python3
"""核心链路走查 —— 对话 / 情感 / 记忆 三部分的端到端冒烟审计。

用法：
    python3 scripts/audit_core.py

模拟一段真实的相处过程，验证：
  1. 对话链路：handle_turn 全链路（降级标记、气泡拆分、记忆命中注入）
  2. 情感链路：好感度轨迹、mood 迁移、久别重逢、intensity 回落
  3. 记忆链路：规则抽取（身份/喜好/约定）、召回排序、遗忘巡检、合并去重

全部在 --sim 等价的 mock 环境下跑，无需任何外部依赖。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipi.app import build_app
from pipi.domain.emotion import EmotionState, Mood
from pipi.infra.clock import FakeClock

PASS, FAIL = "✓", "✗"
fails: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        fails.append(label)


def section(title: str) -> None:
    print(f"\n{'─' * 60}\n  {title}\n{'─' * 60}")


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pipi-audit-"))
    from pipi.infra.config import Config
    cfg = Config.default()
    cfg.set("memory.db_path", str(tmp / "audit.db"))
    clock = FakeClock(tz_name="Asia/Shanghai")
    app = build_app(cfg, sim=True, clock=clock)
    orch = app.orchestrator
    uid = "owner"

    # ======================================================================
    section("1. 对话链路 —— 多轮 handle_turn 全链路")
    # ======================================================================

    script = [
        ("你好", "首轮回合"),
        ("我叫阿哲", "自我介绍（应抽出身份记忆）"),
        ("我喜欢喝美式", "表达喜好（应抽出偏好记忆）"),
        ("我今天心情不太好", "负面情绪（应触发关心向 mood）"),
        ("我今天跟小美聊天聊得可开心了", "提别人（傲娇人格应吃醋）"),
    ]

    last_affection = None
    memories_seen: list[str] = []
    for i, (text, desc) in enumerate(script, 1):
        r = await orch.handle_turn(uid, text)
        state = r.emotion_state
        print(f"\n  [{i}] {desc}")
        print(f"      你: {text}")
        print(f"      皮: {r.text[:60]}")
        print(f"      状态: mood={state.mood.value} 强度={state.intensity:.2f} "
              f"好感={state.affection:.1f}({state.affection_level})")
        check(not r.degraded, f"回合{i} 未降级")
        check(len(r.bubbles) >= 1, f"回合{i} 有气泡输出")
        check(r.latency_ms >= 0, f"回合{i} 延迟记录")
        if last_affection is not None:
            check(state.affection >= last_affection - 5,
                  f"回合{i} 好感度无异常暴跌", f"{last_affection:.1f}→{state.affection:.1f}")
        last_affection = state.affection
        memories_seen.extend(r.memories_used)
        # 等异步记忆写入完成
        await asyncio.sleep(0.05)

    # 后续回合应命中前面写入的记忆
    r = await orch.handle_turn(uid, "我叫什么名字来着？")
    print(f"\n  [6] 记忆回注验证")
    print(f"      命中记忆: {r.memories_used}")
    check(any("阿哲" in m for m in r.memories_used),
          "身份记忆被召回注入 Prompt", str(r.memories_used))

    # ======================================================================
    section("2. 情感链路 —— 好感度轨迹 / 久别重逢 / intensity 回落")
    # ======================================================================

    st = orch.get_state(uid)
    before = st.affection

    # 2a. 冷却回落
    st.intensity = 0.9
    st.cool_down()
    check(abs(st.intensity - max(0.3, 0.9 * 0.85)) < 1e-6,
          "cool_down: 强度 ×0.85 回落", f"0.9→{st.intensity:.2f}")
    st.intensity = 0.32
    st.mood = Mood.HAPPY
    st.cool_down()
    check(st.intensity == 0.3 and st.mood == Mood.CALM,
          "cool_down: 跌破地板 → 回到平静")

    # 2b. 久别重逢（快进 30 小时）
    orch.emotion_repo.save(uid, st, clock.now(), trigger="audit")
    st.last_interaction = clock.now()
    clock.advance(hours=30)
    r = await orch.handle_turn(uid, "我回来啦")
    st2 = r.emotion_state
    check(st2.mood == Mood.HAPPY, "久别重逢(≥24h) → warm_return → 开心")
    check(st2.affection >= 300.0, "好感度不低于认主底价 300",
          f"{st2.affection:.1f}")

    # 2c. 超长分离不归零（快进 30 天）
    st2.last_interaction = clock.now()
    clock.advance(hours=30 * 24)
    r = await orch.handle_turn(uid, "好久没见了")
    st3 = r.emotion_state
    check(st3.affection >= 300.0, "30 天不见好感度仍 ≥ 底价",
          f"{st3.affection:.1f}")
    check(st3.affection_level != "陌生", "关系不会退回陌生")

    # 2d. 陪伴需求累积
    check(st3.companion_need <= 1.0, "陪伴需求有上限 1.0",
          f"{st3.companion_need:.2f}")

    # ======================================================================
    section("3. 记忆链路 —— 抽取 / 召回排序 / 遗忘巡检 / 合并")
    # ======================================================================

    mem = app.memory
    now = clock.now()

    # 3a. 规则抽取验证
    ext = await mem.extract("我答应你，周末一定早点睡", "哼，记住了", orch.get_profile(uid))
    kinds = [c.type.value for c in ext.candidates]
    print(f"      约定句式抽取: {kinds}")
    check(any(k == "commitment" for k in kinds), "「我答应你」→ 约定记忆")
    check(all(not c.content.startswith("我") or "约定" in c.content or len(c.content) > 2
              for c in ext.candidates), "抽取内容非空壳")

    ext2 = await mem.extract("你比我家布丁可爱多了", "哼", orch.get_profile(uid))
    bad = [c.content for c in ext2.candidates if c.content in ("多了", "爱多了", "")]
    check(not bad, "「可爱多了」不再抽出垃圾记忆（否定断言生效）")

    # 3b. 提交与合并去重
    ids1 = await mem.commit(uid, ext.candidates, now)
    ext3 = await mem.extract("我答应你，周末一定早点睡", "哦", orch.get_profile(uid))
    ids2 = await mem.commit(uid, ext3.candidates, now)
    rows = app.repos.memory.list_live(uid)
    commitments = [m for m in rows if m.type.value == "commitment"]
    check(len(commitments) <= 1, "重复约定被合并而不是新增",
          f"commitment 共 {len(commitments)} 条")

    # 3c. 召回排序：身份应排在闲聊前
    recalled = await mem.recall(uid, "我是谁", now, top_k=5)
    if len(recalled) >= 2:
        types = [m.item.type.value for m in recalled]
        print(f"      召回顺序: {types}")
        if "identity" in types and "chitchat" in types:
            check(types.index("identity") < types.index("chitchat"),
                  "身份记忆排在闲聊之前")

    # 3d. 遗忘巡检：闲聊 30 天后应被淘汰，身份永不
    # （注意：规则抽取刻意不产 chitchat —— 这里手动写入一条，验证淘汰路径。
    #   should_forget 对 access_count >= 2 的记忆豁免，所以必须用"从未被想起"的）
    from pipi.domain.memory import MemoryItem, MemoryLayer, MemoryType
    chitchat = MemoryItem(
        id="",
        user_id=uid,
        type=MemoryType.CHITCHAT,
        layer=MemoryLayer.L2,
        content="他今天说天气不错",
        importance=0.3,
        created_at=now,
        last_access_at=now,
    )
    await app.memory_store.write(chitchat)

    before_types = [m.type.value for m in app.repos.memory.list_live(uid)]
    clock.advance(hours=30 * 24)
    report = await mem.decay(uid, clock.now())
    after = app.repos.memory.list_live(uid)
    survivors = [m.type.value for m in after]
    forgotten = [t for t in before_types if t not in survivors]
    print(f"      30 天后 — 存活: {survivors}")
    print(f"      30 天后 — 遗忘: {forgotten}")
    print(f"      巡检统计: {report.summary()}")
    check("identity" in survivors, "身份记忆永不遗忘")
    check("commitment" in survivors, "约定记忆永不遗忘")
    check("chitchat" not in survivors, "闲聊被遗忘")
    check(report.forgotten >= 1, "巡检至少淘汰 1 条", f"forgotten={report.forgotten}")

    # ======================================================================
    section("4. 改名一致性 —— 皮皮 / pipi 全链路")
    # ======================================================================

    import os
    os.environ["PIPI_TEST__FOO"] = "bar"
    from pipi.infra.config import _apply_env_overrides
    merged = _apply_env_overrides({"test": {}})
    check(merged.get("test", {}).get("foo") == "bar",
          "环境变量前缀 PIPI_ 生效", f"{merged.get('test')}")
    del os.environ["PIPI_TEST__FOO"]

    from pipi.infra.config import Config
    cfg = Config.load(Path("configs/default.yaml"))
    check("pipi" in str(cfg.get("memory.db_path", "")),
          "数据库路径为 data/pipi.db", str(cfg.get("memory.db_path")))
    check(cfg.get("app.name") == "皮皮", "应用名为皮皮", str(cfg.get("app.name")))

    # ======================================================================
    print(f"\n{'═' * 60}")
    if fails:
        print(f"结果：{len(fails)} 项未通过：")
        for f in fails:
            print(f"  {FAIL} {f}")
        return 1
    print("结果：核心链路走查全部通过 ✓")
    print(f"{'═' * 60}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
