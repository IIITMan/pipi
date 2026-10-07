-- ============================================================
-- 二白 · 数据库 Schema (SQLite)
-- ============================================================
-- 设计要点：
--   1. 所有表以 user_id 为隔离维度（多用户/陌生人场景）
--   2. memories 表用 REAL 存 salience 便于排序索引
--   3. embedding 存 BLOB (float32 打包)，无向量时该列为 NULL
--   4. 软删除（deleted_at）而非物理删除，保留可回溯性
-- ============================================================

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

-- ---------------- 用户 ----------------
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    created_at      REAL NOT NULL,
    is_owner        INTEGER DEFAULT 0,
    met_at          REAL DEFAULT 0,
    last_seen       REAL DEFAULT 0,
    total_turns     INTEGER DEFAULT 0,
    affection       REAL DEFAULT 500,
    persona_id      TEXT DEFAULT 'tsundere'
);

-- ---------------- L1 身份档案 ----------------
CREATE TABLE IF NOT EXISTS profiles (
    user_id         TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    nickname        TEXT DEFAULT '',
    call_name       TEXT DEFAULT '',
    city            TEXT DEFAULT '',
    living          TEXT DEFAULT '',
    job             TEXT DEFAULT '',
    likes           TEXT DEFAULT '[]',
    dislikes        TEXT DEFAULT '[]',
    rivals          TEXT DEFAULT '[]',
    important_dates TEXT DEFAULT '{}',
    extra           TEXT DEFAULT '{}',
    updated_at      REAL DEFAULT 0
);

-- ---------------- 人格 ----------------
CREATE TABLE IF NOT EXISTS personas (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    spec_json       TEXT NOT NULL,
    is_builtin      INTEGER DEFAULT 1,
    updated_at      REAL DEFAULT 0
);

-- ---------------- 记忆（核心表） ----------------
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    layer           TEXT NOT NULL DEFAULT 'L2',
    type            TEXT NOT NULL,
    content         TEXT NOT NULL,
    importance      REAL NOT NULL DEFAULT 0.5,
    -- 显著性快照（写入/强化/衰减时更新，便于 SQL 排序；精确值仍由算法实时算）
    salience        REAL NOT NULL DEFAULT 0.5,
    decay_lambda    REAL NOT NULL DEFAULT 0.0,
    stability       REAL NOT NULL DEFAULT 1.0,
    access_count    INTEGER DEFAULT 0,
    last_access_at  REAL NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    pinned          INTEGER DEFAULT 0,
    source_turn_id  TEXT,
    embedding       BLOB,
    embed_model     TEXT,
    meta            TEXT DEFAULT '{}',
    deleted_at      REAL
);

CREATE INDEX IF NOT EXISTS idx_mem_user_sal  ON memories(user_id, salience DESC);
CREATE INDEX IF NOT EXISTS idx_mem_user_type ON memories(user_id, type);
CREATE INDEX IF NOT EXISTS idx_mem_user_live ON memories(user_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_mem_access    ON memories(user_id, last_access_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_pinned    ON memories(user_id, pinned);

-- ---------------- 记忆访问日志（强化统计 + 可视化） ----------------
CREATE TABLE IF NOT EXISTS memory_access_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL,
    turn_id     TEXT,
    score       REAL DEFAULT 0,
    accessed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memlog_mid  ON memory_access_log(memory_id);
CREATE INDEX IF NOT EXISTS idx_memlog_time ON memory_access_log(user_id, accessed_at DESC);

-- ---------------- 对话轮次 ----------------
CREATE TABLE IF NOT EXISTS turns (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    turn_index      INTEGER DEFAULT 0,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      REAL NOT NULL,
    asr_emotion     TEXT,
    vision_snapshot TEXT,
    emotion_state   TEXT,
    latency_ms      INTEGER,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    trace_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_turn_session   ON turns(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_turn_user_time ON turns(user_id, created_at DESC);

-- ---------------- 情感状态 ----------------
CREATE TABLE IF NOT EXISTS emotion_states (
    user_id         TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    mood            TEXT DEFAULT '平静',
    intensity       REAL DEFAULT 0.3,
    affection       REAL DEFAULT 500,
    companion_need  REAL DEFAULT 0,
    last_interaction REAL DEFAULT 0,
    updated_at      REAL DEFAULT 0,
    is_attachment   INTEGER DEFAULT 1   -- 是否认主（决定好感度底价）
);

-- 情感历史（画曲线用）
CREATE TABLE IF NOT EXISTS emotion_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    mood        TEXT,
    intensity   REAL,
    affection   REAL,
    trigger     TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emo_hist ON emotion_history(user_id, created_at DESC);

-- ---------------- 人脸身份（认主） ----------------
CREATE TABLE IF NOT EXISTS face_identities (
    person_id       TEXT PRIMARY KEY,
    user_id         TEXT REFERENCES users(id) ON DELETE SET NULL,
    display_name    TEXT DEFAULT '',
    is_owner        INTEGER DEFAULT 0,
    embedding       BLOB,
    embed_model     TEXT,
    enrolled_at     REAL DEFAULT 0,
    sample_count    INTEGER DEFAULT 1
);

-- ---------------- 主动搭话记录（频率控制） ----------------
CREATE TABLE IF NOT EXISTS proactive_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    trigger     TEXT NOT NULL,
    content     TEXT DEFAULT '',
    fired_at    REAL NOT NULL,
    suppressed  INTEGER DEFAULT 0,
    suppress_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_proact_time ON proactive_log(user_id, fired_at DESC);

-- ---------------- 视觉事件（保留有限天数） ----------------
CREATE TABLE IF NOT EXISTS vision_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT,
    event_type  TEXT NOT NULL,
    observation TEXT DEFAULT '{}',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vis_time ON vision_events(user_id, created_at DESC);

-- ---------------- 衰减巡检报告（可视化遗忘曲线） ----------------
CREATE TABLE IF NOT EXISTS decay_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    checked     INTEGER DEFAULT 0,
    decayed     INTEGER DEFAULT 0,
    forgotten   INTEGER DEFAULT 0,
    pinned_kept INTEGER DEFAULT 0,
    avg_before  REAL DEFAULT 0,
    avg_after   REAL DEFAULT 0,
    created_at  REAL NOT NULL
);

-- ---------------- 系统元数据 ----------------
CREATE TABLE IF NOT EXISTS kv_store (
    k           TEXT PRIMARY KEY,
    v           TEXT,
    updated_at  REAL
);
