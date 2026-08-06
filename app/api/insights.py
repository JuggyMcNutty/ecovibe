"""Historical restock data, price tracking, and order history."""
import asyncio
import json
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
async def insights_summary(
    days: int = Query(default=30, ge=1, le=365),
    watched_only: bool = Query(default=True),
) -> dict:
    """Cross-plan overview for the active account: one row per plan with
    restock count, last restock, current in-stock state, and typical
    time-in-stock. Lets the user scan all monitored plans without drilling
    into each one.

    With the region ticker on, stock_events covers hundreds of plans;
    ``watched_only`` (default) keeps the summary scoped to the plans the
    user actually has alerts on. Pass watched_only=false for everything.
    """
    storage = get_storage()
    service = get_active_ovh_service()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = storage.load_account_stock_events(since, account_id=service.account_id)
    if watched_only:
        from app.services.monitor import get_monitor_service
        watched = {a.plan_code for a in get_monitor_service().get_alerts()}
        events = [e for e in events if e["plan_code"] in watched]
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


@router.get("/region-activity")
async def region_activity(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=200, ge=1, le=1000),
    event_type: str | None = Query(default=None, pattern="^(available|unavailable)$"),
) -> dict:
    """Recent region-wide stock events (newest first) for the active
    account. Fed by the region restock ticker; empty unless it has been
    enabled (PUT /api/monitor/region-watch)."""
    storage = get_storage()
    service = get_active_ovh_service()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    events = storage.load_recent_stock_events(
        since, limit=limit, account_id=service.account_id, event_type=event_type
    )
    return {"hours": hours, "events": events}


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


# How many raw promo rows to scan before grouping. One campaign stores one
# row per plan it covers, so the row count runs far ahead of the campaign
# count; scan generously so a campaign's plan list is never half-collected.
_PROMO_ROW_SCAN = 2000


def _group_promos(rows: list[dict]) -> list[dict]:
    """Collapse per-plan promo rows into one entry per campaign.

    Grouping key is the promo's `name`, which stays constant across every
    plan in a campaign. The stored `promo_key` is deliberately not used: it
    hashes the whole payload including the per-plan discount amount, so a
    single campaign can span several keys (one real sale produced 9 keys
    across 17 plans). Falls back to the description when `name` is absent.
    """
    groups: dict[str, dict] = {}
    for row in rows:
        payload = row.get("payload") or ""
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        desc = data.get("description") or data.get("name") or payload[:160]
        group = groups.setdefault(data.get("name") or desc, {
            "name": data.get("name"),
            "description": desc,
            "plan_codes": [],
            "first_seen": row.get("first_seen"),
            "start_date": data.get("startDate"),
            "end_date": data.get("endDate"),
        })
        code = row.get("plan_code")
        if code and code not in group["plan_codes"]:
            group["plan_codes"].append(code)
        # Report when the campaign was first spotted, not its latest row.
        seen = row.get("first_seen")
        if seen and (not group["first_seen"] or seen < group["first_seen"]):
            group["first_seen"] = seen
    out = list(groups.values())
    for group in out:
        group["plan_codes"].sort()
        group["plan_count"] = len(group["plan_codes"])
    out.sort(key=lambda g: g["first_seen"] or "", reverse=True)
    return out


@router.get("/promos")
async def recent_promos(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    """Recently seen OVH promotions, grouped into campaigns.

    OVH publishes a campaign against every plan it covers, so the raw table
    holds one row per plan - 17 rows of identical text for one offer. The
    rows stay as they are (they drive per-plan dedup); only the presentation
    is grouped. `limit` caps campaigns, not rows.
    """
    storage = get_storage()
    service = get_active_ovh_service()
    rows = storage.load_recent_promos(
        limit=_PROMO_ROW_SCAN, account_id=service.account_id
    )
    return {"promos": _group_promos(rows)[:limit]}


@router.get("/catalog-changes")
async def catalog_changes(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=500),
    change_type: str | None = Query(default=None, pattern="^(added|removed)$"),
) -> dict:
    """Plans OVH added to or removed from the active account's catalog,
    newest first.

    Fed by the catalog watch, which diffs each price/promo scan's catalog
    against a stored snapshot. Empty until the second scan — the first one
    only records the baseline.
    """
    storage = get_storage()
    service = get_active_ovh_service()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    changes = storage.load_catalog_changes(
        since, limit=limit, account_id=service.account_id, change_type=change_type
    )
    return {"days": days, "changes": changes}


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
