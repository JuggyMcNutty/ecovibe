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


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _summarize_events(events: list[dict]) -> dict:
    """Derive per-plan aggregates from a plan's stock events (oldest first).

    Pairs each ``available`` event for a config (fqn) with the following
    ``unavailable`` to measure how long it stayed orderable. Returns restock
    count, distinct config count, last restock time, current in-stock state,
    and closed-window durations (seconds).
    """
    open_since: dict[str, datetime] = {}
    durations: list[float] = []
    restocks = 0
    configs: set[str] = set()
    last_restock: datetime | None = None
    for e in events:
        ts = _parse_ts(e["timestamp"])
        if ts is None:
            continue
        fqn = e["fqn"]
        configs.add(fqn)
        if e["event_type"] == "available":
            restocks += 1
            if last_restock is None or ts > last_restock:
                last_restock = ts
            open_since.setdefault(fqn, ts)
        else:  # unavailable — close an open window if we have one
            started = open_since.pop(fqn, None)
            if started is not None:
                durations.append(max((ts - started).total_seconds(), 0.0))
    avg = sum(durations) / len(durations) if durations else None
    median = None
    if durations:
        s = sorted(durations)
        mid = len(s) // 2
        median = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
    return {
        "restocks": restocks,
        "configs": len(configs),
        "last_restock": last_restock.isoformat() if last_restock else None,
        "in_stock_now": len(open_since) > 0,
        "avg_window_seconds": avg,
        "median_window_seconds": median,
    }


@router.get("/summary")
async def insights_summary(days: int = Query(default=30, ge=1, le=365)) -> dict:
    """Cross-plan overview for the active account: one row per plan with
    restock count, last restock, current in-stock state, and typical
    time-in-stock. Lets the user scan all monitored plans without drilling
    into each one."""
    storage = get_storage()
    service = get_active_ovh_service()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = storage.load_account_stock_events(since, account_id=service.account_id)
    by_plan: dict[str, list[dict]] = {}
    for e in events:
        by_plan.setdefault(e["plan_code"], []).append(e)
    plans = []
    for plan_code, plan_events in by_plan.items():
        summary = _summarize_events(plan_events)
        summary["plan_code"] = plan_code
        plans.append(summary)
    # Most recently active first; plans with no restock sink to the bottom.
    plans.sort(key=lambda p: (p["last_restock"] or ""), reverse=True)
    return {"days": days, "plans": plans}


@router.get("/history/{plan_code}")
async def stock_history(
    plan_code: str,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """Return historical stock events for a plan over the last N days."""
    storage = get_storage()
    service = get_active_ovh_service()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = storage.load_stock_events(
        plan_code, since=since, limit=1000, account_id=service.account_id
    )
    return {"plan_code": plan_code, "days": days, "events": events}


@router.get("/patterns/{plan_code}")
async def restock_patterns(
    plan_code: str,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """Aggregate restock counts by hour-of-day to surface the best times to monitor."""
    storage = get_storage()
    service = get_active_ovh_service()
    counts = storage.stock_event_counts_by_hour(
        plan_code, days=days, account_id=service.account_id
    )
    return {"plan_code": plan_code, "days": days, "hourly_counts": counts}


@router.get("/price/{plan_code}")
async def price_history(
    plan_code: str,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    """Return price history for a plan."""
    storage = get_storage()
    service = get_active_ovh_service()
    history = storage.load_price_history(plan_code, limit=limit, account_id=service.account_id)
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
    storage.log_price(
        plan_code, price_ucents, datetime.now(timezone.utc),
        account_id=service.account_id,
        currency_code=service.default_currency_code(country),
    )
    return {"plan_code": plan_code, "price_in_ucents": price_ucents}


@router.get("/orders")
async def list_orders(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    """Return recently placed orders for the active account."""
    storage = get_storage()
    service = get_active_ovh_service()
    return {"orders": storage.load_orders(limit=limit, account_id=service.account_id)}


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
