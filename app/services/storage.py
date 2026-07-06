"""SQLite persistence for alerts, profiles, credentials, and history."""
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
    """Serialise a datetime to ISO 8601 (or None)."""


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class Storage:
    """SQLite-backed persistence for alerts, settings, profiles, and history.

    All methods are thread-safe via a single `threading.Lock`. The connection
    is opened lazily on first `init()` call (which the singleton accessor
    triggers automatically). Callers do not need to manage transactions -
    each method commits its own changes.
    """

    def __init__(self, db_path: str | None = None) -> None:
        # Default to the path from Settings if none is provided. Tests pass
        # a temp path to isolate the database per test.
        self._db_path = db_path or get_settings().db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        """Open the connection and create the schema (idempotent).

        Also runs best-effort `ALTER TABLE` migrations for columns added
        after the database was first created - this lets users upgrade the
        app without losing their existing alerts.
        """
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
                    auto_order_profile_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(plan_code, fqn_pattern)
                )
                """
            )
            # Best-effort column add for pre-existing DBs that lack the sniper column.
            try:
                cur.execute("ALTER TABLE alerts ADD COLUMN auto_order_profile_id TEXT")
            except sqlite3.OperationalError:
                pass
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_code TEXT NOT NULL,
                    fqn TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_stock_events_plan ON stock_events(plan_code, timestamp)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_code TEXT NOT NULL,
                    price_in_ucents INTEGER NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_history_plan ON price_history(plan_code, timestamp)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS checkout_profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    plan_code TEXT NOT NULL,
                    fqn TEXT NOT NULL,
                    ram TEXT,
                    storage TEXT,
                    bandwidth TEXT,
                    datacenters TEXT,
                    region TEXT NOT NULL,
                    os TEXT NOT NULL,
                    duration TEXT NOT NULL,
                    auto_pay INTEGER NOT NULL DEFAULT 0,
                    waive_retractation INTEGER NOT NULL DEFAULT 0,
                    max_price INTEGER,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    cart_id TEXT,
                    plan_code TEXT NOT NULL,
                    status TEXT,
                    url TEXT,
                    placed_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS credentials (
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
        auto_order_profile_id: str | None = None,
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO alerts (id, plan_code, fqn_pattern, enabled, notified_at, auto_order_profile_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    plan_code=excluded.plan_code,
                    fqn_pattern=excluded.fqn_pattern,
                    enabled=excluded.enabled,
                    notified_at=excluded.notified_at,
                    auto_order_profile_id=excluded.auto_order_profile_id
                """,
                (id_, plan_code, fqn_pattern, int(enabled), _iso(notified_at),
                 auto_order_profile_id, datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def set_alert_profile(self, id_: str, profile_id: str | None) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE alerts SET auto_order_profile_id = ? WHERE id = ?",
                (profile_id, id_),
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
            cur.execute(
                "SELECT id, plan_code, fqn_pattern, enabled, notified_at, auto_order_profile_id "
                "FROM alerts"
            )
            rows = cur.fetchall()
        return [
            {
                "id": r["id"],
                "plan_code": r["plan_code"],
                "fqn_pattern": r["fqn_pattern"],
                "enabled": bool(r["enabled"]),
                "notified_at": _parse_iso(r["notified_at"]),
                "auto_order_profile_id": r["auto_order_profile_id"],
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

    # ----- OVH credentials -----

    def save_credentials(
        self,
        endpoint: str,
        application_key: str,
        application_secret: str,
        consumer_key: str,
    ) -> None:
        """Persist OVH API credentials to the database.

        Stored in the `credentials` key-value table. Values are plaintext
        because the OVH SDK needs them in plaintext to sign requests.
        """
        with self._lock:
            cur = self._conn.cursor()
            for key, value in (
                ("endpoint", endpoint),
                ("application_key", application_key),
                ("application_secret", application_secret),
                ("consumer_key", consumer_key),
            ):
                cur.execute(
                    "INSERT INTO credentials (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            self._conn.commit()

    def load_credentials(self) -> dict[str, str] | None:
        """Return all stored credentials, or None if none are stored.

        Returns a dict with keys: endpoint, application_key,
        application_secret, consumer_key.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT key, value FROM credentials")
            rows = cur.fetchall()
        if not rows:
            return None
        return {r["key"]: r["value"] for r in rows}

    def clear_credentials(self) -> None:
        """Delete all stored credentials."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM credentials")
            self._conn.commit()

    # ----- stock events (restock history) -----

    def log_stock_event(
        self, plan_code: str, fqn: str, event_type: str, timestamp: datetime
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO stock_events (plan_code, fqn, event_type, timestamp) VALUES (?, ?, ?, ?)",
                (plan_code, fqn, event_type, _iso(timestamp)),
            )
            self._conn.commit()

    def load_stock_events(
        self, plan_code: str, since: datetime | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            if since:
                cur.execute(
                    "SELECT plan_code, fqn, event_type, timestamp FROM stock_events "
                    "WHERE plan_code = ? AND timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
                    (plan_code, _iso(since), limit),
                )
            else:
                cur.execute(
                    "SELECT plan_code, fqn, event_type, timestamp FROM stock_events "
                    "WHERE plan_code = ? ORDER BY timestamp DESC LIMIT ?",
                    (plan_code, limit),
                )
            rows = cur.fetchall()
        return [
            {
                "plan_code": r["plan_code"],
                "fqn": r["fqn"],
                "event_type": r["event_type"],
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    def stock_event_counts_by_hour(self, plan_code: str, days: int = 30) -> list[dict[str, Any]]:
        """Aggregate restock counts by hour-of-day over the last N days."""
        from datetime import timedelta as _timedelta
        cutoff = (datetime.now(timezone.utc) - _timedelta(days=days)).isoformat()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT CAST(strftime('%H', timestamp) AS INTEGER) AS hour, COUNT(*) AS count
                FROM stock_events
                WHERE plan_code = ? AND event_type = 'available' AND timestamp >= ?
                GROUP BY hour ORDER BY hour
                """,
                (plan_code, cutoff),
            )
            rows = cur.fetchall()
        return [{"hour": r["hour"], "count": r["count"]} for r in rows]

    # ----- price history -----

    def log_price(self, plan_code: str, price_in_ucents: int, timestamp: datetime) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO price_history (plan_code, price_in_ucents, timestamp) VALUES (?, ?, ?)",
                (plan_code, price_in_ucents, _iso(timestamp)),
            )
            self._conn.commit()

    def load_price_history(
        self, plan_code: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT plan_code, price_in_ucents, timestamp FROM price_history "
                "WHERE plan_code = ? ORDER BY timestamp DESC LIMIT ?",
                (plan_code, limit),
            )
            rows = cur.fetchall()
        return [
            {
                "plan_code": r["plan_code"],
                "price_in_ucents": r["price_in_ucents"],
                "price_eur": r["price_in_ucents"] / 1_000_000,
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    def latest_price(self, plan_code: str) -> int | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT price_in_ucents FROM price_history "
                "WHERE plan_code = ? ORDER BY timestamp DESC LIMIT 1",
                (plan_code,),
            )
            row = cur.fetchone()
        return row["price_in_ucents"] if row else None

    # ----- checkout profiles -----

    def upsert_profile(self, p: dict[str, Any]) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO checkout_profiles
                    (id, name, plan_code, fqn, ram, storage, bandwidth, datacenters,
                     region, os, duration, auto_pay, waive_retractation, max_price, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, plan_code=excluded.plan_code, fqn=excluded.fqn,
                    ram=excluded.ram, storage=excluded.storage, bandwidth=excluded.bandwidth,
                    datacenters=excluded.datacenters, region=excluded.region, os=excluded.os,
                    duration=excluded.duration, auto_pay=excluded.auto_pay,
                    waive_retractation=excluded.waive_retractation, max_price=excluded.max_price
                """,
                (
                    p["id"], p["name"], p["plan_code"], p["fqn"],
                    p.get("ram"), p.get("storage"), p.get("bandwidth"),
                    p.get("datacenters"), p["region"], p["os"], p["duration"],
                    int(p.get("auto_pay", False)), int(p.get("waive_retractation", False)),
                    p.get("max_price"), p.get("created_at", _iso(datetime.now(timezone.utc))),
                ),
            )
            self._conn.commit()

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM checkout_profiles WHERE id = ?", (profile_id,))
            self._conn.commit()

    def load_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM checkout_profiles ORDER BY name")
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def load_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM checkout_profiles WHERE id = ?", (profile_id,)
            )
            row = cur.fetchone()
        return dict(row) if row else None

    # ----- orders -----

    def log_order(
        self,
        order_id: int | None,
        cart_id: str,
        plan_code: str,
        status: str | None,
        url: str | None,
        placed_at: datetime,
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO orders (order_id, cart_id, plan_code, status, url, placed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (order_id, cart_id, plan_code, status, url, _iso(placed_at)),
            )
            self._conn.commit()

    def update_order_status(self, order_id: int, status: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE orders SET status = ? WHERE order_id = ?",
                (status, order_id),
            )
            self._conn.commit()

    def load_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT order_id, cart_id, plan_code, status, url, placed_at "
                "FROM orders ORDER BY placed_at DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
        _storage.init()
    return _storage
