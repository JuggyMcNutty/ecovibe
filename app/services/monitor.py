import asyncio
import logging
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any, Dict, List, Optional

from dataclasses import dataclass

from app.services.ovh_service import OVHServiceError, get_ovh_service

logger = logging.getLogger(__name__)


@dataclass
class StockAlert:
    id: str
    plan_code: str
    fqn_pattern: str
    enabled: bool = True
    notified_at: Optional[datetime] = None


@dataclass
class StockStatus:
    plan_code: str
    fqn: str
    available: bool
    last_check: datetime


class DuplicateAlertError(Exception):
    """Raised when adding an alert whose (plan_code, fqn_pattern) already exists."""


class MonitorService:
    def __init__(self) -> None:
        self._alerts: Dict[str, StockAlert] = {}
        self._stock_cache: Dict[str, List[StockStatus]] = {}
        self._last_stock: Dict[str, Dict[str, bool]] = {}
        self._poll_interval = 3
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._subscribers: List[asyncio.Queue] = []

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                changes = await self._poll_once()
                if changes:
                    for q in list(self._subscribers):
                        try:
                            q.put_nowait(changes)
                        except asyncio.QueueFull:
                            logger.warning("dropping stock update for slow subscriber")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("monitor poll cycle failed")
            await asyncio.sleep(self._poll_interval)

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    async def add_alert(self, plan_code: str, fqn_pattern: str = "*") -> StockAlert:
        async with self._lock:
            for existing in self._alerts.values():
                if existing.plan_code == plan_code and existing.fqn_pattern == fqn_pattern:
                    raise DuplicateAlertError(
                        f"Alert already exists for {plan_code}:{fqn_pattern}"
                    )
            alert_id = str(uuid.uuid4())
            alert = StockAlert(id=alert_id, plan_code=plan_code, fqn_pattern=fqn_pattern)
            self._alerts[alert_id] = alert
            return alert

    async def remove_alert(self, alert_id: str) -> bool:
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
            return True

    def get_alerts(self) -> List[StockAlert]:
        return list(self._alerts.values())

    def get_alert(self, alert_id: str) -> Optional[StockAlert]:
        return self._alerts.get(alert_id)

    async def set_alert_enabled(self, alert_id: str, enabled: bool) -> Optional[StockAlert]:
        async with self._lock:
            alert = self._alerts.get(alert_id)
            if alert is not None:
                alert.enabled = enabled
            return alert

    def set_poll_interval(self, seconds: int) -> int:
        self._poll_interval = max(1, min(10, seconds))
        return self._poll_interval

    def get_poll_interval(self) -> int:
        return self._poll_interval

    def get_stock_diff(
        self, plan_code: str, new_statuses: List[StockStatus]
    ) -> Dict[str, Any]:
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

    async def _poll_once(self) -> List[Dict[str, Any]]:
        changes: List[Dict[str, Any]] = []
        service = get_ovh_service()
        if not service.is_configured():
            return changes

        async with self._lock:
            plan_codes = sorted(
                {a.plan_code for a in self._alerts.values() if a.enabled}
            )

        for plan_code in plan_codes:
            try:
                avail_configs = await asyncio.to_thread(
                    service.get_availability, plan_code
                )
                now = datetime.now(timezone.utc)
                new_statuses = [
                    StockStatus(
                        plan_code=plan_code,
                        fqn=c.get("fqn", ""),
                        available=True,
                        last_check=now,
                    )
                    for c in avail_configs
                ]
                async with self._lock:
                    diff = self.get_stock_diff(plan_code, new_statuses)
                    self._stock_cache[plan_code] = new_statuses
                    if diff["newly_available"]:
                        changes.append(diff)
                        for alert in self._alerts.values():
                            if alert.plan_code == plan_code and alert.enabled:
                                for fqn in diff["newly_available"]:
                                    if self._matches_pattern(fqn, alert.fqn_pattern):
                                        alert.notified_at = now
            except OVHServiceError:
                logger.debug("availability fetch failed for %s", plan_code, exc_info=True)

        return changes

    async def poll_and_notify(self) -> List[Dict[str, Any]]:
        return await self._poll_once()

    @staticmethod
    def _matches_pattern(fqn: str, pattern: str) -> bool:
        if pattern == "*":
            return True
        return fnmatch(fqn.lower(), pattern.lower())

    def get_current_stock(self) -> Dict[str, List[StockStatus]]:
        return self._stock_cache


_monitor_service: Optional[MonitorService] = None


def get_monitor_service() -> MonitorService:
    global _monitor_service
    if _monitor_service is None:
        _monitor_service = MonitorService()
    return _monitor_service
