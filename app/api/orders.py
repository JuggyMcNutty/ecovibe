"""Order management endpoints — list, detail, and actions on OVH orders.

Distinct from /api/insights/orders which only tracks locally-placed orders.
These endpoints merge local + live OVH order data and support management
actions like waiving the retraction period.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from app.api.errors import raise_ovh_http_error
from app.services.ovh_service import OVHServiceError, get_active_ovh_service
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/orders", tags=["orders"])


def _extract_price(order: dict) -> tuple[int | None, str | None]:
    """Extract price (microcents) and currency code from an order object."""
    price = order.get("priceWithTax") or {}
    raw = price.get("priceInUcents")
    cc = price.get("currencyCode")
    if isinstance(raw, int) and cc:
        return raw, cc
    return None, None


def _extract_server_name(order: dict) -> str | None:
    """Try to derive a human-readable server name from the order object."""
    for key in ("domain", "description"):
        val = order.get(key)
        if val:
            return val
    md = order.get("metadatas") or []
    if md:
        return str(md[0])
    return None


def _name_from_details(details: list[dict]) -> str | None:
    """Derive a server name from an order's line items.

    OVH dedicated-server orders carry no name on the top-level order object;
    the human-readable name lives on the line-item details (``description``,
    e.g. "Dedicated Server KS-A | ...", or ``domain``). Prefer a description,
    then a domain, skipping empty and placeholder ('*') values.
    """
    for key in ("description", "domain"):
        for d in details:
            val = d.get(key)
            if val and val != "*":
                return val
    return None


async def _order_name_from_details(service, order_id: int) -> str | None:
    """Fetch up to the first few line items of an order and extract a name.

    Used when the order object itself yields no name. Capped so an order with
    many line items doesn't explode the per-order request count."""
    try:
        detail_ids = await asyncio.to_thread(service.get, f"/me/order/{order_id}/details")
    except OVHServiceError:
        return None
    picked: list[dict] = []
    for did in (detail_ids or [])[:3]:
        try:
            picked.append(await asyncio.to_thread(service.get, f"/me/order/{order_id}/details/{did}"))
        except OVHServiceError:
            continue
    return _name_from_details(picked)


@router.get("")
async def list_orders(
    limit: int = Query(default=30, ge=1, le=100),
    days: int = Query(default=90, ge=1, le=365),
) -> dict:
    """Return a merged list of local + live OVH orders.

    Fetches order IDs from OVH (filtered to the last ``days`` days), enriches
    each with the full order object (price, dates, pdfUrl), merges with local
    orders (which carry the plan_code context), and persists the enriched
    data back to the local DB. An overall timeout prevents the endpoint from
    hanging if OVH is slow; partial results are returned on timeout.
    """
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")

    storage = get_storage()
    account_id = service.account_id

    # Fetch local orders first (fast, no network).
    local_orders = storage.load_orders(limit=200, account_id=account_id)
    local_by_id: dict[int, dict] = {}
    for o in local_orders:
        if o.get("order_id"):
            local_by_id[o["order_id"]] = o

    # Fetch all order IDs from OVH, date-filtered.
    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        ovh_ids = await asyncio.to_thread(service.list_orders, date_from, None)
    except OVHServiceError as e:
        if e.status_code == 404:
            ovh_ids = []
        else:
            raise_ovh_http_error(e)

    # Sort newest first (OVH returns them in ascending order).
    ovh_ids = sorted(ovh_ids, reverse=True)[:limit]

    # Fetch the full order object + status for each, enriching local DB.
    # Wrap in a timeout so the endpoint doesn't hang if OVH is slow; return
    # partial results (whatever was enriched before the timeout) rather than
    # an error.
    enriched: list[dict] = []
    timed_out = False
    try:
        async with asyncio.timeout(30):
            for oid in ovh_ids:
                try:
                    order_obj = await asyncio.to_thread(service.get_order, oid)
                    status = await asyncio.to_thread(service.get_order_status, oid)
                    price_ucents, currency_code = _extract_price(order_obj)
                    server_name = _extract_server_name(order_obj)
                    if not server_name:
                        # The name lives on the line items for OVH server orders.
                        # Reuse a previously-persisted name to avoid re-fetching
                        # details on every list load (short-circuits the await).
                        server_name = (
                            local_by_id.get(oid, {}).get("server_name")
                            or await _order_name_from_details(service, oid)
                        )
                    retraction_date = order_obj.get("retractionDate")
                    expiration_date = order_obj.get("expirationDate")
                    pdf_url = order_obj.get("pdfUrl")
                    url = order_obj.get("url")

                    try:
                        storage.upsert_order_enriched(
                            oid,
                            status=str(status),
                            price_with_tax=price_ucents,
                            currency_code=currency_code,
                            pdf_url=pdf_url,
                            retraction_date=retraction_date,
                            expiration_date=expiration_date,
                            server_name=server_name,
                            account_id=account_id,
                        )
                    except Exception:
                        logger.warning("Failed to persist enriched order %s", oid, exc_info=True)
                    local = local_by_id.get(oid, {})
                    enriched.append({
                        "order_id": oid,
                        "date": order_obj.get("date"),
                        "status": str(status),
                        "price_with_tax": price_ucents,
                        "currency_code": currency_code,
                        "plan_code": local.get("plan_code", ""),
                        "server_name": server_name or local.get("plan_code", ""),
                        "url": url,
                        "pdf_url": pdf_url,
                        "retraction_date": retraction_date,
                        "expiration_date": expiration_date,
                        "placed_at": local.get("placed_at"),
                    })
                except OVHServiceError as e:
                    logger.info("Failed to enrich order %s: %s", oid, e)
                    local = local_by_id.get(oid, {})
                    enriched.append({
                        "order_id": oid,
                        "date": None,
                        "status": "unknown",
                        "price_with_tax": local.get("price_with_tax"),
                        "currency_code": local.get("currency_code"),
                        "plan_code": local.get("plan_code", ""),
                        "server_name": local.get("server_name") or local.get("plan_code", f"Order #{oid}"),
                        "url": local.get("url"),
                        "pdf_url": local.get("pdf_url"),
                        "retraction_date": local.get("retraction_date"),
                        "expiration_date": local.get("expiration_date"),
                        "placed_at": local.get("placed_at"),
                    })
    except TimeoutError:
        timed_out = True
        logger.warning("Order enrichment timed out after 30s (%d/%d done)", len(enriched), len(ovh_ids))

    # Also include local orders not returned by OVH (e.g. very recent orders
    # that haven't propagated yet, or orders past the date window).
    ovh_id_set = set(ovh_ids)
    for o in local_orders:
        oid = o.get("order_id")
        if oid and oid not in ovh_id_set:
            enriched.append({
                "order_id": oid,
                "date": o.get("placed_at"),
                "status": o.get("status"),
                "price_with_tax": o.get("price_with_tax"),
                "currency_code": o.get("currency_code"),
                "plan_code": o.get("plan_code", ""),
                "server_name": o.get("server_name") or o.get("plan_code", ""),
                "url": o.get("url"),
                "pdf_url": o.get("pdf_url"),
                "retraction_date": o.get("retraction_date"),
                "expiration_date": o.get("expiration_date"),
                "placed_at": o.get("placed_at"),
            })

    # Sort by date/order_id descending.
    enriched.sort(key=lambda x: (x.get("date") or x.get("placed_at") or "", x.get("order_id") or 0), reverse=True)

    return {"orders": enriched[:limit], "timed_out": timed_out}


@router.get("/{order_id}")
async def get_order_detail(order_id: int) -> dict:
    """Return full order detail: order object + line items + follow-up timeline."""
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        order = await asyncio.to_thread(service.get_order, order_id)
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    try:
        details = await asyncio.to_thread(service.get_order_details, order_id)
    except OVHServiceError:
        details = []
    try:
        followup = await asyncio.to_thread(service.get_order_followup, order_id)
    except OVHServiceError:
        followup = []
    try:
        status = await asyncio.to_thread(service.get_order_status, order_id)
    except OVHServiceError:
        status = "unknown"

    return {"order": order, "status": status, "details": details, "followup": followup}


@router.post("/{order_id}/refresh")
async def refresh_order(order_id: int) -> dict:
    """Re-fetch order status + enriched fields from OVH and persist."""
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        status = await asyncio.to_thread(service.get_order_status, order_id)
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    try:
        order_obj = await asyncio.to_thread(service.get_order, order_id)
        price_ucents, currency_code = _extract_price(order_obj)
        server_name = _extract_server_name(order_obj)
        if not server_name:
            server_name = await _order_name_from_details(service, order_id)
        storage = get_storage()
        storage.upsert_order_enriched(
            order_id,
            status=str(status),
            price_with_tax=price_ucents,
            currency_code=currency_code,
            pdf_url=order_obj.get("pdfUrl"),
            retraction_date=order_obj.get("retractionDate"),
            expiration_date=order_obj.get("expirationDate"),
            server_name=server_name,
            account_id=service.account_id,
        )
    except OVHServiceError:
        storage = get_storage()
        storage.update_order_status(order_id, str(status))
    return {"order_id": order_id, "status": status}


@router.post("/{order_id}/waive-retraction")
async def waive_retraction(order_id: int) -> dict:
    """Waive the legal retraction period to speed up delivery."""
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        await asyncio.to_thread(service.waive_order_retraction, order_id)
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    return {"order_id": order_id, "status": "retraction_waived"}


@router.post("/{order_id}/cancel")
async def cancel_order(order_id: int) -> dict:
    """Cancel an order by exercising the right of retraction (withdrawal).

    Only available during the retraction period; OVH rejects the call
    once the period expires or the order is delivered.
    """
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        await asyncio.to_thread(service.cancel_order, order_id, "other")
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    try:
        status = await asyncio.to_thread(service.get_order_status, order_id)
    except OVHServiceError:
        status = "cancelling"
    storage = get_storage()
    storage.update_order_status(order_id, str(status))
    return {"order_id": order_id, "status": status}
