"""SQLite 数据库封装：硬链接记录、刮削记录、操作日志。"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.core.logger import logger


class Database:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS hardlinks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                target_path TEXT NOT NULL UNIQUE,
                inode INTEGER DEFAULT 0,
                size INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_hardlinks_source ON hardlinks(source_path);
            CREATE INDEX IF NOT EXISTS idx_hardlinks_status ON hardlinks(status);

            CREATE TABLE IF NOT EXISTS scrapes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_path TEXT NOT NULL UNIQUE,
                provider TEXT DEFAULT '',
                match_score REAL DEFAULT 0,
                matched_key TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                cover_path TEXT DEFAULT '',
                lyrics_path TEXT DEFAULT '',
                album_nfo_path TEXT DEFAULT '',
                artist_nfo_path TEXT DEFAULT '',
                error TEXT DEFAULT '',
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scrapes_target ON scrapes(target_path);

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                level TEXT DEFAULT 'info',
                category TEXT DEFAULT 'hardlink',
                message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_logs_time ON logs(timestamp);
            """
        )
        conn.commit()

    # ---- 硬链接 ----
    def upsert_hardlink(self, source_path: str, target_path: str, inode: int, size: int) -> None:
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO hardlinks (source_path, target_path, inode, size, status, created_at)
            VALUES (?, ?, ?, ?, 'active', ?)
            ON CONFLICT(target_path) DO UPDATE SET
                source_path=excluded.source_path,
                inode=excluded.inode,
                size=excluded.size,
                status='active',
                created_at=excluded.created_at
            """,
            (source_path, target_path, inode, size, _now()),
        )
        conn.commit()

    def get_hardlink_by_target(self, target_path: str) -> Optional[sqlite3.Row]:
        cur = self._conn().execute(
            "SELECT * FROM hardlinks WHERE target_path=?", (target_path,)
        )
        return cur.fetchone()

    def get_hardlink_by_source(self, source_path: str) -> Optional[sqlite3.Row]:
        cur = self._conn().execute(
            "SELECT * FROM hardlinks WHERE source_path=?", (source_path,)
        )
        return cur.fetchone()

    def list_hardlinks(self, status: Optional[str] = None) -> list[sqlite3.Row]:
        if status:
            cur = self._conn().execute(
                "SELECT * FROM hardlinks WHERE status=? ORDER BY target_path", (status,)
            )
        else:
            cur = self._conn().execute("SELECT * FROM hardlinks ORDER BY target_path")
        return cur.fetchall()

    def delete_hardlink(self, target_path: str) -> None:
        self._conn().execute("DELETE FROM hardlinks WHERE target_path=?", (target_path,))
        self._conn().commit()

    def set_hardlink_status(self, target_path: str, status: str) -> None:
        self._conn().execute(
            "UPDATE hardlinks SET status=? WHERE target_path=?", (status, target_path)
        )
        self._conn().commit()

    def count_hardlinks(self) -> int:
        cur = self._conn().execute("SELECT COUNT(*) FROM hardlinks")
        return cur.fetchone()[0]

    # ---- 刮削 ----
    def upsert_scrape(self, record: dict[str, Any]) -> None:
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO scrapes (target_path, provider, match_score, matched_key, status,
                cover_path, lyrics_path, album_nfo_path, artist_nfo_path, error, updated_at)
            VALUES (:target_path, :provider, :match_score, :matched_key, :status,
                :cover_path, :lyrics_path, :album_nfo_path, :artist_nfo_path, :error, :updated_at)
            ON CONFLICT(target_path) DO UPDATE SET
                provider=excluded.provider, match_score=excluded.match_score,
                matched_key=excluded.matched_key, status=excluded.status,
                cover_path=excluded.cover_path, lyrics_path=excluded.lyrics_path,
                album_nfo_path=excluded.album_nfo_path, artist_nfo_path=excluded.artist_nfo_path,
                error=excluded.error, updated_at=excluded.updated_at
            """,
            record,
        )
        conn.commit()

    def get_scrape(self, target_path: str) -> Optional[sqlite3.Row]:
        cur = self._conn().execute(
            "SELECT * FROM scrapes WHERE target_path=?", (target_path,)
        )
        return cur.fetchone()

    def list_scrapes(self, status: Optional[str] = None) -> list[sqlite3.Row]:
        if status:
            cur = self._conn().execute(
                "SELECT * FROM scrapes WHERE status=? ORDER BY target_path", (status,)
            )
        else:
            cur = self._conn().execute("SELECT * FROM scrapes ORDER BY target_path")
        return cur.fetchall()

    # ---- 日志 ----
    def add_log(self, level: str, category: str, message: str) -> None:
        self._conn().execute(
            "INSERT INTO logs (timestamp, level, category, message) VALUES (?, ?, ?, ?)",
            (_now(), level, category, message),
        )
        self._conn().commit()

    def list_logs(self, level: Optional[str] = None, category: Optional[str] = None,
                  limit: int = 200) -> list[sqlite3.Row]:
        q = "SELECT * FROM logs"
        conds, params = [], []
        if level:
            conds.append("level=?")
            params.append(level)
        if category:
            conds.append("category=?")
            params.append(category)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self._conn().execute(q, params).fetchall()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        from app.core.config import config
        _db = Database(Path(config.data_dir) / "music.db")
    return _db
