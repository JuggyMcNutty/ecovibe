"""SSE stock stream, availability checks, and poll-interval control."""
import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.errors import raise_ovh_http_error
from app.models.schemas import PollIntervalRequest, RegionWatchRequest
from app.services.monitor import get_monitor_service
from app.services.ovh_service import OVHServiceError, get_active_ovh_service
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor", tags=["monitor"])


@router.get("/stream")
async def stream_stock_updates() -> StreamingResponse:
    """Server-Sent Events stream of stock changes.

    The browser opens an `EventSource` to this endpoint. Each connected
    client gets its own bounded queue; the single background poller pushes
    diffs to every queue. The generator runs forever - disconnection
    cancels it and the `finally` block deregisters the queue.
    """
    monitor = get_monitor_service()
    queue = await monitor.subscribe()

    async def event_generator():
        try:
            while True:
                try:
                    # Wait for the next queue item, or send a keep-alive.
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # SSE comment - keeps the connection alive without
                    # producing a client-visible event.
                    yield ": ping\n\n"
                    continue
                if isinstance(item, dict) and item.get("type"):
                    # Pre-typed event (e.g. region_restock) — pass through.
                    data = item
                else:
                    # A list of plan diffs — the classic stock_update shape.
                    data = {
                        "type": "stock_update",
                        "changes": item,
                        "alerts_triggered": len(item),
                    }
                yield f"data: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            await monitor.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Critical for nginx - disables proxy buffering so SSE events
            # reach the client immediately instead of being batched.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/availability")
async def get_availability(plans: str = Query(default="")) -> dict[str, Any]:
    """One-shot stock check for a comma-separated list of plan codes."""
    if not plans:
        return {"stocks": {}}
    plan_codes = [p.strip() for p in plans.split(",") if p.strip()]
    svc = get_active_ovh_service()
    if not svc.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        stocks: dict[str, list] = {}
        for plan_code in plan_codes:
            avail = await asyncio.to_thread(svc.get_availability, plan_code)
            stocks[plan_code] = [
                {"fqn": a.get("fqn", ""), "available": True} for a in avail
            ]
        return {"stocks": stocks}
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """The whole monitoring picture in one call.

    Three distinct things, deliberately reported separately because they
    are what the UI kept conflating:

    - ``running``      — the global background poller (Settings → App).
    - ``accounts[]``   — per-account: is it monitored, is its ticker on,
                         how many alerts it has, and whether it is actually
                         being polled this cycle.
    - ``alerts_count`` / ``monitored_plans`` — the ACTIVE account only,
                         which is what the monitor tab renders.
    """
    monitor = get_monitor_service()
    storage = get_storage()
    active_id = storage.get_active_account_id()
    scoped = monitor.get_alerts_for_account(active_id)
    all_alerts = monitor.get_alerts()

    accounts = []
    for acct in storage.list_accounts():
        aid = acct["id"]
        enabled_alerts = [
            a for a in all_alerts if a.account_id == aid and a.enabled
        ]
        monitoring = monitor.is_monitoring_enabled(aid)
        ticker = monitor.is_region_enabled(aid)
        accounts.append({
            "id": aid,
            "label": acct["label"],
            "endpoint": acct["endpoint"],
            "monitoring_enabled": monitoring,
            "region_ticker_enabled": ticker,
            "alerts_count": len(
                [a for a in all_alerts if a.account_id == aid]
            ),
            # What the poller will actually do with it next cycle.
            "polled": bool(monitoring and (enabled_alerts or ticker)),
        })

    return {
        "running": monitor.is_running(),
        "poll_interval": monitor.get_poll_interval(),
        "active_account_id": active_id,
        "monitoring_enabled": monitor.is_monitoring_enabled(active_id),
        "alerts_count": len(scoped),
        "monitored_plans": sorted({a.plan_code for a in scoped}),
        "total_alerts_count": len(all_alerts),
        "accounts": accounts,
        "accounts_polled": len([a for a in accounts if a["polled"]]),
    }


@router.put("/poll-interval")
async def set_poll_interval(request: PollIntervalRequest) -> dict[str, Any]:
    """Set the poll interval (1-60 seconds). Persists across restarts."""
    monitor = get_monitor_service()
    monitor.set_poll_interval(request.poll_interval)
    return {"poll_interval": monitor.get_poll_interval()}


@router.post("/poll-interval")
async def set_poll_interval_post(request: PollIntervalRequest) -> dict[str, Any]:
    """POST alias for PUT /poll-interval (some clients prefer POST)."""
    monitor = get_monitor_service()
    monitor.set_poll_interval(request.poll_interval)
    return {"poll_interval": monitor.get_poll_interval()}


@router.get("/region-watch")
async def get_region_watch() -> dict[str, Any]:
    """Whether the region-wide restock ticker is on for the ACTIVE account.

    Convenience alias for the active account — the flag itself lives on the
    account (``PUT /api/accounts/{id}/monitoring``), alongside the
    monitoring master switch.
    """
    return {"enabled": get_monitor_service().is_region_enabled()}


@router.put("/region-watch")
async def set_region_watch(request: RegionWatchRequest) -> dict[str, Any]:
    """Enable/disable the ACTIVE account's region restock ticker.

    Active-account alias for ``PUT /api/accounts/{id}/monitoring``; both
    land on the same setter, so the DB and the poller's cache stay in step.
    While enabled, every poll cycle fetches that account's whole region, so
    the poll interval is clamped to 3s (see BATCH_MIN_POLL_INTERVAL)."""
    monitor = get_monitor_service()
    await monitor.set_region_enabled(request.enabled)
    return {"enabled": monitor.is_region_enabled()}
