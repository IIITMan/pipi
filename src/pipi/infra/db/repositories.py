"""数据仓储层 —— 领域对象 ↔ SQLite 的转换。

每个 Repo 只负责一张表，接口保持窄，方便替换存储。
embedding 用 float32 打包成 BLOB，体积是 JSON 的 1/4。
"""

from __future__ import annotations

import json
import logging
import struct
from typing import Any

from pipi.domain.dialogue import DialogueTurn, Role
from pipi.domain.emotion import EmotionState, Mood
from pipi.domain.memory import (
    DecayReport,
    MemoryItem,
    MemoryLayer,
    MemoryType,
)
from pipi.domain.profile import UserProfile
from pipi.infra.db.connection import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 向量打包
# ---------------------------------------------------------------------------

def pack_embedding(vec: list[float] | None) -> bytes | None:
    if vec is None:
        return None
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_embedding(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# ---------------------------------------------------------------------------
# 用户 / 档案
# ---------------------------------------------------------------------------

class UserRepo:
    def __init__(self, db: Database):
        self.db = db

    def ensure(self, user_id: str, now: float) -> None:
        """不存在则创建。"""
        if not self.exists(user_id):
            with self.db.conn as conn:
                conn.execute(
                    "INSERT INTO users (id, created_at, is_owner, met_at, last_seen, affection) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, now, 0, now, now, 500.0),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO emotion_states "
                    "(user_id, mood, intensity, affection, companion_need, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, Mood.CALM.value, 0.3, 500.0, 0.0, now),
                )

    def exists(self, user_id: str) -> bool:
        return self.db.query_one("SELECT 1 FROM users WHERE id=?", (user_id,)) is not None

    def get(self, user_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
        return dict(row) if row else None

    def update(self, user_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db.conn as conn:
            conn.execute(
                f"UPDATE users SET {cols} WHERE id=?",
                (*fields.values(), user_id),
            )

    def all_users(self) -> list[str]:
        return [r["id"] for r in self.db.query("SELECT id FROM users")]


class ProfileRepo:
    def __init__(self, db: Database):
        self.db = db

    def get(self, user_id: str) -> UserProfile:
        row = self.db.query_one("SELECT * FROM profiles WHERE user_id=?", (user_id,))
        if not row:
            return UserProfile(user_id=user_id)
        return UserProfile(
            user_id=user_id,
            nickname=row["nickname"] or "",
            call_name=row["call_name"] or "",
            city=row["city"] or "",
            living=row["living"] or "",
            job=row["job"] or "",
            likes=json.loads(row["likes"] or "[]"),
            dislikes=json.loads(row["dislikes"] or "[]"),
            rivals=json.loads(row["rivals"] or "[]"),
            important_dates=json.loads(row["important_dates"] or "{}"),
            extra=json.loads(row["extra"] or "{}"),
        )

    def save(self, p: UserProfile, now: float) -> None:
        with self.db.conn as conn:
            conn.execute(
                """
                INSERT INTO profiles
                  (user_id, nickname, call_name, city, living, job,
                   likes, dislikes, rivals, important_dates, extra, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                  nickname=excluded.nickname,
                  call_name=excluded.call_name,
                  city=excluded.city,
                  living=excluded.living,
                  job=excluded.job,
                  likes=excluded.likes,
                  dislikes=excluded.dislikes,
                  rivals=excluded.rivals,
                  important_dates=excluded.important_dates,
                  extra=excluded.extra,
                  updated_at=excluded.updated_at
                """,
                (
                    p.user_id, p.nickname, p.call_name, p.city, p.living, p.job,
                    json.dumps(p.likes, ensure_ascii=False),
                    json.dumps(p.dislikes, ensure_ascii=False),
                    json.dumps(p.rivals, ensure_ascii=False),
                    json.dumps(p.important_dates, ensure_ascii=False),
                    json.dumps(p.extra, ensure_ascii=False),
                    now,
                ),
            )


# ---------------------------------------------------------------------------
# 记忆
# ---------------------------------------------------------------------------

class MemoryRepo:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, m: MemoryItem) -> str:
        with self.db.conn as conn:
            conn.execute(
                """
                INSERT INTO memories
                  (id, user_id, layer, type, content, importance, salience,
                   decay_lambda, stability, access_count, last_access_at,
                   created_at, updated_at, pinned, source_turn_id,
                   embedding, embed_model, meta, deleted_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    m.id, m.user_id, m.layer.value, m.type.value, m.content,
                    m.importance, m.salience(m.updated_at or m.created_at),
                    m.decay_lambda, m.stability, m.access_count,
                    m.last_access_at or m.created_at, m.created_at, m.updated_at,
                    1 if m.pinned else 0, m.source_turn_id,
                    pack_embedding(m.embedding), m.embed_model,
                    json.dumps(m.meta, ensure_ascii=False), m.deleted_at,
                ),
            )
        return m.id

    def update(self, m: MemoryItem) -> None:
        with self.db.conn as conn:
            conn.execute(
                """
                UPDATE memories SET
                  content=?, importance=?, salience=?, decay_lambda=?,
                  stability=?, access_count=?, last_access_at=?, updated_at=?,
                  pinned=?, layer=?, type=?, embedding=?, meta=?
                WHERE id=?
                """,
                (
                    m.content, m.importance, m.salience(m.updated_at),
                    m.decay_lambda, m.stability, m.access_count,
                    m.last_access_at, m.updated_at, 1 if m.pinned else 0,
                    m.layer.value, m.type.value, pack_embedding(m.embedding),
                    json.dumps(m.meta, ensure_ascii=False), m.id,
                ),
            )

    def get(self, memory_id: str) -> MemoryItem | None:
        row = self.db.query_one("SELECT * FROM memories WHERE id=?", (memory_id,))
        return self._row_to_item(row) if row else None

    def list_live(self, user_id: str, limit: int = 1000) -> list[MemoryItem]:
        """所有未删除的记忆。"""
        rows = self.db.query(
            "SELECT * FROM memories WHERE user_id=? AND deleted_at IS NULL "
            "ORDER BY salience DESC LIMIT ?",
            (user_id, limit),
        )
        return [self._row_to_item(r) for r in rows]

    def list_pinned(self, user_id: str) -> list[MemoryItem]:
        rows = self.db.query(
            "SELECT * FROM memories WHERE user_id=? AND pinned=1 AND deleted_at IS NULL",
            (user_id,),
        )
        return [self._row_to_item(r) for r in rows]

    def soft_delete(self, memory_id: str, now: float) -> None:
        with self.db.conn as conn:
            conn.execute(
                "UPDATE memories SET deleted_at=? WHERE id=?", (now, memory_id)
            )

    def hard_delete(self, memory_id: str) -> None:
        with self.db.conn as conn:
            conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))

    def purge_deleted_older_than(self, cutoff: float) -> int:
        with self.db.conn as conn:
            cur = conn.execute(
                "DELETE FROM memories WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff,),
            )
            return cur.rowcount

    def batch_update(self, items: list[MemoryItem]) -> None:
        """批量更新（衰减巡检用，一个事务提交）。"""
        if not items:
            return
        with self.db.conn as conn:
            for m in items:
                conn.execute(
                    "UPDATE memories SET salience=?, decay_lambda=?, stability=?, "
                    "access_count=?, last_access_at=?, updated_at=? WHERE id=?",
                    (
                        m.salience(m.updated_at), m.decay_lambda, m.stability,
                        m.access_count, m.last_access_at, m.updated_at, m.id,
                    ),
                )

    def log_access(self, memory_id: str, user_id: str, turn_id: str | None,
                   score: float, now: float) -> None:
        with self.db.conn as conn:
            conn.execute(
                "INSERT INTO memory_access_log (memory_id, user_id, turn_id, score, accessed_at) "
                "VALUES (?,?,?,?,?)",
                (memory_id, user_id, turn_id, score, now),
            )

    def count(self, user_id: str, include_deleted: bool = False) -> int:
        sql = "SELECT COUNT(*) AS c FROM memories WHERE user_id=?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        row = self.db.query_one(sql, (user_id,))
        return row["c"] if row else 0

    @staticmethod
    def _row_to_item(row) -> MemoryItem:
        item = MemoryItem(
            id=row["id"],
            user_id=row["user_id"],
            type=MemoryType(row["type"]),
            layer=MemoryLayer(row["layer"]),
            content=row["content"],
            importance=row["importance"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_access_at=row["last_access_at"],
            access_count=row["access_count"],
            stability=row["stability"],
            pinned=bool(row["pinned"]),
            source_turn_id=row["source_turn_id"],
            embedding=unpack_embedding(row["embedding"]),
            embed_model=row["embed_model"],
            meta=json.loads(row["meta"] or "{}"),
            deleted_at=row["deleted_at"],
        )
        # 回填持久化的 λ（写入时固化；access_count 变化后由 decay_pass 重算）
        item.decay_lambda = row["decay_lambda"]
        return item

    def list_story(self, user_id: str, limit: int = 10) -> list[str]:
        """story 标签的记忆（成长/联想链用），按显著性排序。"""
        rows = self.db.query(
            "SELECT content FROM memories "
            "WHERE user_id=? AND deleted_at IS NULL "
            "AND json_extract(meta, '$.story')=1 "
            "ORDER BY salience DESC LIMIT ?",
            (user_id, limit),
        )
        return [r["content"] for r in rows]

    def mark_story(self, memory_id: str, now: float) -> None:
        """给一条记忆打上 story 标签（成长故事沉淀）。"""
        item = self.get(memory_id)
        if not item:
            return
        meta = dict(item.meta or {})
        meta["story"] = True
        item.meta = meta
        item.pinned = True
        item.updated_at = now
        self.update(item)

    def stats(self, user_id: str) -> dict[str, Any]:
        """相处统计：互动轮次 / 相识天数 / 里程碑数。"""
        import time as _t

        row = self.db.query_one(
            "SELECT total_turns, met_at FROM users WHERE id=?", (user_id,))
        turns = int((row or {}).get("total_turns") or 0)
        met_at = float((row or {}).get("met_at") or 0)
        days = int((_t.time() - met_at) / _DAY) if met_at else 0
        ms = self.db.query_one(
            "SELECT COUNT(*) AS c FROM growth_milestones WHERE user_id=?",
            (user_id,))
        return {
            "turns": turns,
            "days": max(0, days),
            "milestones": int((ms or {}).get("c") or 0),
        }

    def stats(self, user_id: str) -> dict[str, Any]:
        """相处统计：互动轮次 / 相识天数 / 里程碑数。"""
        row = self.db.query_one(
            "SELECT total_turns, met_at FROM users WHERE id=?", (user_id,))
        turns = int((row or {}).get("total_turns") or 0)
        met_at = float((row or {}).get("met_at") or 0)
        return {
            "turns": turns,
            "days": 0,
            "milestones": 0,
            "stage": 1,
        }


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------

class TurnRepo:
    def __init__(self, db: Database):
        self.db = db

    def save(self, t: DialogueTurn) -> None:
        with self.db.conn as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO turns
                  (id, user_id, session_id, turn_index, role, content, created_at,
                   asr_emotion, vision_snapshot, emotion_state, latency_ms,
                   tokens_in, tokens_out, trace_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    t.id, t.user_id, t.session_id, t.turn_index, t.role.value,
                    t.content, t.created_at, t.asr_emotion,
                    json.dumps(t.vision_snapshot, ensure_ascii=False) if t.vision_snapshot else None,
                    json.dumps(t.emotion_state, ensure_ascii=False) if t.emotion_state else None,
                    t.latency_ms, t.tokens_in, t.tokens_out, t.trace_id,
                ),
            )

    def recent(self, user_id: str, limit: int = 20) -> list[DialogueTurn]:
        rows = self.db.query(
            "SELECT * FROM turns WHERE user_id=? AND role!='system_event' "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        out = []
        for r in reversed(rows):
            out.append(DialogueTurn(
                id=r["id"], user_id=r["user_id"], session_id=r["session_id"],
                turn_index=r["turn_index"], role=Role(r["role"]),
                content=r["content"], created_at=r["created_at"],
                asr_emotion=r["asr_emotion"],
                latency_ms=r["latency_ms"], trace_id=r["trace_id"],
            ))
        return out

    def by_session(self, session_id: str) -> list[DialogueTurn]:
        rows = self.db.query(
            "SELECT * FROM turns WHERE session_id=? ORDER BY turn_index",
            (session_id,),
        )
        return [DialogueTurn(
            id=r["id"], user_id=r["user_id"], session_id=r["session_id"],
            turn_index=r["turn_index"], role=Role(r["role"]),
            content=r["content"], created_at=r["created_at"],
            asr_emotion=r["asr_emotion"],
            vision_snapshot=json.loads(r["vision_snapshot"]) if r["vision_snapshot"] else None,
            emotion_state=json.loads(r["emotion_state"]) if r["emotion_state"] else None,
            latency_ms=r["latency_ms"], tokens_in=r["tokens_in"],
            tokens_out=r["tokens_out"], trace_id=r["trace_id"],
        ) for r in rows]

    def count(self, user_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS c FROM turns WHERE user_id=? AND role='user'", (user_id,)
        )
        return row["c"] if row else 0


# ---------------------------------------------------------------------------
# 情感
# ---------------------------------------------------------------------------

class EmotionRepo:
    def __init__(self, db: Database):
        self.db = db

    def load(self, user_id: str, now: float) -> EmotionState:
        row = self.db.query_one("SELECT * FROM emotion_states WHERE user_id=?", (user_id,))
        if not row:
            return EmotionState(updated_at=now)
        return EmotionState(
            mood=Mood(row["mood"]) if row["mood"] in Mood._value2member_map_
            else Mood.CALM,
            intensity=row["intensity"],
            affection=row["affection"],
            companion_need=row["companion_need"],
            last_interaction=row["last_interaction"] or 0.0,
            updated_at=row["updated_at"] or now,
            is_attachment=bool(row["is_attachment"])
            if "is_attachment" in row.keys() else True,
        )

    def save(self, user_id: str, s: EmotionState, now: float, trigger: str = "") -> None:
        with self.db.conn as conn:
            conn.execute(
                """
                INSERT INTO emotion_states
                  (user_id, mood, intensity, affection, companion_need,
                   last_interaction, updated_at, is_attachment)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                  mood=excluded.mood, intensity=excluded.intensity,
                  affection=excluded.affection,
                  companion_need=excluded.companion_need,
                  last_interaction=excluded.last_interaction,
                  updated_at=excluded.updated_at,
                  is_attachment=excluded.is_attachment
                """,
                (user_id, s.mood.value, s.intensity, s.affection,
                 s.companion_need, s.last_interaction, now,
                 1 if s.is_attachment else 0),
            )
            if trigger:
                conn.execute(
                    "INSERT INTO emotion_history "
                    "(user_id, mood, intensity, affection, trigger, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, s.mood.value, s.intensity, s.affection, trigger, now),
                )

    def history(self, user_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT mood, intensity, affection, trigger, created_at FROM emotion_history "
            "WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(r) for r in reversed(rows)]


# ---------------------------------------------------------------------------
# 主动搭话日志
# ---------------------------------------------------------------------------

class ProactiveRepo:
    def __init__(self, db: Database):
        self.db = db

    def log(self, user_id: str, trigger: str, content: str, now: float,
            suppressed: bool = False, reason: str = "") -> None:
        with self.db.conn as conn:
            conn.execute(
                "INSERT INTO proactive_log "
                "(user_id, trigger, content, fired_at, suppressed, suppress_reason) "
                "VALUES (?,?,?,?,?,?)",
                (user_id, trigger, content, now, 1 if suppressed else 0, reason),
            )

    def count_today(self, user_id: str, day_start: float) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS c FROM proactive_log "
            "WHERE user_id=? AND fired_at>=? AND suppressed=0",
            (user_id, day_start),
        )
        return row["c"] if row else 0

    def last_fired(self, user_id: str, trigger: str) -> float | None:
        row = self.db.query_one(
            "SELECT MAX(fired_at) AS t FROM proactive_log "
            "WHERE user_id=? AND trigger=? AND suppressed=0",
            (user_id, trigger),
        )
        return row["t"] if row and row["t"] else None

    def recent(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM proactive_log WHERE user_id=? ORDER BY fired_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 衰减报告
# ---------------------------------------------------------------------------

class DecayRepo:
    def __init__(self, db: Database):
        self.db = db

    def save(self, user_id: str, r: DecayReport, now: float) -> None:
        with self.db.conn as conn:
            conn.execute(
                "INSERT INTO decay_reports "
                "(user_id, checked, decayed, forgotten, pinned_kept, "
                "avg_before, avg_after, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, r.checked, r.decayed, r.forgotten, r.pinned_kept,
                 r.avg_salience_before, r.avg_salience_after, now),
            )

    def history(self, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM decay_reports WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(r) for r in reversed(rows)]


# ---------------------------------------------------------------------------
# 人脸身份
# ---------------------------------------------------------------------------

class FaceRepo:
    def __init__(self, db: Database):
        self.db = db

    def enroll(self, person_id: str, user_id: str | None, display_name: str,
               embedding: list[float] | None, model: str, now: float,
               is_owner: bool = False) -> None:
        with self.db.conn as conn:
            conn.execute(
                """
                INSERT INTO face_identities
                  (person_id, user_id, display_name, is_owner, embedding,
                   embed_model, enrolled_at, sample_count)
                VALUES (?,?,?,?,?,?,?,1)
                ON CONFLICT(person_id) DO UPDATE SET
                  embedding=excluded.embedding,
                  sample_count=face_identities.sample_count+1,
                  enrolled_at=excluded.enrolled_at
                """,
                (person_id, user_id, display_name, 1 if is_owner else 0,
                 pack_embedding(embedding), model, now),
            )

    def all_faces(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM face_identities")
        out = []
        for r in rows:
            d = dict(r)
            d["embedding"] = unpack_embedding(r["embedding"])
            out.append(d)
        return out

    def owner(self) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM face_identities WHERE is_owner=1 LIMIT 1")
        if not row:
            return None
        d = dict(row)
        d["embedding"] = unpack_embedding(row["embedding"])
        return d

    def delete(self, person_id: str) -> None:
        with self.db.conn as conn:
            conn.execute("DELETE FROM face_identities WHERE person_id=?", (person_id,))


# ---------------------------------------------------------------------------
# 键值
# ---------------------------------------------------------------------------

class KVRepo:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.query_one("SELECT v FROM kv_store WHERE k=?", (key,))
        return row["v"] if row else default

    def set(self, key: str, value: str, now: float) -> None:
        with self.db.conn as conn:
            conn.execute(
                "INSERT INTO kv_store (k, v, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                (key, value, now),
            )


class GrowthRepo:
    """成长系统仓储：里程碑 + 演化日志（GROWTH_DESIGN §3）。"""

    def __init__(self, db: Database):
        self.db = db

    # ---- 里程碑 ----

    def has_milestone(self, user_id: str, code: str) -> bool:
        return self.db.query_one(
            "SELECT 1 FROM growth_milestones WHERE user_id=? AND code=?",
            (user_id, code)) is not None

    def add_milestone(self, user_id: str, code: str, text: str, now: float) -> bool:
        """幂等写入：已存在静默返回 False，不刷警告。"""
        import logging as _log

        try:
            with self.db.conn as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO growth_milestones "
                    "(user_id, code, text, created_at) VALUES (?,?,?,?)",
                    (user_id, code, text, now),
                )
            return cur.rowcount > 0
        except Exception as exc:
            _log.getLogger(__name__).warning(
                "add_milestone(%s, %s) 失败：%s", user_id, code, exc)
            return False

    def list_milestones(self, user_id: str) -> list[dict]:
        rows = self.db.query(
            "SELECT code, text, created_at FROM growth_milestones "
            "WHERE user_id=? ORDER BY created_at",
            (user_id,),
        )
        return [dict(r) for r in rows]

    # ---- 演化日志 ----

    def add_log(self, user_id: str, kind: str, detail: dict, now: float) -> None:
        with self.db.conn as conn:
            conn.execute(
                "INSERT INTO growth_log (user_id, kind, detail, created_at) "
                "VALUES (?,?,?,?)",
                (user_id, kind, json.dumps(detail, ensure_ascii=False), now),
            )

    def list_log(self, user_id: str, limit: int = 50) -> list[dict]:
        rows = self.db.query(
            "SELECT kind, detail, created_at FROM growth_log "
            "WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        out = []
        for r in rows:
            try:
                detail = json.loads(r["detail"])
            except (json.JSONDecodeError, TypeError):
                detail = {}
            out.append({"kind": r["kind"], "detail": detail,
                        "created_at": r["created_at"]})
        return out
