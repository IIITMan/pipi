#!/usr/bin/env python3
"""清理污染数据：垃圾身份记忆 + 二白旧称记忆 + 主人昵称重置。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sqlite3

from pipi.infra.config import PROJECT_ROOT

DB = PROJECT_ROOT / "data" / "pipi.db"

# 垃圾记忆（ASR 噪声误抽取）与旧称
GARBAGE = ["他叫谁吗", "他叫谁", "他叫问问你", "问问你"]
STALE = "二白"

conn = sqlite3.connect(DB)
cur = conn.cursor()
now = __import__("time").time()

cur.execute("SELECT id, content FROM memories WHERE deleted_at IS NULL")
rows = cur.fetchall()
removed = []
for mid, content in rows:
    if content in GARBAGE or any(g in content for g in GARBAGE) or STALE in content:
        cur.execute("UPDATE memories SET deleted_at=? WHERE id=?", (now, mid))
        removed.append(content)
conn.commit()

print(f"清理 {len(removed)} 条污染记忆：")
for c in removed:
    print(f"  ✗ {c}")
conn.close()
