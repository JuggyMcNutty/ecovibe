"""SQLite persistence for alerts, profiles, credentials, and history."""
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def _iso(dt: datetime | None) -> str | None:
    """Serialise a datetime to ISO 8601 (or None)."""
    if dt is None:
        return None
    return dt.isoformat()


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
                    account_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(plan_code, fqn_pattern, account_id)
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
            # Bare-timestamp index for retention pruning (the composite
            # plan_code index can't serve a pure timestamp range scan).
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_stock_events_ts ON stock_events(timestamp)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_code TEXT NOT NULL,
                    price_in_ucents INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    currency_code TEXT
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_history_plan ON price_history(plan_code, timestamp)"
            )
            # Best-effort column add for pre-existing DBs (currency wasn't
            # tracked per-row before, so old rows fall back to the caller's
            # current display currency rather than a stored value).
            try:
                cur.execute("ALTER TABLE price_history ADD COLUMN currency_code TEXT")
            except sqlite3.OperationalError:
                pass
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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS price_watches (
                    id TEXT PRIMARY KEY,
                    plan_code TEXT NOT NULL,
                    threshold_ucents INTEGER NOT NULL,
                    currency_code TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_notified_price INTEGER,
                    notified_at TEXT,
                    account_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(plan_code, account_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS promo_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_code TEXT NOT NULL,
                    promo_key TEXT NOT NULL,
                    payload TEXT,
                    first_seen TEXT NOT NULL,
                    account_id TEXT,
                    UNIQUE(plan_code, promo_key, account_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    application_key TEXT NOT NULL,
                    application_secret TEXT NOT NULL,
                    consumer_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Best-effort column adds: account_id on data tables (multi-account).
            for tbl in (
                "alerts",
                "checkout_profiles",
                "orders",
                "stock_events",
                "price_history",
            ):
                try:
                    cur.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN account_id TEXT"
                    )
                except sqlite3.OperationalError:
                    pass
            # Best-effort column adds on orders: enriched data from OVH API.
            for col_def in (
                "price_with_tax INTEGER",
                "currency_code TEXT",
                "pdf_url TEXT",
                "retraction_date TEXT",
                "expiration_date TEXT",
                "server_name TEXT",
            ):
                try:
                    cur.execute(f"ALTER TABLE orders ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass
            # Migrate legacy single-credential set into an account row.
            self._migrate_legacy_credentials(cur)
            # Must run after the migration above so account_id is backfilled
            # before the new constraint takes effect.
            self._migrate_alerts_unique_constraint(cur)
            self._conn.commit()

    def _migrate_legacy_credentials(self, cur: sqlite3.Cursor) -> None:
        """One-time migration: turn the old single-row `credentials` table
        into an `accounts` row, set it active, and backfill `account_id` on
        all existing data rows.

        Runs on every init() but is a no-op once `accounts` is populated or
        the legacy table is empty. Idempotent.
        """
        cur.execute("SELECT COUNT(*) AS n FROM accounts")
        if cur.fetchone()["n"] > 0:
            return  # already multi-account
        cur.execute("SELECT key, value FROM credentials")
        rows = cur.fetchall()
        if not rows:
            return  # fresh install, nothing to migrate
        creds = {r["key"]: r["value"] for r in rows}
        endpoint = creds.get("endpoint", get_settings().endpoint)
        label = {
            "ovh-eu": "Europe",
            "ovh-us": "United States",
            "ovh-ca": "Canada",
        }.get(endpoint, endpoint)
        acct_id = uuid.uuid4().hex
        cur.execute(
            "INSERT INTO accounts (id, label, endpoint, application_key, "
            "application_secret, consumer_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                acct_id,
                label,
                endpoint,
                creds.get("application_key", ""),
                creds.get("application_secret", ""),
                creds.get("consumer_key", ""),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        cur.execute(
            "INSERT INTO settings (key, value) VALUES ('active_account_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (acct_id,),
        )
        for tbl in (
            "alerts",
            "checkout_profiles",
            "orders",
            "stock_events",
            "price_history",
        ):
            cur.execute(
                f"UPDATE {tbl} SET account_id = ? WHERE account_id IS NULL",
                (acct_id,),
            )
        logger.info("Migrated legacy credentials to account %s (%s)", acct_id, label)

    def _migrate_alerts_unique_constraint(self, cur: sqlite3.Cursor) -> None:
        """One-time migration: rebuild `alerts` so its UNIQUE constraint
        includes `account_id`, not just `(plan_code, fqn_pattern)`.

        Without `account_id` in the constraint, two different accounts
        watching the same plan/pattern collide on the same unique index -
        the second `upsert_alert` raises `sqlite3.IntegrityError`, which
        `MonitorService.add_alert` swallows, so the alert silently never
        persists. Runs on every init() but is a no-op once the constraint
        already includes account_id (including on fresh installs, whose
        `CREATE TABLE` already has the right constraint). Idempotent.
        """
        cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='alerts'"
        )
        row = cur.fetchone()
        if row is None or "UNIQUE(plan_code, fqn_pattern, account_id)" in row["sql"]:
            return
        cur.execute(
            """
            CREATE TABLE alerts_new (
                id TEXT PRIMARY KEY,
                plan_code TEXT NOT NULL,
                fqn_pattern TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                notified_at TEXT,
                auto_order_profile_id TEXT,
                account_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(plan_code, fqn_pattern, account_id)
            )
            """
        )
        # On a (plan_code, fqn_pattern, account_id) collision (only possible
        # among rows that predate account scoping), keep the most recently
        # created row - INSERT OR IGNORE keeps the first row it sees per key.
        cur.execute(
            """
            INSERT OR IGNORE INTO alerts_new
                (id, plan_code, fqn_pattern, enabled, notified_at,
                 auto_order_profile_id, account_id, created_at)
            SELECT id, plan_code, fqn_pattern, enabled, notified_at,
                   auto_order_profile_id, account_id, created_at
            FROM alerts ORDER BY created_at DESC
            """
        )
        cur.execute("DROP TABLE alerts")
        cur.execute("ALTER TABLE alerts_new RENAME TO alerts")
        logger.info("Migrated alerts table: UNIQUE constraint now includes account_id")

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
        account_id: str | None = None,
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO alerts (id, plan_code, fqn_pattern, enabled, notified_at, auto_order_profile_id, account_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    plan_code=excluded.plan_code,
                    fqn_pattern=excluded.fqn_pattern,
                    enabled=excluded.enabled,
                    notified_at=excluded.notified_at,
                    auto_order_profile_id=excluded.auto_order_profile_id,
                    account_id=COALESCE(excluded.account_id, alerts.account_id)
                """,
                (id_, plan_code, fqn_pattern, int(enabled), _iso(notified_at),
                 auto_order_profile_id, account_id, datetime.now(timezone.utc).isoformat()),
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

    def load_alerts(self, account_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            if account_id:
                cur.execute(
                    "SELECT id, plan_code, fqn_pattern, enabled, notified_at, "
                    "auto_order_profile_id, account_id FROM alerts WHERE account_id = ?",
                    (account_id,),
                )
            else:
                cur.execute(
                    "SELECT id, plan_code, fqn_pattern, enabled, notified_at, "
                    "auto_order_profile_id, account_id FROM alerts"
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
                "account_id": r["account_id"],
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

    # ----- accounts (multi-region credentials) -----

    def list_accounts(self) -> list[dict[str, Any]]:
        """Return all stored accounts (raw secrets)."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT id, label, endpoint, application_key, application_secret, "
                "consumer_key, created_at FROM accounts ORDER BY created_at"
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT id, label, endpoint, application_key, application_secret, "
                "consumer_key, created_at FROM accounts WHERE id = ?",
                (account_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def save_account(
        self,
        account_id: str | None,
        label: str,
        endpoint: str,
        application_key: str,
        application_secret: str,
        consumer_key: str,
    ) -> str:
        """Insert or update an account. Returns the account id.

        If ``account_id`` is None a new id is generated. On update, empty
        ``application_secret``, ``application_key``, or ``consumer_key``
        are preserved (so masked edits don't wipe stored credentials).
        """
        aid = account_id or uuid.uuid4().hex
        with self._lock:
            cur = self._conn.cursor()
            if account_id:
                # Preserve stored credentials when the caller sends empty
                # values (masked-edit flow). Queried inline — NOT via
                # self.get_account(), which would deadlock on self._lock.
                cur.execute(
                    "SELECT application_key, application_secret, consumer_key FROM accounts WHERE id = ?",
                    (account_id,),
                )
                row = cur.fetchone()
                key = application_key or (row["application_key"] if row else "")
                secret = application_secret or (row["application_secret"] if row else "")
                ck = consumer_key or (row["consumer_key"] if row else "")
                cur.execute(
                    "UPDATE accounts SET label=?, endpoint=?, application_key=?, "
                    "application_secret=?, consumer_key=? WHERE id=?",
                    (label, endpoint, key, secret, ck, aid),
                )
            else:
                cur.execute(
                    "INSERT INTO accounts (id, label, endpoint, application_key, "
                    "application_secret, consumer_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        aid, label, endpoint, application_key, application_secret,
                        consumer_key, datetime.now(timezone.utc).isoformat(),
                    ),
                )
            self._conn.commit()
        return aid

    def delete_account(self, account_id: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            self._conn.commit()

    def get_active_account_id(self) -> str | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = 'active_account_id'")
            row = cur.fetchone()
        return row["value"] if row else None

    def set_active_account_id(self, account_id: str | None) -> None:
        with self._lock:
            cur = self._conn.cursor()
            if account_id is None:
                cur.execute("DELETE FROM settings WHERE key = 'active_account_id'")
            else:
                cur.execute(
                    "INSERT INTO settings (key, value) VALUES ('active_account_id', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (account_id,),
                )
            self._conn.commit()

    # ----- stock events (restock history) -----

    def log_stock_event(
        self, plan_code: str, fqn: str, event_type: str, timestamp: datetime,
        account_id: str | None = None,
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO stock_events (plan_code, fqn, event_type, timestamp, account_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (plan_code, fqn, event_type, _iso(timestamp), account_id),
            )
            self._conn.commit()

    def log_stock_events(
        self, events: list[tuple[str, str, str, datetime, str | None]]
    ) -> None:
        """Batch-insert stock events in one transaction.

        Each tuple is ``(plan_code, fqn, event_type, timestamp, account_id)``.
        Used by the monitor poller, which collects a cycle's events and
        persists them in a single call off the event loop (one commit
        instead of one per event).
        """
        if not events:
            return
        with self._lock:
            cur = self._conn.cursor()
            cur.executemany(
                "INSERT INTO stock_events (plan_code, fqn, event_type, timestamp, account_id) "
                "VALUES (?, ?, ?, ?, ?)",
                [(p, f, e, _iso(ts), aid) for p, f, e, ts, aid in events],
            )
            self._conn.commit()

    def prune_stock_events(self, retention_days: int, max_rows: int) -> int:
        """Delete stock events past the retention window, then enforce a
        hard row cap (oldest overflow dropped). Returns rows deleted.

        Called hourly (best-effort) by the monitor loop so the region
        ticker can't grow the DB unbounded during busy sales.
        """
        from datetime import timedelta
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()
        deleted = 0
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM stock_events WHERE timestamp < ?", (cutoff,))
            deleted += cur.rowcount
            cur.execute("SELECT COUNT(*) AS n FROM stock_events")
            overflow = cur.fetchone()["n"] - max_rows
            if overflow > 0:
                cur.execute(
                    "DELETE FROM stock_events WHERE id IN ("
                    "SELECT id FROM stock_events ORDER BY timestamp ASC, id ASC LIMIT ?)",
                    (overflow,),
                )
                deleted += cur.rowcount
            self._conn.commit()
        return deleted

    def load_stock_events(
        self, plan_code: str, since: datetime | None = None, limit: int = 500,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        # When ``account_id`` is given, scope to that account so two accounts on
        # the same subsidiary watching the same plan_code don't see each other's
        # events. None = all accounts (backwards-compatible).
        where = "plan_code = ?"
        params: list[Any] = [plan_code]
        if since:
            where += " AND timestamp >= ?"
            params.append(_iso(since))
        if account_id is not None:
            where += " AND account_id = ?"
            params.append(account_id)
        params.append(limit)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT plan_code, fqn, event_type, timestamp FROM stock_events "
                f"WHERE {where} ORDER BY timestamp DESC LIMIT ?",
                params,
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

    def stock_event_counts_by_hour(
        self, plan_code: str, days: int = 30, account_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Aggregate restock counts by hour-of-day over the last N days.

        ``account_id`` scopes the aggregate to a single account; None = all.
        """
        from datetime import timedelta as _timedelta
        cutoff = (datetime.now(timezone.utc) - _timedelta(days=days)).isoformat()
        where = "plan_code = ? AND event_type = 'available' AND timestamp >= ?"
        params: list[Any] = [plan_code, cutoff]
        if account_id is not None:
            where += " AND account_id = ?"
            params.append(account_id)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"""
                SELECT CAST(strftime('%H', timestamp) AS INTEGER) AS hour, COUNT(*) AS count
                FROM stock_events
                WHERE {where}
                GROUP BY hour ORDER BY hour
                """,
                params,
            )
            rows = cur.fetchall()
        return [{"hour": r["hour"], "count": r["count"]} for r in rows]

    def load_recent_stock_events(
        self, since: datetime, limit: int = 200,
        account_id: str | None = None, event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent stock events across ALL plans, newest first.

        Powers the region-activity feed (the ticker logs every plan's
        transitions, so this is the region-wide view). ``event_type``
        filters to 'available'/'unavailable'; None returns both.
        """
        where = "timestamp >= ?"
        params: list[Any] = [_iso(since)]
        if account_id is not None:
            where += " AND account_id = ?"
            params.append(account_id)
        if event_type is not None:
            where += " AND event_type = ?"
            params.append(event_type)
        params.append(limit)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT plan_code, fqn, event_type, timestamp FROM stock_events "
                f"WHERE {where} ORDER BY timestamp DESC, id DESC LIMIT ?",
                params,
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

    def load_account_stock_events(
        self, since: datetime, account_id: str | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        """Return all stock events for an account since ``since``, oldest first.

        Used by the insights summary to derive per-plan aggregates
        (restock counts, availability windows, current in-stock state).
        When ``account_id`` is None, events are not filtered by account.
        """
        where = "timestamp >= ?"
        params: list[Any] = [_iso(since)]
        if account_id is not None:
            where += " AND account_id = ?"
            params.append(account_id)
        params.append(limit)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT plan_code, fqn, event_type, timestamp FROM stock_events "
                f"WHERE {where} ORDER BY timestamp ASC LIMIT ?",
                params,
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

    # ----- price history -----

    def log_price(self, plan_code: str, price_in_ucents: int, timestamp: datetime,
                  account_id: str | None = None, currency_code: str | None = None) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO price_history (plan_code, price_in_ucents, timestamp, account_id, currency_code) "
                "VALUES (?, ?, ?, ?, ?)",
                (plan_code, price_in_ucents, _iso(timestamp), account_id, currency_code),
            )
            self._conn.commit()

    def load_price_history(
        self, plan_code: str, limit: int = 100, account_id: str | None = None
    ) -> list[dict[str, Any]]:
        where = "plan_code = ?"
        params: list[Any] = [plan_code]
        if account_id is not None:
            where += " AND account_id = ?"
            params.append(account_id)
        params.append(limit)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT plan_code, price_in_ucents, timestamp, currency_code FROM price_history "
                f"WHERE {where} ORDER BY timestamp DESC LIMIT ?",
                params,
            )
            rows = cur.fetchall()
        return [
            {
                "plan_code": r["plan_code"],
                "price_in_ucents": r["price_in_ucents"],
                "timestamp": r["timestamp"],
                "currency_code": r["currency_code"],
            }
            for r in rows
        ]

    def latest_price(self, plan_code: str, account_id: str | None = None) -> int | None:
        where = "plan_code = ?"
        params: list[Any] = [plan_code]
        if account_id is not None:
            where += " AND account_id = ?"
            params.append(account_id)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT price_in_ucents FROM price_history "
                f"WHERE {where} ORDER BY timestamp DESC LIMIT 1",
                params,
            )
            row = cur.fetchone()
        return row["price_in_ucents"] if row else None

    # ----- price watches + promo events -----

    def upsert_price_watch(
        self, watch_id: str | None, plan_code: str, threshold_ucents: int,
        currency_code: str | None = None, account_id: str | None = None,
    ) -> str:
        """Insert or update a price watch. One watch per (plan, account);
        re-saving the same plan updates the threshold and re-arms the
        notification state. Returns the watch id."""
        wid = watch_id or uuid.uuid4().hex
        with self._lock:
            cur = self._conn.cursor()
            # Manual upsert: SQLite UNIQUE treats NULLs as distinct, so
            # ON CONFLICT would not fire for rows with a NULL account_id.
            cur.execute(
                "SELECT id FROM price_watches WHERE plan_code = ? AND account_id IS ?",
                (plan_code, account_id),
            )
            row = cur.fetchone()
            if row:
                wid = row["id"]
                cur.execute(
                    "UPDATE price_watches SET threshold_ucents = ?, "
                    "currency_code = ?, enabled = 1, last_notified_price = NULL, "
                    "notified_at = NULL WHERE id = ?",
                    (threshold_ucents, currency_code, wid),
                )
            else:
                cur.execute(
                    "INSERT INTO price_watches "
                    "(id, plan_code, threshold_ucents, currency_code, enabled, "
                    " last_notified_price, notified_at, account_id, created_at) "
                    "VALUES (?, ?, ?, ?, 1, NULL, NULL, ?, ?)",
                    (wid, plan_code, threshold_ucents, currency_code, account_id,
                     _iso(datetime.now(timezone.utc))),
                )
            self._conn.commit()
        return wid

    def load_price_watches(
        self, account_id: str | None = None, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        where, params = "1=1", []
        if account_id is not None:
            where += " AND account_id = ?"
            params.append(account_id)
        if enabled_only:
            where += " AND enabled = 1"
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT id, plan_code, threshold_ucents, currency_code, enabled, "
                f"last_notified_price, notified_at, account_id, created_at "
                f"FROM price_watches WHERE {where} ORDER BY created_at",
                params,
            )
            rows = cur.fetchall()
        return [dict(r) | {"enabled": bool(r["enabled"])} for r in rows]

    def delete_price_watch(self, watch_id: str, account_id: str | None = None) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            if account_id:
                cur.execute(
                    "DELETE FROM price_watches WHERE id = ? AND account_id = ?",
                    (watch_id, account_id),
                )
            else:
                cur.execute("DELETE FROM price_watches WHERE id = ?", (watch_id,))
            deleted = cur.rowcount > 0
            self._conn.commit()
        return deleted

    def mark_price_watch_notified(self, watch_id: str, price_ucents: int) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE price_watches SET last_notified_price = ?, notified_at = ? "
                "WHERE id = ?",
                (price_ucents, _iso(datetime.now(timezone.utc)), watch_id),
            )
            self._conn.commit()

    def record_promo(
        self, plan_code: str, promo_key: str, payload: str,
        account_id: str | None = None,
    ) -> bool:
        """Record a promotion sighting. Returns True the FIRST time this
        (plan, promo) pair is seen — the caller notifies only then."""
        with self._lock:
            cur = self._conn.cursor()
            # Manual dedup for the same NULL-account reason as price watches.
            cur.execute(
                "SELECT 1 FROM promo_events WHERE plan_code = ? AND "
                "promo_key = ? AND account_id IS ?",
                (plan_code, promo_key, account_id),
            )
            if cur.fetchone():
                return False
            cur.execute(
                "INSERT INTO promo_events "
                "(plan_code, promo_key, payload, first_seen, account_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (plan_code, promo_key, payload,
                 _iso(datetime.now(timezone.utc)), account_id),
            )
            self._conn.commit()
        return True

    def load_recent_promos(
        self, limit: int = 50, account_id: str | None = None
    ) -> list[dict[str, Any]]:
        where, params = "1=1", []
        if account_id is not None:
            where += " AND account_id = ?"
            params.append(account_id)
        params.append(limit)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT plan_code, promo_key, payload, first_seen FROM promo_events "
                f"WHERE {where} ORDER BY first_seen DESC, id DESC LIMIT ?",
                params,
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    # ----- checkout profiles -----

    def upsert_profile(self, p: dict[str, Any]) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO checkout_profiles
                    (id, name, plan_code, fqn, ram, storage, bandwidth, datacenters,
                     region, os, duration, auto_pay, waive_retractation, max_price, account_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, plan_code=excluded.plan_code, fqn=excluded.fqn,
                    ram=excluded.ram, storage=excluded.storage, bandwidth=excluded.bandwidth,
                    datacenters=excluded.datacenters, region=excluded.region, os=excluded.os,
                    duration=excluded.duration, auto_pay=excluded.auto_pay,
                    waive_retractation=excluded.waive_retractation, max_price=excluded.max_price,
                    account_id=COALESCE(excluded.account_id, checkout_profiles.account_id)
                """,
                (
                    p["id"], p["name"], p["plan_code"], p["fqn"],
                    p.get("ram"), p.get("storage"), p.get("bandwidth"),
                    p.get("datacenters"), p["region"], p["os"], p["duration"],
                    int(p.get("auto_pay", False)), int(p.get("waive_retractation", False)),
                    p.get("max_price"), p.get("account_id"),
                    p.get("created_at", _iso(datetime.now(timezone.utc))),
                ),
            )
            self._conn.commit()

    def delete_profile(self, profile_id: str, account_id: str | None = None) -> None:
        with self._lock:
            cur = self._conn.cursor()
            if account_id:
                cur.execute(
                    "DELETE FROM checkout_profiles WHERE id = ? AND account_id = ?",
                    (profile_id, account_id),
                )
            else:
                cur.execute("DELETE FROM checkout_profiles WHERE id = ?", (profile_id,))
            self._conn.commit()

    def load_profiles(self, account_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            if account_id:
                cur.execute(
                    "SELECT * FROM checkout_profiles WHERE account_id = ? ORDER BY name",
                    (account_id,),
                )
            else:
                cur.execute("SELECT * FROM checkout_profiles ORDER BY name")
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def load_profile(self, profile_id: str, account_id: str | None = None) -> dict[str, Any] | None:
        """Fetch a profile by id, optionally scoped to an account.

        ``account_id`` is omitted by internal callers that legitimately
        need cross-account lookup (e.g. the sniper firing under an alert's
        own account, which may differ from the currently active one).
        User-facing routes must always pass it to prevent IDOR access to
        another account's profile.
        """
        with self._lock:
            cur = self._conn.cursor()
            if account_id:
                cur.execute(
                    "SELECT * FROM checkout_profiles WHERE id = ? AND account_id = ?",
                    (profile_id, account_id),
                )
            else:
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
        account_id: str | None = None,
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO orders (order_id, cart_id, plan_code, status, url, placed_at, account_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (order_id, cart_id, plan_code, status, url, _iso(placed_at), account_id),
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

    def load_orders(self, limit: int = 50, account_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            if account_id:
                cur.execute(
                    "SELECT order_id, cart_id, plan_code, status, url, placed_at, account_id, "
                    "price_with_tax, currency_code, pdf_url, retraction_date, expiration_date, server_name "
                    "FROM orders WHERE account_id = ? ORDER BY placed_at DESC LIMIT ?",
                    (account_id, limit),
                )
            else:
                cur.execute(
                    "SELECT order_id, cart_id, plan_code, status, url, placed_at, account_id, "
                    "price_with_tax, currency_code, pdf_url, retraction_date, expiration_date, server_name "
                    "FROM orders ORDER BY placed_at DESC LIMIT ?",
                    (limit,),
                )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def get_order_by_id(self, order_id: int) -> dict[str, Any] | None:
        """Return a single order row by OVH order ID, or None."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT order_id, cart_id, plan_code, status, url, placed_at, account_id, "
                "price_with_tax, currency_code, pdf_url, retraction_date, expiration_date, server_name "
                "FROM orders WHERE order_id = ?",
                (order_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def upsert_order_enriched(
        self,
        order_id: int,
        *,
        status: str | None = None,
        price_with_tax: int | None = None,
        currency_code: str | None = None,
        pdf_url: str | None = None,
        retraction_date: str | None = None,
        expiration_date: str | None = None,
        server_name: str | None = None,
        account_id: str | None = None,
    ) -> None:
        """Insert or update an order with enriched data from the OVH API.

        If the order doesn't exist locally (placed outside this app),
        a new row is created with the OVH-supplied fields. If it does
        exist, only the enriched fields are updated.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT 1 FROM orders WHERE order_id = ?", (order_id,))
            exists = cur.fetchone() is not None
            if exists:
                cur.execute(
                    """
                    UPDATE orders SET
                        status = COALESCE(?, status),
                        price_with_tax = COALESCE(?, price_with_tax),
                        currency_code = COALESCE(?, currency_code),
                        pdf_url = COALESCE(?, pdf_url),
                        retraction_date = COALESCE(?, retraction_date),
                        expiration_date = COALESCE(?, expiration_date),
                        server_name = COALESCE(?, server_name)
                    WHERE order_id = ?
                    """,
                    (status, price_with_tax, currency_code, pdf_url,
                     retraction_date, expiration_date, server_name, order_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO orders (order_id, cart_id, plan_code, status, url, placed_at,
                                        account_id, price_with_tax, currency_code, pdf_url,
                                        retraction_date, expiration_date, server_name)
                    VALUES (?, '', '', ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (order_id, status, _iso(datetime.now(timezone.utc)), account_id,
                     price_with_tax, currency_code, pdf_url,
                     retraction_date, expiration_date, server_name),
                )
            self._conn.commit()


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
        _storage.init()
    return _storage
