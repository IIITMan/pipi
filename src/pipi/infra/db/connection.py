"""数据库连接与初始化。

用 sqlite3 标准库（不引 ORM）：记忆检索需要手写 SQL 做排序，
ORM 反而碍事。连接用 WAL 模式，允许读写并发。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Database:
    """轻量数据库封装。

    注意：sqlite3 连接不是线程安全的，这里每个线程一个连接（thread-local）。
    对单进程应用来说足够，也避免引入连接池复杂度。
    """

    def __init__(self, path: str | Path, schema_path: Path | None = None):
        self.path = Path(path)
        self.schema_path = schema_path or SCHEMA_PATH
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---- 连接管理 ----

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path),
                check_same_thread=False,
                timeout=10.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        if not self.schema_path.exists():
            logger.warning("未找到 schema 文件：%s", self.schema_path)
            return
        sql = self.schema_path.read_text(encoding="utf-8")
        with self.conn as conn:
            conn.executescript(sql)
        logger.debug("数据库初始化完成：%s", self.path)

    # ---- 查询辅助 ----

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---- 维护 ----

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    def stats(self) -> dict[str, int]:
        """各表行数，用于健康检查。"""
        tables = [
            "users", "profiles", "memories", "turns",
            "emotion_states", "face_identities", "proactive_log",
        ]
        out = {}
        for t in tables:
            try:
                row = self.query_one(f"SELECT COUNT(*) AS c FROM {t}")
                out[t] = row["c"] if row else 0
            except sqlite3.Error:
                out[t] = -1
        return out
