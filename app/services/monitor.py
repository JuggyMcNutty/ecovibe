"""Stock monitoring engine + sniper auto-orderer.

MonitorService runs a single background poller that checks OVH for stock
changes and broadcasts to SSE subscribers. SniperService fires rush orders
automatically when an armed alert matches. State is in-memory, mirrored
to SQLite.

The poller watches EVERY stored account, not just the active one: each
cycle groups the enabled alerts by ``account_id`` and polls each group
under its own credentials. Switching the active account therefore only
changes what the UI shows — insight data keeps accruing and alerts keep
firing for every account. All per-plan state is consequently keyed by
``(account_id, plan_code)``: two accounts can watch the same plan code in
different regions with completely different stock.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from typing import Any

from app.services.app_settings import app_setting_bool, app_setting_int
from app.services.ovh_service import (
    OVHServiceError,
    get_active_ovh_service,
    get_ovh_service,
    orderable_entry,
)

logger = logging.getLogger(__name__)

# Floor on the poll interval while batch mode is active. The unfiltered
# region-wide availabilities response is ~28k entries (~1.3s fetch), so
# hammering it every second would keep the per-account client lock busy
# and strain OVH. Single-plan polling keeps its 1s fidelity for sniping.
BATCH_MIN_POLL_INTERVAL = 3

# Delivery watch. An order in one of these states is finished with — OVH will
# not move it again — so it is never re-queried, and a long order history
# costs nothing after the first settle.
TERMINAL_ORDER_STATUSES = frozenset({"delivered", "cancelled"})

# Terminal transitions worth a notification. Intermediate churn
# (checking → delivering) is persisted and streamed but never fanned out.
NOTIFY_ORDER_STATUSES = TERMINAL_ORDER_STATUSES

# How far back the delivery watch lists orders, and how many status calls it
# will spend per account per cycle. Every OVH call serialises on the account's
# client lock, so an account with a pile of pending orders must not be able to
# monopolise a cycle (same reasoning as name_budget in api/orders.py).
ORDER_WATCH_DAYS = 90
ORDER_STATUS_BUDGET = 10


@dataclass
class StockAlert:
    """A watch on a plan_code + FQN pattern. auto_order_profile_id
    links to a checkout profile for sniper mode. account_id scopes the
    alert to one stored credential set (the sniper orders under it)."""

    id: str
    plan_code: str
    fqn_pattern: str
    enabled: bool = True
    notified_at: datetime | None = None
    auto_order_profile_id: str | None = None
    account_id: str | None = None


@dataclass
class StockStatus:
    """Snapshot of one FQN's availability at a point in time."""

    plan_code: str
    fqn: str
    available: bool
    last_check: datetime


class DuplicateAlertError(Exception):
    """Raised when adding an alert whose (plan_code, fqn_pattern) already exists."""


class SniperService:
    """Background auto-order: when an alert with a profile fires, run rush checkout.

    Tracks armed state per alert. Only fires once per alert per arm cycle (avoids
    double-orders if the same stock persists across polls). Re-arm by POSTing to
    /api/sniper/arm again after a successful (or failed) order.
    """

    def __init__(self) -> None:
        # alert_id -> {profile_id, fqns_seen}
        self._armed: dict[str, dict[str, Any]] = {}
        # alert_ids currently being processed (prevents overlapping fires)
        self._in_flight: set[str] = set()
        # alert_id -> last result payload (for the status endpoint)
        self._results: dict[str, dict[str, Any]] = {}

    def arm(
        self, alert_id: str, profile_id: str,
        plan_code: str | None = None, fqn_pattern: str | None = None,
        account_id: str | None = None,
    ) -> None:
        """Arm an alert: future matches will trigger the profile's rush order.

        ``plan_code``/``fqn_pattern``/``account_id`` capture what to watch and
        under which credentials, so the poller can keep firing this sniper even
        after the user switches away from its account (see
        MonitorService._sweep_snipers). Entries armed without them fall back to
        active-account-only polling.
        """
        self._armed[alert_id] = {
            "profile_id": profile_id,
            "fqns_seen": set(),
            "plan_code": plan_code,
            "fqn_pattern": fqn_pattern,
            "account_id": account_id,
        }
        self._results.pop(alert_id, None)

    def disarm(self, alert_id: str) -> None:
        """Cancel sniper mode for an alert. In-flight orders are not aborted."""
        self._armed.pop(alert_id, None)

    def disarm_for_account(self, account_id: str) -> list[str]:
        """Disarm every sniper armed under an account and return their alert
        ids. Called when the account is deleted — otherwise its armed entries
        would sit in ``_armed`` forever: the sweep skips them (the service is
        unconfigured) and the poller never sees their alerts again."""
        ids = [
            aid for aid, v in self._armed.items()
            if v.get("account_id") == account_id
        ]
        for aid in ids:
            self._armed.pop(aid, None)
        return ids

    def is_armed(self, alert_id: str) -> bool:
        return alert_id in self._armed

    def status(self) -> dict[str, Any]:
        """Snapshot of armed alerts and last results (for GET /api/sniper/status)."""
        return {
            "armed": [
                {
                    "alert_id": aid,
                    "profile_id": v["profile_id"],
                    "fqns_seen": sorted(v["fqns_seen"]),
                    # plan_code/fqn_pattern/account_id let the status endpoint
                    # (and UI) identify snipers armed under a now-inactive
                    # account, which are no longer in the active alert list.
                    "plan_code": v.get("plan_code"),
                    "fqn_pattern": v.get("fqn_pattern"),
                    "account_id": v.get("account_id"),
                }
                for aid, v in self._armed.items()
            ],
            "results": self._results,
        }

    async def maybe_fire(
        self,
        alert_id: str,
        plan_code: str,
        matched_fqns: list[str],
        account_id: str | None = None,
    ) -> None:
        """Called from MonitorService after an alert match.

        Fires the rush order in the background if (and only if):
          - the alert is armed,
          - no order is already in flight for it, and
          - there is at least one FQN we haven't already tried to order.

        ``account_id`` selects which credential set the order runs under
        (the alert's own account, not necessarily the active one).

        Tracking `fqns_seen` per arm cycle is what prevents double-orders
        when the same stock config persists across consecutive polls.
        """
        if alert_id not in self._armed:
            return
        if alert_id in self._in_flight:
            return
        seen = self._armed[alert_id]["fqns_seen"]
        new_fqns = [f for f in matched_fqns if f not in seen]
        if not new_fqns:
            return
        # Only mark the FQN we actually attempt (new_fqns[0]) as seen. Marking
        # the whole batch would permanently skip the siblings even though the
        # order was never tried for them; _fire un-marks this one on failure so
        # a transient error can retry.
        seen.add(new_fqns[0])
        profile_id = self._armed[alert_id]["profile_id"]
        self._in_flight.add(alert_id)
        # Fire-and-forget - the result is recorded in self._results.
        asyncio.create_task(
            self._fire(alert_id, plan_code, new_fqns[0], profile_id, account_id)
        )

    async def _fire(
        self, alert_id: str, plan_code: str, fqn: str, profile_id: str,
        account_id: str | None = None,
    ) -> None:
        """Execute the rush order for one alert match.

        Imports are local to avoid a circular import at module load time
        (checkout.py imports from monitor.py for SniperService).

        ``account_id`` selects the credential set — the alert's own
        account, so the sniper orders under the right region even if the
        active account has since changed.
        """
        try:
            from app.api.checkout import RushOrderRequest, _execute_rush_order
            from app.services.storage import get_storage

            storage = get_storage()
            profile = storage.load_profile(profile_id)
            if not profile:
                self._results[alert_id] = {
                    "status": "error", "message": f"profile {profile_id} not found"
                }
                return
            service = get_ovh_service(account_id)
            if not service.is_configured():
                self._results[alert_id] = {"status": "error", "message": "OVH not configured"}
                return

            # Profiles store datacenters as a comma-separated string.
            dcs = []
            if profile.get("datacenters"):
                dcs = [d.strip() for d in profile["datacenters"].split(",") if d.strip()]

            req = RushOrderRequest(
                plan_code=profile["plan_code"],
                fqn=profile["fqn"],
                ram=profile.get("ram") or None,
                storage=profile.get("storage") or None,
                bandwidth=profile.get("bandwidth") or None,
                datacenters=dcs,
                region=profile["region"],
                os=profile["os"],
                duration=profile["duration"],
                auto_pay=bool(profile.get("auto_pay")),
                waive_retractation=bool(profile.get("waive_retractation")),
                max_price=profile.get("max_price"),
            )
            result = await _execute_rush_order(service, req)
            storage.log_order(
                order_id=result.get("orderId"),
                cart_id="",
                plan_code=profile["plan_code"],
                status=None,
                url=result.get("url"),
                placed_at=datetime.now(timezone.utc),
                account_id=account_id,
            )
            self._results[alert_id] = {
                "status": "ordered",
                "order_id": result.get("orderId"),
                "url": result.get("url"),
            }
            logger.info(
                "sniper auto-ordered %s (%s) - order %s",
                profile["plan_code"], fqn, result.get("orderId"),
            )
            # Disarm after a successful fire - caller must re-arm to fire again.
            self._armed.pop(alert_id, None)
        except Exception as e:
            logger.error("sniper order failed for %s", alert_id, exc_info=True)
            self._results[alert_id] = {"status": "error", "message": str(e)}
            # Un-blacklist the FQN so a transient failure (OVH 500, network
            # blip, momentary price spike) doesn't permanently stop the
            # sniper from retrying it on the next poll cycle.
            if alert_id in self._armed:
                self._armed[alert_id]["fqns_seen"].discard(fqn)
        finally:
            self._in_flight.discard(alert_id)


_sniper_service: SniperService | None = None


def get_sniper_service() -> SniperService:
    """Return the shared SniperService singleton, creating it on first use."""
    global _sniper_service
    if _sniper_service is None:
        _sniper_service = SniperService()
    return _sniper_service


class MonitorService:
    """Background poller + alert registry + SSE subscriber queues.

    One poller serves all SSE clients. State is guarded by an asyncio.Lock;
    OVH fetches happen outside the lock so slow API calls don't block
    alert mutations.
    """

    def __init__(self) -> None:
        # Every account's alerts, keyed by alert id. The API scopes reads to
        # the active account (get_alerts_for_account); the poller uses them all.
        self._alerts: dict[str, StockAlert] = {}
        # Per-plan state, keyed by (account_id, plan_code).
        self._stock_cache: dict[tuple[str | None, str], list[StockStatus]] = {}
        self._last_stock: dict[tuple[str | None, str], dict[str, bool]] = {}
        # Plans whose stock baseline has been recorded at least once. A
        # plan's FIRST poll after startup only primes the baseline: without
        # this, an empty _last_stock makes every in-stock config look
        # "newly available", re-firing notifications on each restart.
        # Armed snipers still fire on the first cycle (they exist to order
        # ASAP); only notifications/SSE/stock events are suppressed. An
        # account switch no longer clears this — the account was being
        # polled all along, so its baseline is still valid.
        self._primed: set[tuple[str | None, str]] = set()
        # True when the last cycle used the batched region-wide fetch for at
        # least one account; _run() then clamps the sleep to
        # BATCH_MIN_POLL_INTERVAL.
        self._last_cycle_batched = False
        self._batch_clamp_logged = False
        # Per-account master switch. False = this poller does NO OVH work for
        # that account: no stock polling, no region ticker, no price/promo
        # scan, no sniper fire. Defaults to True for accounts it hasn't seen.
        self._monitoring_enabled: dict[str | None, bool] = {}
        # Region restock ticker, per account: when enabled, every batch cycle
        # diffs that account's ENTIRE region (not just watched plans), logs
        # all transitions, and broadcasts a compact region_restock SSE event.
        # Observational only — alerts/snipers still fire on watched plans.
        self._region_enabled: dict[str | None, bool] = {}
        self._last_region_avail: dict[str | None, dict[str, set[str]]] = {}
        self._region_primed: set[str | None] = set()
        # region_restock events (one per ticking account) queued for _run's
        # next broadcast.
        self._region_events: list[dict[str, Any]] = []
        # account_id -> label, for tagging SSE events and notifications.
        self._account_labels: dict[str | None, str] = {}
        # Monotonic timestamp of the last stock-event prune (hourly).
        self._last_prune = 0.0
        # Monotonic timestamp of the last price/promo catalog check.
        self._last_price_check = 0.0
        # Monotonic timestamp of the last delivery watch (order status +
        # owned-server diff). Separate cadence from the price check.
        self._last_order_check = 0.0
        self._poll_interval = 3  # clamped to [1, 60]
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._subscribers: list[asyncio.Queue] = []
        self._storage = None

    def _storage_get(self):
        """Lazily fetch Storage. Returns None if unavailable.

        On transient failure, keeps self._storage = None so the next call
        retries instead of permanently disabling persistence.
        """
        if self._storage is None:
            try:
                from app.services.storage import get_storage
                self._storage = get_storage()
            except Exception:
                logger.warning("storage unavailable; alerts will not persist", exc_info=True)
                return None  # don't cache the failure — retry next call
        return self._storage

    def is_running(self) -> bool:
        """True while the background poller task is alive.

        Reflects the poller itself, not the monitor_enabled setting — they
        diverge if start() failed or the task died.
        """
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Load persisted alerts + settings, then spawn the background poller."""
        await self._load_from_storage()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    @staticmethod
    def _alerts_from_rows(
        rows: list[dict[str, Any]],
        active_id: str | None,
        known_accounts: set[str],
    ) -> dict[str, StockAlert]:
        """Build the in-memory alert map from storage rows.

        A legacy row with no account_id is attributed to the active
        account. Rows belonging to a DELETED account are skipped: deleting
        an account doesn't delete its data rows (its history stays
        queryable), but nothing can ever poll them again — keeping them
        would inflate the alert counts and the sniper status with entries
        that have no credentials.
        """
        out: dict[str, StockAlert] = {}
        for row in rows:
            account_id = row.get("account_id") or active_id
            if account_id is not None and account_id not in known_accounts:
                continue
            out[row["id"]] = StockAlert(
                id=row["id"],
                plan_code=row["plan_code"],
                fqn_pattern=row["fqn_pattern"],
                enabled=row["enabled"],
                notified_at=row["notified_at"],
                auto_order_profile_id=row.get("auto_order_profile_id"),
                account_id=account_id,
            )
        return out

    def _refresh_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """Refresh the cached account labels + per-account monitoring flags.

        The ``None`` key (no account row yet — a fresh install or a test)
        has nothing to read from, so whatever is in memory is preserved.
        """
        labels = {a["id"]: a["label"] for a in accounts}
        region = {a["id"]: bool(a.get("region_ticker_enabled")) for a in accounts}
        # Absent column (an old row read before the migration) reads as
        # monitored — the flag defaults to on, matching the pre-flag
        # behaviour where every account with alerts was polled.
        monitoring = {
            a["id"]: bool(a.get("monitoring_enabled", True)) for a in accounts
        }
        for cached, fresh in ((self._region_enabled, region),
                              (self._monitoring_enabled, monitoring)):
            if None in cached:
                fresh[None] = cached[None]
        self._account_labels = labels
        self._region_enabled = region
        self._monitoring_enabled = monitoring

    def _account_label(self, account_id: str | None) -> str | None:
        """Human label for an account, memoised. Falls back to a storage
        lookup so an account created after startup is still named."""
        if account_id is None:
            return None
        label = self._account_labels.get(account_id)
        if label is None:
            storage = self._storage_get()
            if storage:
                try:
                    acct = storage.get_account(account_id)
                except Exception:
                    return None
                if acct:
                    label = acct["label"]
                    self._account_labels[account_id] = label
        return label

    async def _load_from_storage(self) -> None:
        """Load every account's alerts + settings from SQLite on startup.

        Alerts from ALL accounts are watched (each carries its own
        ``account_id``), so the poller keeps building history and firing
        alerts for accounts that are not currently active.
        """
        storage = self._storage_get()
        if not storage:
            return
        try:
            active_id = storage.get_active_account_id()
            accounts = storage.list_accounts()
            loaded = storage.load_alerts()
            self._alerts.update(
                self._alerts_from_rows(
                    loaded, active_id, {a["id"] for a in accounts}
                )
            )
            interval_str = storage.get_setting("poll_interval")
            if interval_str:
                self.set_poll_interval(int(interval_str))
            self._refresh_accounts(accounts)
            if loaded:
                logger.info(
                    "loaded %d alerts from storage across %d account(s)",
                    len(loaded),
                    len({a.account_id for a in self._alerts.values()}),
                )
        except Exception:
            logger.warning("failed to load alerts from storage", exc_info=True)

    async def stop(self) -> None:
        """Cancel the background poller. Safe to call multiple times.

        Uses a timeout so shutdown doesn't hang if the poller is mid-way
        through an OVH API call (asyncio.to_thread can't be cancelled
        mid-flight).
        """
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._task = None

    async def reload(self) -> None:
        """Re-sync alerts and account metadata from storage.

        Called by the accounts API whenever an account is switched, added,
        updated or deleted. Stock baselines are deliberately PRESERVED for
        accounts that still exist: the poller watches every account
        regardless of which one is active, so a switch must not re-prime —
        that would drop a cycle of edges and re-log everything already in
        stock. Only state belonging to a deleted account, or to a plan
        nobody watches any more, is pruned.
        """
        storage = self._storage_get()
        if not storage:
            return
        try:
            alerts = await asyncio.to_thread(storage.load_alerts)
            accounts = await asyncio.to_thread(storage.list_accounts)
            active_id = await asyncio.to_thread(storage.get_active_account_id)
        except Exception:
            logger.warning("failed to reload alerts from storage", exc_info=True)
            return
        known = {a["id"] for a in accounts}
        async with self._lock:
            self._alerts = self._alerts_from_rows(alerts, active_id, known)
            self._refresh_accounts(accounts)
            watched = {
                (a.account_id, a.plan_code) for a in self._alerts.values()
            }
            for store in (self._stock_cache, self._last_stock):
                for key in [k for k in store if k not in watched]:
                    store.pop(key, None)
            self._primed &= watched
            # The None key is the pre-account bucket, never a deleted account.
            live = known | {None}
            for aid in [k for k in self._last_region_avail if k not in live]:
                self._last_region_avail.pop(aid, None)
            self._region_primed &= live

    async def _run(self) -> None:
        """Main poll loop: poll, broadcast, sleep, repeat.

        Runs forever until the task is cancelled. Exceptions in a single
        cycle are logged but do not stop the loop - the next cycle will
        try again after `_poll_interval` seconds.
        """
        while True:
            try:
                changes = await self._poll_once()
                # Queue items are either a list of plan diffs (wrapped as a
                # stock_update by the SSE handler) or a pre-typed dict event
                # like region_restock (passed through as-is).
                items: list[Any] = []
                if changes:
                    items.append(changes)
                if self._region_events:
                    items.extend(self._region_events)
                    self._region_events = []
                for item in items:
                    self._publish(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("monitor poll cycle failed")
            # Hourly best-effort retention prune; never stops the loop.
            try:
                await self._maybe_prune_events()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stock-event prune failed")
            # Periodic price-watch + promo scan (one catalog fetch).
            try:
                await self._maybe_check_prices_and_promos()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("price/promo check failed")
            # Periodic delivery watch (order status + owned servers).
            try:
                await self._maybe_check_orders_and_servers()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("order/server check failed")
            await asyncio.sleep(self._effective_sleep())

    def _publish(self, item: Any) -> None:
        """Fan one event out to every connected SSE client.

        Slow subscribers (full queue) are dropped with a warning rather than
        blocking the caller. Used by the poll loop and by the catalog watch,
        which runs after the loop's broadcast step and would otherwise have
        to wait a whole cycle to be seen.
        """
        for q in list(self._subscribers):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                logger.warning("dropping stock update for slow subscriber")

    async def subscribe(self) -> asyncio.Queue:
        """Register a new SSE client. Returns the queue it should await."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        """Deregister a queue when its SSE client disconnects."""
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    async def add_alert(self, plan_code: str, fqn_pattern: str = "*") -> StockAlert:
        """Create a new alert. Raises DuplicateAlertError if (plan, pattern) exists.

        The alert is bound to the currently active account so the sniper
        orders under the right region.
        """
        storage = self._storage_get()
        account_id = storage.get_active_account_id() if storage else None
        async with self._lock:
            for existing in self._alerts.values():
                # Scoped to the account, mirroring the DB's
                # UNIQUE(plan_code, fqn_pattern, account_id) — two accounts
                # may legitimately watch the same plan/pattern.
                if (
                    existing.account_id == account_id
                    and existing.plan_code == plan_code
                    and existing.fqn_pattern == fqn_pattern
                ):
                    raise DuplicateAlertError(
                        f"Alert already exists for {plan_code}:{fqn_pattern}"
                    )
            alert_id = str(uuid.uuid4())
            alert = StockAlert(
                id=alert_id, plan_code=plan_code, fqn_pattern=fqn_pattern,
                account_id=account_id,
            )
            self._alerts[alert_id] = alert
        if storage:
            try:
                storage.upsert_alert(
                    alert_id, plan_code, fqn_pattern, True, None,
                    account_id=account_id,
                )
            except Exception:
                logger.warning("failed to persist alert %s", alert_id, exc_info=True)
        return alert

    async def remove_alert(self, alert_id: str) -> bool:
        """Delete an alert. Also clears stock cache if no other alerts watch the plan."""
        async with self._lock:
            alert = self._alerts.pop(alert_id, None)
            if alert is None:
                return False
            key = (alert.account_id, alert.plan_code)
            still_monitored = any(
                (a.account_id, a.plan_code) == key for a in self._alerts.values()
            )
            if not still_monitored:
                self._last_stock.pop(key, None)
                self._stock_cache.pop(key, None)
                self._primed.discard(key)
        # A deleted alert can never fire again — the poller no longer sees it
        # in _alerts — so drop the armed entry too.
        sniper = get_sniper_service()
        if sniper.is_armed(alert_id):
            sniper.disarm(alert_id)
            logger.info("disarmed sniper for deleted alert %s", alert_id)
        storage = self._storage_get()
        if storage:
            try:
                storage.delete_alert(alert_id)
            except Exception:
                logger.warning("failed to delete alert %s", alert_id, exc_info=True)
        return True

    def get_alerts(self) -> list[StockAlert]:
        """Return every account's alerts (enabled and disabled).

        Callers rendering the UI want ``get_alerts_for_account`` instead —
        this is the poller's / sniper status' view.
        """
        return list(self._alerts.values())

    def get_alerts_for_account(self, account_id: str | None) -> list[StockAlert]:
        """Return one account's alerts (enabled and disabled)."""
        return [a for a in self._alerts.values() if a.account_id == account_id]

    def get_alert(self, alert_id: str) -> StockAlert | None:
        return self._alerts.get(alert_id)

    async def set_alert_enabled(self, alert_id: str, enabled: bool) -> StockAlert | None:
        """Toggle an alert on/off. Disabled alerts do not participate in polling."""
        async with self._lock:
            alert = self._alerts.get(alert_id)
            if alert is not None:
                alert.enabled = enabled
        if alert is not None and not enabled:
            # A disabled alert is never polled, so an armed sniper on it would
            # sit "armed" but dead. Pausing an alert therefore disarms its
            # sniper; the API surfaces this so the UI can tell the user.
            # (Deliberate semantic: a "paused" alert must never silently
            # auto-order.)
            sniper = get_sniper_service()
            if sniper.is_armed(alert_id):
                sniper.disarm(alert_id)
                logger.info("disarmed sniper for disabled alert %s", alert_id)
        storage = self._storage_get()
        if storage and alert is not None:
            try:
                storage.set_alert_enabled(alert_id, enabled)
            except Exception:
                logger.warning("failed to persist alert %s enable=%s", alert_id, enabled, exc_info=True)
        return alert

    def set_poll_interval(self, seconds: int) -> int:
        """Set the poll interval, clamped to [1, 60] seconds. Persists to storage."""
        self._poll_interval = max(1, min(60, seconds))
        storage = self._storage_get()
        if storage:
            try:
                storage.set_setting("poll_interval", str(self._poll_interval))
            except Exception:
                logger.warning("failed to persist poll_interval", exc_info=True)
        return self._poll_interval

    def get_poll_interval(self) -> int:
        return self._poll_interval

    def is_region_enabled(self, account_id: str | None = None) -> bool:
        """Whether the region ticker is on for an account (default: active)."""
        if account_id is None:
            account_id = self._active_account_id()
        return self._region_enabled.get(account_id, False)

    def is_monitoring_enabled(self, account_id: str | None = None) -> bool:
        """Whether the poller does any work for an account (default: active).

        Unknown accounts default to True: monitoring is opt-out, and an
        account the cache hasn't seen yet (created since the last reload)
        must not be silently ignored by the poller.
        """
        if account_id is None:
            account_id = self._active_account_id()
        return self._monitoring_enabled.get(account_id, True)

    async def set_monitoring_enabled(
        self, enabled: bool, account_id: str | None = None
    ) -> None:
        """Turn all background work for one account on/off (default: active).

        Turning it back on drops that account's baselines so the next cycle
        primes SILENTLY: while it was off, stock moved without being
        observed, so diffing against the stale baseline would report a burst
        of "newly available" configs that are simply what is in stock now.
        """
        if account_id is None:
            account_id = self._active_account_id()
        async with self._lock:
            self._monitoring_enabled[account_id] = enabled
            self._forget_account_state(account_id)
        storage = self._storage_get()
        # account_id is None only before any account exists (fresh install
        # or a test); there is no row to persist the flag on.
        if storage and account_id is not None:
            try:
                storage.set_account_monitoring(account_id, enabled)
            except Exception:
                logger.warning(
                    "failed to persist monitoring flag for account %s",
                    account_id, exc_info=True,
                )
        logger.info(
            "monitoring %s for account %s",
            "enabled" if enabled else "disabled", account_id,
        )

    def _forget_account_state(self, account_id: str | None) -> None:
        """Drop one account's stock/region baselines. Caller holds _lock."""
        for store in (self._stock_cache, self._last_stock):
            for key in [k for k in store if k[0] == account_id]:
                store.pop(key, None)
        self._primed = {k for k in self._primed if k[0] != account_id}
        self._last_region_avail.pop(account_id, None)
        self._region_primed.discard(account_id)

    def _active_account_id(self) -> str | None:
        storage = self._storage_get()
        return storage.get_active_account_id() if storage else None

    async def set_region_enabled(
        self, enabled: bool, account_id: str | None = None
    ) -> None:
        """Toggle one account's region restock ticker (default: the active
        account). Persists across restarts and re-primes that account's
        region baseline so re-enabling starts silent."""
        if account_id is None:
            account_id = self._active_account_id()
        async with self._lock:
            self._region_enabled[account_id] = enabled
            self._last_region_avail.pop(account_id, None)
            self._region_primed.discard(account_id)
        storage = self._storage_get()
        # account_id is None only before any account exists (fresh install
        # or a test); there is no row to persist the flag on.
        if storage and account_id is not None:
            try:
                storage.set_account_region_ticker(account_id, enabled)
            except Exception:
                logger.warning(
                    "failed to persist region ticker for account %s",
                    account_id, exc_info=True,
                )

    async def _process_region_snapshot(
        self,
        account_id: str | None,
        storage,
        watched: set[str],
        snapshot: dict[str, set[str]],
    ) -> None:
        """Diff one account's region-wide stock snapshot and emit its event.

        First cycle primes silently (same rule as watched plans). After
        that: every transition is logged to stock_events (watched plans
        are skipped — the per-plan loop already logs them edge-accurately)
        and one compact ``region_restock`` SSE event is queued for
        broadcast, capped at 50 plans / 5 FQNs each so a sale-opening
        stampede doesn't produce a megabyte event.
        """
        now = datetime.now(timezone.utc)
        async with self._lock:
            if account_id not in self._region_primed:
                self._last_region_avail[account_id] = snapshot
                self._region_primed.add(account_id)
                logger.info(
                    "primed region-wide stock baseline for account %s "
                    "(%d plans orderable)",
                    account_id, len(snapshot),
                )
                return
            prev = self._last_region_avail.get(account_id, {})
            self._last_region_avail[account_id] = snapshot

        pending: list[tuple[str, str, str, datetime, str | None]] = []
        restocks: list[dict[str, Any]] = []
        for plan in sorted(set(prev) | set(snapshot)):
            new_fqns = snapshot.get(plan, set()) - prev.get(plan, set())
            gone_fqns = prev.get(plan, set()) - snapshot.get(plan, set())
            if plan not in watched:
                for fqn in sorted(new_fqns):
                    pending.append((plan, fqn, "available", now, account_id))
                for fqn in sorted(gone_fqns):
                    pending.append((plan, fqn, "unavailable", now, account_id))
            if new_fqns:
                restocks.append({"plan_code": plan, "fqns": sorted(new_fqns)[:5]})

        if storage and pending:
            try:
                await asyncio.to_thread(storage.log_stock_events, pending)
            except Exception:
                logger.debug("failed to log region stock events", exc_info=True)

        if restocks:
            async with self._lock:
                self._region_events.append({
                    "type": "region_restock",
                    "timestamp": now.isoformat(),
                    "account_id": account_id,
                    "account_label": self._account_label(account_id),
                    "restocks": restocks[:50],
                    "total_plans": len(restocks),
                })
            logger.info(
                "region restock (account %s): %d plan(s) gained stock",
                account_id, len(restocks),
            )

    async def _maybe_check_prices_and_promos(self) -> None:
        """Every ``price_check_interval`` seconds, for EVERY account: fetch
        that account's catalog once, evaluate its enabled price watches
        against it, and scan every plan's pricings for new promotions.
        Best-effort; never raises.

        Runs per-account (not just the active one) for the same reason the
        poller does — price history and promo detection must keep building
        for accounts the user isn't currently looking at.

        The interval is read DB-first (Settings → App) each cycle, so a
        change takes effect immediately without a restart."""
        interval = app_setting_int("price_check_interval")
        if interval <= 0:
            return
        if time.monotonic() - self._last_price_check < interval:
            return
        self._last_price_check = time.monotonic()
        storage = self._storage_get()
        if not storage:
            return
        accounts = await asyncio.to_thread(storage.list_accounts)
        for acct in accounts:
            # Same master switch as the stock poller: off means no OVH work
            # of any kind for this account.
            if not self._monitoring_enabled.get(acct["id"], True):
                continue
            service = get_ovh_service(acct["id"])
            if not service.is_configured():
                continue
            try:
                await self._check_prices_and_promos(service, storage)
            except Exception:
                # One account's catalog failing must not skip the others.
                logger.warning(
                    "price/promo check failed for account %s",
                    acct["id"], exc_info=True,
                )

    async def _check_prices_and_promos(self, service, storage) -> None:
        import hashlib
        import json

        from app.services.notifier import notify_price_drop, notify_promo
        from app.services.ovh_service import OVHService

        try:
            catalog = await asyncio.to_thread(service.fetch_catalog)
        except OVHServiceError:
            logger.debug("price/promo catalog fetch failed", exc_info=True)
            return
        now = datetime.now(timezone.utc)
        currency = service.default_currency_code()
        label = self._account_label(service.account_id)

        watches = await asyncio.to_thread(
            storage.load_price_watches, service.account_id, True
        )
        for w in watches:
            price = OVHService.plan_price_from_catalog(catalog, w["plan_code"])
            if price is None:
                continue
            try:
                await asyncio.to_thread(
                    storage.log_price, w["plan_code"], price, now,
                    service.account_id, currency,
                )
            except Exception:
                logger.debug("failed to log watched price", exc_info=True)
            # Fire when at/below threshold, but only when the price has
            # moved since the last notification — no spam while it sits
            # at the same level, yet a further drop re-alerts.
            if price <= w["threshold_ucents"] and price != w["last_notified_price"]:
                logger.info(
                    "price watch hit: %s at %d ucents (threshold %d)",
                    w["plan_code"], price, w["threshold_ucents"],
                )
                try:
                    await notify_price_drop(
                        w["plan_code"], price / 100_000_000,
                        w["threshold_ucents"] / 100_000_000, currency,
                        account_label=label,
                    )
                except Exception:
                    logger.warning("price-drop notify failed", exc_info=True)
                await asyncio.to_thread(
                    storage.mark_price_watch_notified, w["id"], price
                )

        # Promo scan over the same catalog. The promotions field is empty
        # outside sales and its populated shape is unverified — hash the
        # raw entry defensively and never let a weird shape raise.
        #
        # OVH attaches a campaign to every plan it covers, so one sale records
        # a row per plan code. Collect the new ones and notify once per
        # campaign; notifying inside the loop sent 17 identical messages for a
        # single flash sale. Group on the promo's `name`, which is stable
        # across a campaign — the payload hash is not, since it includes the
        # per-plan discount amount.
        new_campaigns: dict[str, dict[str, Any]] = {}
        for plan in catalog.get("plans", []):
            plan_code = plan.get("planCode") or ""
            for pricing in plan.get("pricings") or []:
                for promo in pricing.get("promotions") or []:
                    try:
                        payload = json.dumps(promo, sort_keys=True, default=str)
                    except Exception:
                        payload = repr(promo)
                    key = hashlib.sha256(payload.encode()).hexdigest()[:16]
                    try:
                        is_new = await asyncio.to_thread(
                            storage.record_promo, plan_code, key, payload,
                            service.account_id,
                        )
                    except Exception:
                        logger.debug("failed to record promo", exc_info=True)
                        continue
                    if not is_new:
                        continue
                    desc = None
                    name = None
                    if isinstance(promo, dict):
                        desc = promo.get("description") or promo.get("name")
                        name = promo.get("name")
                    desc = desc or payload[:160]
                    entry = new_campaigns.setdefault(
                        name or desc, {"description": desc, "plan_codes": []}
                    )
                    if plan_code and plan_code not in entry["plan_codes"]:
                        entry["plan_codes"].append(plan_code)

        for entry in new_campaigns.values():
            try:
                await notify_promo(
                    entry["description"], entry["plan_codes"],
                    account_label=label,
                )
            except Exception:
                logger.warning("promo notify failed", exc_info=True)

        # Same catalog, third consumer: which plans OVH added or retired.
        await self._diff_catalog(service, storage, catalog)

    async def _diff_catalog(self, service, storage, catalog: dict[str, Any]) -> None:
        """Diff this account's catalog plan codes against the stored snapshot,
        recording additions and removals (and optionally notifying).

        Piggybacks on the price/promo catalog fetch — no extra OVH call. The
        snapshot lives in SQLite, so a restart still compares against the last
        observed catalog instead of re-priming.
        """
        if not app_setting_bool("catalog_watch_enabled"):
            return
        from app.services.notifier import notify_catalog_change
        from app.services.ovh_service import OVHService

        current: dict[str, str | None] = {}
        for plan in catalog.get("plans", []):
            code = plan.get("planCode")
            if code:
                current[code] = plan.get("invoiceName")
        if not current:
            # An empty catalog is a bad response, never a retired region.
            logger.warning(
                "catalog watch: empty catalog for account %s; skipping diff",
                service.account_id,
            )
            return

        snapshot = await asyncio.to_thread(
            storage.load_catalog_snapshot, service.account_id
        )
        now = datetime.now(timezone.utc)
        currency = service.default_currency_code()

        def _added_row(code: str) -> dict[str, Any]:
            return {
                "plan_code": code,
                "invoice_name": current[code],
                "price_in_ucents": OVHService.plan_price_from_catalog(catalog, code),
                "currency_code": currency,
            }

        if not snapshot:
            # First scan for this account: record the baseline only. Reporting
            # ~700 plans as "added" would be an artefact of having no history.
            await asyncio.to_thread(
                storage.apply_catalog_diff, service.account_id,
                [{"plan_code": c, "invoice_name": current[c]} for c in current],
                [], [], now, False,
            )
            logger.info(
                "catalog watch primed: %d plans for account %s",
                len(current), service.account_id,
            )
            return

        added = [_added_row(c) for c in current if c not in snapshot]
        removed = [dict(snapshot[c]) for c in snapshot if c not in current]
        seen = [c for c in current if c in snapshot]
        if not added and not removed:
            return
        # A truncated catalog must not read as a mass retirement (same rule as
        # a failed batch availability fetch keeping every plan's baseline).
        if len(removed) > len(snapshot) / 2:
            logger.warning(
                "catalog watch: %d of %d plans missing for account %s — "
                "treating as a bad catalog response, snapshot left untouched",
                len(removed), len(snapshot), service.account_id,
            )
            return

        await asyncio.to_thread(
            storage.apply_catalog_diff, service.account_id,
            added, removed, seen, now,
        )
        label = self._account_label(service.account_id)
        if added:
            logger.info(
                "catalog watch: %d plan(s) added for account %s: %s",
                len(added), service.account_id,
                ", ".join(r["plan_code"] for r in added[:10]),
            )
        if removed:
            logger.info(
                "catalog watch: %d plan(s) removed for account %s: %s",
                len(removed), service.account_id,
                ", ".join(r["plan_code"] for r in removed[:10]),
            )
        self._publish({
            "type": "catalog_change",
            "account_id": service.account_id,
            "account_label": label,
            "added": [
                {"plan_code": r["plan_code"], "invoice_name": r["invoice_name"]}
                for r in added[:50]
            ],
            "removed": [
                {"plan_code": r["plan_code"], "invoice_name": r.get("invoice_name")}
                for r in removed[:50]
            ],
            "added_count": len(added),
            "removed_count": len(removed),
        })
        if app_setting_bool("catalog_watch_notify"):
            try:
                await notify_catalog_change(added, removed, account_label=label)
            except Exception:
                logger.warning("catalog change notify failed", exc_info=True)

    async def _maybe_check_orders_and_servers(self) -> None:
        """Every ``order_check_interval`` seconds, for EVERY account: re-check
        the status of its non-terminal orders and diff its dedicated-server
        list. Best-effort; never raises.

        This is what makes a delivery visible without the browser: the Orders
        and Servers tabs only fetch OVH when they are opened, so before this
        watch existed an order sat at whatever status it had when the tab was
        last viewed and OVH's own email was the only notice a server was ready.

        Runs per-account for the same reason the poller does, and honours the
        same per-account master switch — off means no OVH work at all.
        """
        interval = app_setting_int("order_check_interval")
        if interval <= 0:
            return
        if time.monotonic() - self._last_order_check < interval:
            return
        self._last_order_check = time.monotonic()
        storage = self._storage_get()
        if not storage:
            return
        accounts = await asyncio.to_thread(storage.list_accounts)
        for acct in accounts:
            if not self._monitoring_enabled.get(acct["id"], True):
                continue
            service = get_ovh_service(acct["id"])
            if not service.is_configured():
                continue
            for check in (self._check_orders, self._check_servers):
                try:
                    await check(service, storage)
                except Exception:
                    # One account (or one half of the watch) failing must not
                    # skip the rest.
                    logger.warning(
                        "%s failed for account %s",
                        check.__name__, acct["id"], exc_info=True,
                    )

    async def _check_orders(self, service, storage) -> None:
        """Re-check non-terminal orders and record/notify status transitions.

        Costs one ``/me/order`` list call plus one status call per pending
        order (capped by ``ORDER_STATUS_BUDGET``). Terminal orders are skipped
        outright, so a settled account costs exactly one call per cycle.

        **Notify only on a transition from a status we already knew.** An order
        id seen for the first time is recorded silently — otherwise a fresh
        install (or an account whose orders were all placed in the OVH manager)
        would fan out a notification for every historical delivered order the
        first time this ran. That is the same priming lesson as the catalog
        watch, expressed as a rule about transitions instead of a first-scan
        branch, so it also covers an account that gains orders later.
        """
        from app.services.notifier import notify_order_status

        date_from = (
            datetime.now(timezone.utc) - timedelta(days=ORDER_WATCH_DAYS)
        ).isoformat()
        try:
            ovh_ids = await asyncio.to_thread(service.list_orders, date_from, None)
        except OVHServiceError as e:
            if e.status_code != 404:
                raise
            ovh_ids = []

        local_rows = await asyncio.to_thread(
            storage.load_orders, 200, service.account_id
        )
        known: dict[int, str | None] = {
            r["order_id"]: r.get("status") for r in local_rows if r.get("order_id")
        }
        names: dict[int, str | None] = {
            r["order_id"]: (r.get("server_name") or r.get("plan_code") or None)
            for r in local_rows if r.get("order_id")
        }

        budget = ORDER_STATUS_BUDGET
        changes: list[dict[str, Any]] = []
        notify: list[dict[str, Any]] = []
        primed = 0
        # Newest first: a just-placed order is the one the user is waiting on.
        for oid in sorted(ovh_ids, reverse=True):
            previous = known.get(oid)
            if previous in TERMINAL_ORDER_STATUSES:
                continue
            if budget <= 0:
                break
            budget -= 1
            try:
                status = str(await asyncio.to_thread(service.get_order_status, oid))
            except OVHServiceError:
                logger.debug("order status fetch failed for %s", oid, exc_info=True)
                continue
            if status == previous:
                continue
            await asyncio.to_thread(
                storage.upsert_order_enriched, oid,
                status=status, account_id=service.account_id,
            )
            if oid not in known:
                # First sighting — record the baseline, say nothing.
                primed += 1
                continue
            logger.info(
                "order %s (account %s): %s -> %s",
                oid, service.account_id, previous or "?", status,
            )
            entry = {
                "order_id": oid, "name": names.get(oid),
                "status": status, "previous": previous,
            }
            changes.append(entry)
            if status in NOTIFY_ORDER_STATUSES:
                notify.append(entry)

        if primed:
            logger.info(
                "order watch primed: %d order(s) for account %s",
                primed, service.account_id,
            )
        if not changes:
            return
        label = self._account_label(service.account_id)
        self._publish({
            "type": "order_update",
            "account_id": service.account_id,
            "account_label": label,
            "changes": changes,
        })
        for entry in notify:
            try:
                await notify_order_status(
                    entry["order_id"], entry["name"], entry["status"],
                    entry["previous"], account_label=label,
                )
            except Exception:
                logger.warning("order status notify failed", exc_info=True)

    async def _check_servers(self, service, storage) -> None:
        """Diff the account's dedicated-server list against the stored snapshot.

        One OVH call. A delivered order turns into a machine appearing here, so
        this is the signal that the thing the user actually ordered has landed.

        Unlike the catalog watch there is no "more than half missing" guard:
        that rule exists because a truncated ~700-plan catalog response is
        indistinguishable from a retired region, whereas ``/dedicated/server``
        returns a short, complete list where ``[]`` is a legitimate answer (an
        account may genuinely own no servers). A failed fetch raises and leaves
        the snapshot untouched — that is the guard that matters here.
        """
        from app.services.notifier import notify_server_change

        names = await asyncio.to_thread(service.list_dedicated_servers)
        current = [str(n) for n in (names or [])]

        snapshot = await asyncio.to_thread(
            storage.load_owned_servers, service.account_id
        )
        added = [n for n in current if n not in snapshot]
        removed = [n for n in snapshot if n not in current]
        seen = [n for n in current if n in snapshot]
        now = datetime.now(timezone.utc)

        # "No snapshot rows" is ambiguous — it means both "never scanned" and
        # "owns no servers" — so priming is tracked by an explicit marker.
        # Without it, an account that starts empty (the common case: you buy
        # your first server through this app) would treat that first delivery
        # as a baseline and never announce it.
        marker = f"server_watch_primed_{service.account_id}"
        primed = await asyncio.to_thread(storage.get_setting, marker)
        if not primed:
            await asyncio.to_thread(
                storage.apply_server_diff, service.account_id,
                current, [], [], now,
            )
            await asyncio.to_thread(storage.set_setting, marker, "true")
            logger.info(
                "server watch primed: %d server(s) for account %s",
                len(current), service.account_id,
            )
            return

        if not added and not removed:
            # Nothing to record. `last_seen` is deliberately not refreshed on a
            # no-op cycle (same as the catalog watch): nothing reads it, and a
            # write every interval per account would be pure churn.
            return

        await asyncio.to_thread(
            storage.apply_server_diff, service.account_id, added, removed, seen, now,
        )
        if added:
            logger.info(
                "server watch: %d server(s) added for account %s: %s",
                len(added), service.account_id, ", ".join(added[:10]),
            )
        if removed:
            logger.info(
                "server watch: %d server(s) removed for account %s: %s",
                len(removed), service.account_id, ", ".join(removed[:10]),
            )
        label = self._account_label(service.account_id)
        self._publish({
            "type": "server_change",
            "account_id": service.account_id,
            "account_label": label,
            "added": added[:50],
            "removed": removed[:50],
            "added_count": len(added),
            "removed_count": len(removed),
        })
        try:
            await notify_server_change(added, removed, account_label=label)
        except Exception:
            logger.warning("server change notify failed", exc_info=True)

    async def _maybe_prune_events(self) -> None:
        """Prune old stock events at most once an hour (best-effort)."""
        if time.monotonic() - self._last_prune < 3600:
            return
        self._last_prune = time.monotonic()
        storage = self._storage_get()
        if not storage:
            return
        # DB-first (Settings → App) with env fallback, read per prune.
        deleted = await asyncio.to_thread(
            storage.prune_stock_events,
            app_setting_int("stock_event_retention_days"),
            app_setting_int("stock_event_max_rows"),
        )
        if deleted:
            logger.info("pruned %d old stock events", deleted)

    def _effective_sleep(self) -> int:
        """The user's poll interval, clamped to BATCH_MIN_POLL_INTERVAL
        while batch mode is active (the region-wide fetch is too heavy
        for 1s cycles). Single-plan polling is never clamped."""
        sleep_for = self._poll_interval
        if self._last_cycle_batched and sleep_for < BATCH_MIN_POLL_INTERVAL:
            if not self._batch_clamp_logged:
                logger.info(
                    "batch polling active: poll interval clamped to %ds "
                    "(the region-wide fetch is too heavy for %ds cycles)",
                    BATCH_MIN_POLL_INTERVAL, sleep_for,
                )
                self._batch_clamp_logged = True
            return BATCH_MIN_POLL_INTERVAL
        return sleep_for

    def get_stock_diff(
        self,
        plan_code: str,
        new_statuses: list[StockStatus],
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Compute what changed since the last poll for one account's plan.

        Returns a dict with `newly_available`, `now_unavailable`, and
        `currently_available` FQN lists, plus a UTC timestamp. Also
        updates `_last_stock` to the new state - callers must hold `_lock`.
        """
        key = (account_id, plan_code)
        old_statuses = self._last_stock.get(key, {})
        new_available_fqns = {s.fqn for s in new_statuses if s.available}
        old_available_fqns = set(old_statuses.keys())

        newly_available = new_available_fqns - old_available_fqns
        now_unavailable = old_available_fqns - new_available_fqns

        self._last_stock[key] = {s.fqn: s.available for s in new_statuses}

        return {
            "plan_code": plan_code,
            "account_id": account_id,
            "account_label": self._account_label(account_id),
            "newly_available": sorted(newly_available),
            "now_unavailable": sorted(now_unavailable),
            "currently_available": sorted(new_available_fqns),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def _poll_once(self) -> list[dict[str, Any]]:
        """One poll cycle across EVERY account. Returns the diffs to
        broadcast to SSE clients.

        Alerts are grouped by account and each group is polled under its
        own credentials, concurrently — every OVHService has its own client
        lock, so the cycle costs the slowest account, not their sum.
        """
        # Snapshot what each account needs polled this cycle. Bail out before
        # building any OVH service when there's nothing to poll — service
        # construction does network I/O (endpoint/time fetch), so we avoid it
        # entirely when idle.
        async with self._lock:
            plans_by_account: dict[str | None, set[str]] = {}
            for alert in self._alerts.values():
                if alert.enabled:
                    plans_by_account.setdefault(alert.account_id, set()).add(
                        alert.plan_code
                    )
            # A ticking account polls even with no alerts (it watches
            # everything in its region).
            for account_id, on in self._region_enabled.items():
                if on:
                    plans_by_account.setdefault(account_id, set())
            # Single gate for the per-account master switch: dropping the
            # account here covers stock polling, the region ticker AND
            # sniper fire, since _poll_account drives all three.
            for account_id in [
                a for a in plans_by_account
                if not self._monitoring_enabled.get(a, True)
            ]:
                plans_by_account.pop(account_id, None)
        if not plans_by_account:
            return []

        account_ids = sorted(plans_by_account, key=lambda a: (a is not None, a or ""))
        results = await asyncio.gather(
            *(
                self._poll_one_account(aid, sorted(plans_by_account[aid]))
                for aid in account_ids
            ),
            return_exceptions=True,
        )

        changes: list[dict[str, Any]] = []
        batched = False
        for account_id, result in zip(account_ids, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                # One account's failure must never stop the others.
                logger.error(
                    "poll failed for account %s: %s", account_id, result,
                    exc_info=result,
                )
                continue
            account_changes, account_batched = result
            changes.extend(account_changes)
            batched = batched or account_batched
        self._last_cycle_batched = batched
        return changes

    async def _poll_one_account(
        self, account_id: str | None, plan_codes: list[str]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Resolve one account's service and poll it. Returns
        (changes, used_batch_fetch)."""
        # account_id is None only when no account row exists yet (fresh
        # install or a test) — that is exactly what the active-account
        # resolver returns for.
        service = (
            get_active_ovh_service() if account_id is None
            else get_ovh_service(account_id)
        )
        if not service.is_configured():
            return [], False
        return await self._poll_account(
            account_id, service, plan_codes,
            self._region_enabled.get(account_id, False),
        )

    async def _fetch_availability_map(
        self, service, plan_codes: list[str], region_enabled: bool
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, set[str]] | None, bool]:
        """Fetch orderable configs for each plan, keyed by plan_code.

        Returns ``(availability_map, region_snapshot, used_batch_fetch)``.
        The region snapshot is None unless the ticker is on for this
        account. Nothing is stashed on ``self`` — accounts are polled
        concurrently, so per-cycle state must stay local to the call.

        Two strategies:
        - **Per-plan** (single watched plan): one filtered availabilities
          call — the smallest, fastest request, preserving 1s snipe
          fidelity. A failed plan is omitted from the map (baseline kept).
        - **Batch** (2+ plans): ONE unfiltered call returns the whole
          region (~28k entries / ~1.3s, verified live), replacing N
          round-trips that would otherwise serialise behind the
          per-account client lock. Plans with no orderable entry map to
          [] (genuinely out of stock, so sell-out diffs still fire). If
          the batch call fails, an empty map is returned (all baselines
          kept).
        """
        if len(plan_codes) <= 1 and not region_enabled:
            out: dict[str, list[dict[str, Any]]] = {}
            for plan_code in plan_codes:
                try:
                    out[plan_code] = await asyncio.to_thread(
                        service.get_availability, plan_code
                    )
                except OVHServiceError:
                    logger.debug(
                        "availability fetch failed for %s", plan_code,
                        exc_info=True,
                    )
            return out, None, False

        try:
            entries = await asyncio.to_thread(service.get_stock, None)
        except OVHServiceError:
            logger.debug("batch availability fetch failed", exc_info=True)
            return {}, None, True
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            if orderable_entry(entry):
                grouped.setdefault(entry.get("planCode", ""), []).append(
                    {"fqn": entry.get("fqn", "")}
                )
        snapshot = None
        if region_enabled:
            # Handed to _process_region_snapshot.
            snapshot = {
                pc: {c["fqn"] for c in configs}
                for pc, configs in grouped.items()
                if pc
            }
        return {pc: grouped.get(pc, []) for pc in plan_codes}, snapshot, True

    async def _poll_account(
        self,
        account_id: str | None,
        service,
        plan_codes: list[str],
        region_enabled: bool,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Poll one account's watched plans and process diffs/alerts/snipers.

        Returns (changes, used_batch_fetch).
        """
        changes: list[dict[str, Any]] = []
        storage = self._storage_get()
        avail_map, region_snapshot, batched = await self._fetch_availability_map(
            service, plan_codes, region_enabled
        )

        if region_snapshot is not None:
            try:
                await self._process_region_snapshot(
                    account_id, storage, set(plan_codes), region_snapshot
                )
            except Exception:
                logger.exception("region ticker processing failed")

        for plan_code in plan_codes:
            avail_configs = avail_map.get(plan_code)
            if avail_configs is None:
                # Fetch failed for this plan — keep its baseline untouched.
                continue
            try:
                now = datetime.now(timezone.utc)
                # OVH returns only currently-orderable configs, so every
                # returned FQN is implicitly `available=True`.
                new_statuses = [
                    StockStatus(
                        plan_code=plan_code,
                        fqn=c.get("fqn", ""),
                        available=True,
                        last_check=now,
                    )
                    for c in avail_configs
                ]
                pending_events: list[tuple[str, str, str, datetime, str | None]] = []
                # Notifications are EDGE-triggered (a config that just became
                # available) and suppressed on the priming cycle; snipers are
                # LEVEL-triggered (any matching config currently orderable),
                # because a sniper's job is to order the moment stock exists —
                # including stock that was already there when it was armed.
                # SniperService's per-arm `fqns_seen` set stops repeat orders.
                notify_targets: list[tuple[StockAlert, list[str]]] = []
                snipe_targets: list[tuple[StockAlert, list[str]]] = []
                sniper = get_sniper_service()
                key = (account_id, plan_code)
                async with self._lock:
                    first_cycle = key not in self._primed
                    diff = self.get_stock_diff(plan_code, new_statuses, account_id)
                    self._stock_cache[key] = new_statuses
                    self._primed.add(key)
                    if first_cycle and diff["currently_available"]:
                        # Prime silently: record the baseline without SSE
                        # broadcasts, notifications, or stock events — an
                        # empty baseline would otherwise report everything
                        # already in stock as "newly available" on every
                        # restart.
                        logger.info(
                            "primed stock baseline for %s (account %s, %d "
                            "configs available); notifications suppressed",
                            plan_code, account_id,
                            len(diff["currently_available"]),
                        )
                    for alert in self._alerts.values():
                        if (
                            alert.account_id != account_id
                            or alert.plan_code != plan_code
                            or not alert.enabled
                        ):
                            continue
                        if sniper.is_armed(alert.id):
                            matched_now = [
                                fqn
                                for fqn in diff["currently_available"]
                                if self._matches_pattern(fqn, alert.fqn_pattern)
                            ]
                            if matched_now:
                                snipe_targets.append((alert, matched_now))
                        if first_cycle or not diff["newly_available"]:
                            continue
                        matched_new = [
                            fqn
                            for fqn in diff["newly_available"]
                            if self._matches_pattern(fqn, alert.fqn_pattern)
                        ]
                        if matched_new:
                            alert.notified_at = now
                            notify_targets.append((alert, matched_new))

                    if not first_cycle:
                        # Broadcast on any change (restock or sell-out) so SSE
                        # clients can keep their stock indicators accurate -
                        # not just when something newly became available.
                        if diff["newly_available"] or diff["now_unavailable"]:
                            changes.append(diff)
                            logger.info(
                                "stock change %s (account %s): +%d available, "
                                "-%d unavailable",
                                plan_code, account_id,
                                len(diff["newly_available"]),
                                len(diff["now_unavailable"]),
                            )

                        # Collect stock events for the historical-patterns view.
                        # Persisted below, after the lock is released — SQLite
                        # writes are blocking disk I/O and must not stall alert
                        # mutations (or the event loop) from inside the lock.
                        for fqn in diff["newly_available"]:
                            pending_events.append(
                                (plan_code, fqn, "available", now, account_id)
                            )
                        for fqn in diff["now_unavailable"]:
                            pending_events.append(
                                (plan_code, fqn, "unavailable", now, account_id)
                            )

                if storage and pending_events:
                    try:
                        await asyncio.to_thread(
                            storage.log_stock_events, pending_events
                        )
                    except Exception:
                        logger.debug("failed to log stock events", exc_info=True)

                # Persist notified_at so it survives restarts (the in-memory
                # update above is lost otherwise). Off-loop, outside the lock.
                if storage:
                    for alert_obj, _ in notify_targets:
                        try:
                            await asyncio.to_thread(
                                storage.set_notified_at, alert_obj.id, now
                            )
                        except Exception:
                            logger.debug(
                                "failed to persist notified_at for %s",
                                alert_obj.id, exc_info=True,
                            )

                # Fan out notifications + sniper *outside* the lock so we
                # don't block other alert mutations during a slow webhook.
                if notify_targets:
                    from app.services.notifier import notify_stock_alert
                    price = None
                    if storage:
                        price_ucents = await asyncio.to_thread(
                            storage.latest_price, plan_code, account_id,
                        )
                        if price_ucents is not None:
                            price = price_ucents / 100_000_000
                    for _alert_obj, matched_fqns in notify_targets:
                        try:
                            await notify_stock_alert(
                                plan_code, matched_fqns, price,
                                currency_code=service.default_currency_code(),
                                account_label=self._account_label(account_id),
                            )
                        except Exception:
                            logger.warning("notifier failed for %s", plan_code, exc_info=True)

                for alert_obj, matched_fqns in snipe_targets:
                    try:
                        await sniper.maybe_fire(
                            alert_obj.id, plan_code, matched_fqns,
                            account_id=alert_obj.account_id,
                        )
                    except Exception:
                        logger.warning("sniper fire failed for %s", alert_obj.id, exc_info=True)

            except Exception:
                # One plan failing should not stop the others.
                logger.exception("poll processing failed for %s", plan_code)

        return changes, batched

    @staticmethod
    def _matches_pattern(fqn: str, pattern: str) -> bool:
        """Glob match an FQN against a pattern. `*` matches everything.

        Uses `fnmatch` so patterns like `24sk10*ssd*` work as expected.
        Comparison is case-insensitive.
        """
        if pattern == "*":
            return True
        return fnmatch(fqn.lower(), pattern.lower())


_monitor_service: MonitorService | None = None


def get_monitor_service() -> MonitorService:
    """Return the shared MonitorService singleton, creating it on first use."""
    global _monitor_service
    if _monitor_service is None:
        _monitor_service = MonitorService()
    return _monitor_service
