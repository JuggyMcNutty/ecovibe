"""SSE stock stream, availability checks, and poll-interval control."""
import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.api.errors import raise_ovh_http_error
from app.models.schemas import PollIntervalRequest
from app.services.monitor import get_monitor_service
from app.services.ovh_service import OVHServiceError, get_active_ovh_service

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
                    # Wait for the next diff batch, or send a keep-alive.
                    changes = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # SSE comment - keeps the connection alive without
                    # producing a client-visible event.
                    yield ": ping\n\n"
                    continue
                data = {
                    "type": "stock_update",
                    "changes": changes,
                    "alerts_triggered": len(changes),
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
    """Return the current poll interval, alert count, and monitored plans."""
    monitor = get_monitor_service()
    return {
        "poll_interval": monitor.get_poll_interval(),
        "alerts_count": len(monitor.get_alerts()),
        "monitored_plans": sorted({a.plan_code for a in monitor.get_alerts()}),
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
