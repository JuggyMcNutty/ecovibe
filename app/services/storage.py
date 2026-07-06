import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class Storage:
    """SQLite-backed persistence for alerts and settings."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or get_settings().db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        should_exist = os.path.exists(self._db_path)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if not should_exist:
            logger.info("created SQLite database at %s", self._db_path)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    plan_code TEXT NOT NULL,
                    fqn_pattern TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    notified_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(plan_code, fqn_pattern)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def upsert_alert(
        self,
        id_: str,
        plan_code: str,
        fqn_pattern: str,
        enabled: bool,
        notified_at: datetime | None,
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO alerts (id, plan_code, fqn_pattern, enabled, notified_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    plan_code=excluded.plan_code,
                    fqn_pattern=excluded.fqn_pattern,
                    enabled=excluded.enabled,
                    notified_at=excluded.notified_at
                """,
                (id_, plan_code, fqn_pattern, int(enabled), _iso(notified_at),
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def delete_alert(self, id_: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM alerts WHERE id = ?", (id_,))
            self._conn.commit()

    def set_alert_enabled(self, id_: str, enabled: bool) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("UPDATE alerts SET enabled = ? WHERE id = ?", (int(enabled), id_))
            self._conn.commit()

    def set_notified_at(self, id_: str, notified_at: datetime) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("UPDATE alerts SET notified_at = ? WHERE id = ?",
                        (_iso(notified_at), id_))
            self._conn.commit()

    def load_alerts(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT id, plan_code, fqn_pattern, enabled, notified_at FROM alerts")
            rows = cur.fetchall()
        return [
            {
                "id": r["id"],
                "plan_code": r["plan_code"],
                "fqn_pattern": r["fqn_pattern"],
                "enabled": bool(r["enabled"]),
                "notified_at": _parse_iso(r["notified_at"]),
            }
            for r in rows
        ]

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def save_snapshot(self, snapshot: dict[str, Any], path: str) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, default=str)
        os.replace(tmp, path)


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
        _storage.init()
    return _storage
