import asyncio
import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.errors import raise_ovh_http_error
from app.services.monitor import get_monitor_service
from app.services.ovh_service import OVHServiceError, get_ovh_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor", tags=["monitor"])


class PollIntervalRequest(BaseModel):
    poll_interval: int = Field(..., ge=1, le=10)


@router.get("/stream")
async def stream_stock_updates() -> StreamingResponse:
    monitor = get_monitor_service()
    queue = await monitor.subscribe()

    async def event_generator():
        try:
            while True:
                try:
                    changes = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
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
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/availability")
async def get_availability(plans: str = Query(default="")) -> Dict[str, Any]:
    if not plans:
        return {"stocks": {}}
    plan_codes = [p.strip() for p in plans.split(",") if p.strip()]
    svc = get_ovh_service()
    try:
        stocks: Dict[str, list] = {}
        for plan_code in plan_codes:
            avail = await asyncio.to_thread(svc.get_availability, plan_code)
            stocks[plan_code] = [
                {"fqn": a.get("fqn", ""), "available": True} for a in avail
            ]
        return {"stocks": stocks}
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.get("/status")
async def get_status() -> Dict[str, Any]:
    monitor = get_monitor_service()
    return {
        "poll_interval": monitor.get_poll_interval(),
        "alerts_count": len(monitor.get_alerts()),
        "monitored_plans": sorted({a.plan_code for a in monitor.get_alerts()}),
    }


@router.put("/poll-interval")
async def set_poll_interval(request: PollIntervalRequest) -> Dict[str, Any]:
    monitor = get_monitor_service()
    monitor.set_poll_interval(request.poll_interval)
    return {"poll_interval": monitor.get_poll_interval()}


@router.post("/poll-interval")
async def set_poll_interval_post(request: PollIntervalRequest) -> Dict[str, Any]:
    monitor = get_monitor_service()
    monitor.set_poll_interval(request.poll_interval)
    return {"poll_interval": monitor.get_poll_interval()}
