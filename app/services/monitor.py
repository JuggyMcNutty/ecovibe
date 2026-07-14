"""Stock monitoring engine + sniper auto-orderer.

MonitorService runs a single background poller that checks OVH for stock
changes and broadcasts to SSE subscribers. SniperService fires rush orders
automatically when an armed alert matches. State is in-memory, mirrored
to SQLite.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any

from app.config import get_settings
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

    def armed_watches(self) -> list[tuple[str, str | None, str | None, str | None]]:
        """Return (alert_id, plan_code, fqn_pattern, account_id) for each armed
        sniper, for the poller's cross-account sweep. Entries armed before this
        info was captured have plan_code None and are skipped by the sweep."""
        return [
            (aid, v.get("plan_code"), v.get("fqn_pattern"), v.get("account_id"))
            for aid, v in self._armed.items()
        ]

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
        self._alerts: dict[str, StockAlert] = {}
        self._stock_cache: dict[str, list[StockStatus]] = {}
        self._last_stock: dict[str, dict[str, bool]] = {}
        # Plans whose stock baseline has been recorded at least once. A
        # plan's FIRST poll after startup/reload only primes the baseline:
        # without this, an empty _last_stock makes every in-stock config
        # look "newly available", re-firing notifications on each restart
        # or account switch. Armed snipers still fire on the first cycle
        # (they exist to order ASAP); only notifications/SSE/stock events
        # are suppressed.
        self._primed: set[str] = set()
        # True when the last cycle used the batched region-wide fetch;
        # _run() then clamps the sleep to BATCH_MIN_POLL_INTERVAL.
        self._last_cycle_batched = False
        self._batch_clamp_logged = False
        # Monotonic timestamp of the last stock-event prune (hourly).
        self._last_prune = 0.0
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

    async def start(self) -> None:
        """Load persisted alerts + settings, then spawn the background poller."""
        await self._load_from_storage()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _load_from_storage(self) -> None:
        """Reload alerts and poll_interval from SQLite on startup.

        Alerts are scoped to the active account (multi-account model):
        only the active account's alerts are watched, so switching
        accounts only sees that account's monitors.
        """
        storage = self._storage_get()
        if not storage:
            return
        try:
            active_id = storage.get_active_account_id()
            loaded = storage.load_alerts(account_id=active_id)
            for a in loaded:
                self._alerts[a["id"]] = StockAlert(
                    id=a["id"],
                    plan_code=a["plan_code"],
                    fqn_pattern=a["fqn_pattern"],
                    enabled=a["enabled"],
                    notified_at=a["notified_at"],
                    auto_order_profile_id=a.get("auto_order_profile_id"),
                    account_id=a.get("account_id") or active_id,
                )
            interval_str = storage.get_setting("poll_interval")
            if interval_str:
                self.set_poll_interval(int(interval_str))
            if loaded:
                logger.info("loaded %d alerts from storage", len(loaded))
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
        """Reload alerts + clear stock caches after an account switch.

        Drops all in-memory alerts and the stock cache, then re-reads
        the active account's alerts from storage. Called by the accounts
        API when the active account changes so stale cross-region stock
        data doesn't bleed into the new account's view.
        """
        async with self._lock:
            self._alerts.clear()
            self._stock_cache.clear()
            self._last_stock.clear()
            self._primed.clear()
        await self._load_from_storage()

    async def _run(self) -> None:
        """Main poll loop: poll, broadcast, sleep, repeat.

        Runs forever until the task is cancelled. Exceptions in a single
        cycle are logged but do not stop the loop - the next cycle will
        try again after `_poll_interval` seconds.
        """
        while True:
            try:
                changes = await self._poll_once()
                if changes:
                    # Fan out to every connected SSE client. Slow subscribers
                    # (full queue) are dropped with a warning rather than
                    # blocking the poller.
                    for q in list(self._subscribers):
                        try:
                            q.put_nowait(changes)
                        except asyncio.QueueFull:
                            logger.warning("dropping stock update for slow subscriber")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("monitor poll cycle failed")
            # Keep snipers armed under non-active accounts firing too. Isolated
            # from the main poll so a sweep failure never stops SSE monitoring.
            try:
                await self._sweep_snipers()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sniper sweep failed")
            # Hourly best-effort retention prune; never stops the loop.
            try:
                await self._maybe_prune_events()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stock-event prune failed")
            await asyncio.sleep(self._effective_sleep())

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
                if existing.plan_code == plan_code and existing.fqn_pattern == fqn_pattern:
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
            still_monitored = any(
                a.plan_code == alert.plan_code for a in self._alerts.values()
            )
            if not still_monitored:
                self._last_stock.pop(alert.plan_code, None)
                self._stock_cache.pop(alert.plan_code, None)
                self._primed.discard(alert.plan_code)
        # A deleted alert can never fire again — neither the poller (alert
        # gone from _alerts) nor the sweep (skips the active account) would
        # ever trigger its sniper, so drop the armed entry too.
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
        """Return all alerts (enabled and disabled)."""
        return list(self._alerts.values())

    def get_alert(self, alert_id: str) -> StockAlert | None:
        return self._alerts.get(alert_id)

    async def set_alert_enabled(self, alert_id: str, enabled: bool) -> StockAlert | None:
        """Toggle an alert on/off. Disabled alerts do not participate in polling."""
        async with self._lock:
            alert = self._alerts.get(alert_id)
            if alert is not None:
                alert.enabled = enabled
        if alert is not None and not enabled:
            # A disabled alert is not polled, and the sweep skips the active
            # account — an armed sniper on it would sit "armed" but dead.
            # Pausing an alert therefore disarms its sniper; the API surfaces
            # this so the UI can tell the user. (Deliberate semantic: a
            # "paused" alert must never silently auto-order.)
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

    async def _maybe_prune_events(self) -> None:
        """Prune old stock events at most once an hour (best-effort)."""
        if time.monotonic() - self._last_prune < 3600:
            return
        self._last_prune = time.monotonic()
        storage = self._storage_get()
        if not storage:
            return
        settings = get_settings()
        deleted = await asyncio.to_thread(
            storage.prune_stock_events,
            settings.stock_event_retention_days,
            settings.stock_event_max_rows,
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
        self, plan_code: str, new_statuses: list[StockStatus]
    ) -> dict[str, Any]:
        """Compute what changed since the last poll for one plan.

        Returns a dict with `newly_available`, `now_unavailable`, and
        `currently_available` FQN lists, plus a UTC timestamp. Also
        updates `_last_stock` to the new state - callers must hold `_lock`.
        """
        old_statuses = self._last_stock.get(plan_code, {})
        new_available_fqns = {s.fqn for s in new_statuses if s.available}
        old_available_fqns = set(old_statuses.keys())

        newly_available = new_available_fqns - old_available_fqns
        now_unavailable = old_available_fqns - new_available_fqns

        self._last_stock[plan_code] = {s.fqn: s.available for s in new_statuses}

        return {
            "plan_code": plan_code,
            "newly_available": sorted(newly_available),
            "now_unavailable": sorted(now_unavailable),
            "currently_available": sorted(new_available_fqns),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def _poll_once(self) -> list[dict[str, Any]]:
        """One poll cycle. Returns diffs to broadcast to SSE clients."""
        changes: list[dict[str, Any]] = []

        # Snapshot the distinct plan_codes we need to poll this cycle.
        # Bail out before building the OVH service when there's nothing to
        # poll — service construction does network I/O (endpoint/time fetch)
        # so we avoid it entirely when idle.
        async with self._lock:
            plan_codes = sorted(
                {a.plan_code for a in self._alerts.values() if a.enabled}
            )
        if not plan_codes:
            return changes

        service = get_active_ovh_service()
        if not service.is_configured():
            return changes

        return await self._poll_account(service, plan_codes)

    async def _fetch_availability_map(
        self, service, plan_codes: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch orderable configs for each plan, keyed by plan_code.

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
        if len(plan_codes) <= 1:
            self._last_cycle_batched = False
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
            return out

        self._last_cycle_batched = True
        try:
            entries = await asyncio.to_thread(service.get_stock, None)
        except OVHServiceError:
            logger.debug("batch availability fetch failed", exc_info=True)
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            if orderable_entry(entry):
                grouped.setdefault(entry.get("planCode", ""), []).append(
                    {"fqn": entry.get("fqn", "")}
                )
        return {pc: grouped.get(pc, []) for pc in plan_codes}

    async def _poll_account(
        self, service, plan_codes: list[str]
    ) -> list[dict[str, Any]]:
        """Poll one account's watched plans and process diffs/alerts/snipers."""
        changes: list[dict[str, Any]] = []
        storage = self._storage_get()
        avail_map = await self._fetch_availability_map(service, plan_codes)

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
                # (alert, fqns, notify): notify=False on the priming cycle —
                # armed snipers still fire, but no notification is sent.
                triggered_alerts: list[tuple[StockAlert, list[str], bool]] = []
                async with self._lock:
                    first_cycle = plan_code not in self._primed
                    diff = self.get_stock_diff(plan_code, new_statuses)
                    self._stock_cache[plan_code] = new_statuses
                    self._primed.add(plan_code)
                    if first_cycle:
                        # Prime silently: record the baseline without SSE
                        # broadcasts, notifications, or stock events — an
                        # empty baseline would otherwise report everything
                        # already in stock as "newly available" on every
                        # restart/account switch. Armed snipers DO still
                        # fire below on already-available stock: a sniper's
                        # job is to order the moment stock is orderable.
                        if diff["currently_available"]:
                            logger.info(
                                "primed stock baseline for %s (%d configs "
                                "available); notifications suppressed",
                                plan_code, len(diff["currently_available"]),
                            )
                        for alert in self._alerts.values():
                            if alert.plan_code == plan_code and alert.enabled:
                                matched = [
                                    fqn
                                    for fqn in diff["currently_available"]
                                    if self._matches_pattern(fqn, alert.fqn_pattern)
                                ]
                                if matched:
                                    triggered_alerts.append((alert, matched, False))
                    else:
                        # Broadcast on any change (restock or sell-out) so SSE
                        # clients can keep their stock indicators accurate -
                        # not just when something newly became available.
                        if diff["newly_available"] or diff["now_unavailable"]:
                            changes.append(diff)
                            logger.info(
                                "stock change %s: +%d available, -%d unavailable",
                                plan_code,
                                len(diff["newly_available"]),
                                len(diff["now_unavailable"]),
                            )
                        if diff["newly_available"]:
                            # Find every alert that matches at least one new FQN.
                            for alert in self._alerts.values():
                                if alert.plan_code == plan_code and alert.enabled:
                                    matched = [
                                        fqn
                                        for fqn in diff["newly_available"]
                                        if self._matches_pattern(fqn, alert.fqn_pattern)
                                    ]
                                    if matched:
                                        alert.notified_at = now
                                        triggered_alerts.append((alert, matched, True))

                        # Collect stock events for the historical-patterns view.
                        # Persisted below, after the lock is released — SQLite
                        # writes are blocking disk I/O and must not stall alert
                        # mutations (or the event loop) from inside the lock.
                        for fqn in diff["newly_available"]:
                            pending_events.append(
                                (plan_code, fqn, "available", now, service.account_id)
                            )
                        for fqn in diff["now_unavailable"]:
                            pending_events.append(
                                (plan_code, fqn, "unavailable", now, service.account_id)
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
                    for alert_obj, _, notify in triggered_alerts:
                        if not notify:
                            continue
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
                if triggered_alerts:
                    from app.services.notifier import notify_stock_alert
                    sniper = get_sniper_service()
                    for alert_obj, matched_fqns, notify in triggered_alerts:
                        if notify:
                            price = None
                            if storage:
                                price_ucents = await asyncio.to_thread(
                                    storage.latest_price,
                                    plan_code, service.account_id,
                                )
                                if price_ucents is not None:
                                    price = price_ucents / 100_000_000
                            try:
                                await notify_stock_alert(
                                    plan_code, matched_fqns, price,
                                    currency_code=service.default_currency_code(),
                                )
                            except Exception:
                                logger.warning("notifier failed for %s", plan_code, exc_info=True)
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

        return changes

    async def _sweep_snipers(self) -> None:
        """Fire armed snipers whose account is NOT the currently active one.

        `_poll_once` only watches the active account's alerts, so a sniper armed
        under account A stops being polled the moment the user switches to
        account B — it would sit "armed" forever without firing. This sweep
        keeps every armed sniper live regardless of the active account: it polls
        each armed alert's plan under its OWN credentials and fires on a match.

        Active-account snipers are already handled (edge-triggered, with SSE and
        notifications) by `_poll_once`, so they're skipped here to avoid double
        work. This path is level-triggered: it fires whenever a matching config
        is currently available, which is the desired safety-net behaviour for a
        sniper. `SniperService.maybe_fire`'s per-arm `fqns_seen` set still
        prevents duplicate orders across cycles.
        """
        sniper = get_sniper_service()
        watches = sniper.armed_watches()
        if not watches:
            return
        storage = self._storage_get()
        active_id = storage.get_active_account_id() if storage else None
        pending = [
            (aid, plan_code, pattern, account_id)
            for (aid, plan_code, pattern, account_id) in watches
            if plan_code and account_id and account_id != active_id
        ]
        if not pending:
            return
        for alert_id, plan_code, pattern, account_id in pending:
            service = get_ovh_service(account_id)
            if not service.is_configured():
                continue
            try:
                avail_configs = await asyncio.to_thread(
                    service.get_availability, plan_code
                )
            except OVHServiceError:
                logger.debug(
                    "sniper sweep availability fetch failed for %s", plan_code,
                    exc_info=True,
                )
                continue
            matched = [
                c.get("fqn", "")
                for c in avail_configs
                if self._matches_pattern(c.get("fqn", ""), pattern or "*")
            ]
            if not matched:
                continue
            try:
                await sniper.maybe_fire(
                    alert_id, plan_code, matched, account_id=account_id
                )
            except Exception:
                logger.warning(
                    "sniper sweep fire failed for %s", alert_id, exc_info=True
                )

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
