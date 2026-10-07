#!/usr/bin/env python3
"""会话回放 —— 把历史对话在虚拟时钟里重演一遍。

用途：
  - 调 Prompt 之后，回归验证"同一批输入产出是否变好"
  - 演示：一天的使用过程（早问候 → 上班 → 回家 → 深夜）用加速时钟跑完

用法：
    python3 scripts/replay_session.py                 # 回放最近 20 轮
    python3 scripts/replay_session.py --limit 50
    python3 scripts/replay_session.py --export out.json
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


def main() -> None:
    ap = argparse.ArgumentParser(description="皮皮会话回放")
    ap.add_argument("--user", default="owner")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--export", default="", help="导出 JSON 路径")
    args = ap.parse_args()
    asyncio.run(replay(args.user, args.limit, args.export))


if __name__ == "__main__":
    main()
