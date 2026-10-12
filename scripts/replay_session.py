#!/usr/bin/env python3
"""会话回放 —— 把历史对话在虚拟时钟里重演一遍。

用途：
  - 调 Prompt 之后，回归验证"同一批输入产出是否变好"
  - 演示：一天的使用过程（早问候 → 上班 → 回家 → 深夜）用加速时钟跑完

确定性回放（契约 ENGINE_CONTRACTS §7.3）：
  python3 scripts/replay_session.py --fixture
  注入固定 FixtureLLM（不依赖真实 API），同一批输入回放两次，
  对比输出（除 latency_ms 外）是否逐字节一致 —— Prompt 改动回归全确定。

用法：
    python3 scripts/replay_session.py                 # 回放最近 20 轮
    python3 scripts/replay_session.py --limit 50
    python3 scripts/replay_session.py --export out.json
    python3 scripts/replay_session.py --fixture       # 确定性回放（不调 LLM）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipi.app import build_app
from pipi.infra import Config
from pipi.providers.llm.base import LLMResponse

FIXTURE_REPLY = "[闲聊]这是固定回放回复。皮皮在确定性模式下想你，安安静静等着你说话。"
FIXTURE_JSON = (
    '[{"type":"preference","content":"固定回放记忆项","importance":0.6}]'
)


class FixtureLLM:
    """确定性 LLM：固定回复 + 固定记忆 JSON，回放不依赖任何真实 API。"""

    name = "fixture"

    async def chat(self, messages, *, temperature=0.9, max_tokens=150,
                   use_cheap=False) -> LLMResponse:
        return LLMResponse(text=FIXTURE_REPLY, model="fixture",
                           finish_reason="stop")

    async def complete(self, prompt, *, system=None, use_cheap=False,
                       max_tokens=400, temperature=0.3) -> str:
        return FIXTURE_JSON

    async def stream(self, messages, *, temperature=0.9, max_tokens=150,
                     use_cheap=False):
        # 分两段 yield，覆盖"缓冲后判定"路径
        for piece in (FIXTURE_REPLY[:10], FIXTURE_REPLY[10:]):
            yield piece

    def is_available(self) -> bool:
        return True


async def replay(uid: str, limit: int, export: str) -> None:
    cfg = Config.default()
    app = build_app(cfg, sim=True)

    turns = [t for t in app.repos.turn.recent(uid, limit=limit)
             if t.role.value == "user"]
    if not turns:
        print(f"  {uid} 还没有历史对话。先跑 scripts/seed_demo_data.py")
        app.close()
        return

    print(f"回放 {uid} 的 {len(turns)} 轮对话\n" + "─" * 60)
    dump = []
    for i, t in enumerate(turns, 1):
        # 用独立会话历史避免污染：只喂这一句，看皮皮怎么答
        res = await app.orchestrator.handle_turn(uid, t.content, persist=False)
        await asyncio.sleep(0.05)
        dump.append({
            "turn": i, "user": t.content,
            "pipi": [b.text for b in res.bubbles],
            "memory_hits": res.memories_used,
            "mood": res.emotion_state.mood.value if res.emotion_state else "",
            "affection": round(res.emotion_state.affection, 1) if res.emotion_state else 0,
            "latency_ms": res.latency_ms,
        })
        hits = f"  [想起 {len(res.memories_used)} 条]" if res.memories_used else ""
        print(f"\n  你   > {t.content}")
        for b in res.bubbles:
            print(f"  皮皮 > {b.text}")
        if hits:
            print(f"       {hits}")

    print("\n" + "─" * 60)
    print(f"完成。共 {len(turns)} 轮")

    if export:
        Path(export).write_text(
            json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已导出 → {export}")

    app.close()


def _dump_without_latency(dump: list[dict]) -> str:
    """去掉 latency_ms（真实耗时，与 LLM 输出无关）后规范化序列化。"""
    clean = [{k: v for k, v in d.items() if k != "latency_ms"} for d in dump]
    return json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True)


async def replay_deterministic(uid: str, limit: int) -> None:
    """确定性回放（契约 ENGINE_CONTRACTS §7.3）：fixture LLM 跑两遍，逐字节对比。"""
    cfg = Config.default()
    probe = build_app(cfg, sim=True)
    turns = [t for t in probe.repos.turn.recent(uid, limit=limit)
             if t.role.value == "user"]
    probe.close()
    if not turns:
        print(f"  {uid} 还没有历史对话。先跑 scripts/seed_demo_data.py")
        return

    print(f"确定性回放 {uid} 的 {len(turns)} 轮（FixtureLLM × 2 遍）\n" + "─" * 60)
    runs: list[str] = []
    for rnd in (1, 2):
        app = build_app(cfg, sim=True, llm_override=FixtureLLM())
        dump = []
        for t in turns:
            res = await app.orchestrator.handle_turn(uid, t.content, persist=False)
            dump.append({
                "turn": t.content,
                "pipi": [b.text for b in res.bubbles],
                "memory_hits": res.memories_used,
                "mood": res.emotion_state.mood.value if res.emotion_state else "",
                "affection": round(res.emotion_state.affection, 1)
                if res.emotion_state else 0,
            })
        app.close()
        runs.append(_dump_without_latency(dump))
        print(f"  第 {rnd} 遍：{len(dump)} 轮完成")

    identical = runs[0] == runs[1]
    print("─" * 60)
    if identical:
        print("  [通过] 两次回放逐字节一致（fixture LLM，确定性成立）")
    else:
        for i, (a, b) in enumerate(zip(runs[0].splitlines(), runs[1].splitlines())):
            if a != b:
                print(f"  首次差异 @ 行 {i}：\n    A={a}\n    B={b}")
                break
        print("  [失败] 两次回放不一致！")
    print(f"  样本输出：{json.loads(runs[0])[0]['pipi']}")
    return


def main() -> None:
    ap = argparse.ArgumentParser(description="皮皮会话回放")
    ap.add_argument("--user", default="owner")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--export", default="", help="导出 JSON 路径")
    ap.add_argument("--fixture", action="store_true",
                    help="确定性回放：注入 FixtureLLM，跑两遍并逐字节对比")
    args = ap.parse_args()
    if args.fixture:
        asyncio.run(replay_deterministic(args.user, args.limit))
    else:
        asyncio.run(replay(args.user, args.limit, args.export))


if __name__ == "__main__":
    main()
