"""Historical restock data, price tracking, and order history."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from app.api.errors import raise_ovh_http_error
from app.services.ovh_service import OVHServiceError, get_active_ovh_service
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/insights", tags=["insights"])


@router.get("/history/{plan_code}")
async def stock_history(
    plan_code: str,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """Return historical stock events for a plan over the last N days."""
    storage = get_storage()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = storage.load_stock_events(plan_code, since=since, limit=1000)
    return {"plan_code": plan_code, "days": days, "events": events}


@router.get("/patterns/{plan_code}")
async def restock_patterns(
    plan_code: str,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """Aggregate restock counts by hour-of-day to surface the best times to monitor."""
    storage = get_storage()
    counts = storage.stock_event_counts_by_hour(plan_code, days=days)
    return {"plan_code": plan_code, "days": days, "hourly_counts": counts}


@router.get("/price/{plan_code}")
async def price_history(
    plan_code: str,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    """Return price history for a plan."""
    storage = get_storage()
    history = storage.load_price_history(plan_code, limit=limit)
    return {"plan_code": plan_code, "history": history}


@router.post("/price/{plan_code}/refresh")
async def refresh_price(
    plan_code: str,
    country: str | None = Query(default=None, min_length=1, max_length=4),
) -> dict:
    """Fetch the current price for a plan from the catalog and log it."""
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        price_ucents = await asyncio.to_thread(service.get_plan_price, plan_code, country)
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    if price_ucents is None:
        raise HTTPException(status_code=404, detail="Plan not found in catalog")
    storage = get_storage()
    storage.log_price(plan_code, price_ucents, datetime.now(timezone.utc))
    return {"plan_code": plan_code, "price_in_ucents": price_ucents, "price_eur": price_ucents / 1_000_000}


@router.get("/orders")
async def list_orders(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    """Return recently placed orders."""
    storage = get_storage()
    return {"orders": storage.load_orders(limit=limit)}


@router.get("/orders/{order_id}")
async def get_order_status(order_id: int) -> dict:
    """Fetch the current status of an order from OVH."""
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        status = await asyncio.to_thread(
            service.get, f"/me/order/{order_id}/status"
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    storage = get_storage()
    storage.update_order_status(order_id, str(status))
    return {"order_id": order_id, "status": status}
