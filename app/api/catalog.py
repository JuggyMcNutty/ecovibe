"""Catalog endpoints - browse ECO server catalog and availability."""
import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.errors import raise_ovh_http_error
from app.services.ovh_service import OVHServiceError, get_ovh_service

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def _default_subsidiary() -> str:
    """Return the default subsidiary for the configured OVH endpoint.

    Each OVH region only accepts certain subsidiaries:
      ovh-eu → IE (also FR, DE, GB, ES, PL, ...)
      ovh-us → US
      ovh-ca → CA
    Sending the wrong subsidiary (e.g. IE to ovh-us) returns HTTP 400.
    """
    service = get_ovh_service()
    return service._default_subsidiary()


@router.get("")
async def get_catalog(
    country: str | None = Query(default=None, description="OVH subsidiary country code"),
    force_refresh: bool = Query(default=False, description="Force cache refresh"),
) -> dict[str, Any]:
    """Return the full ECO catalog for a subsidiary (large payload)."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    subsidiary = country if country else _default_subsidiary()
    try:
        return await asyncio.to_thread(
            service.fetch_catalog, subsidiary=subsidiary, force=force_refresh
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.get("/availability")
async def get_availability(
    plan_code: str = Query(..., min_length=1, max_length=64, description="Server plan code (e.g., 24sk10)"),
) -> list[dict[str, Any]]:
    """Return the currently orderable FQN configurations for a plan."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        return await asyncio.to_thread(service.get_availability, plan_code)
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.get("/plans")
async def get_plans(
    country: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return plans + addon pricing from the catalog."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    subsidiary = country if country else _default_subsidiary()
    try:
        catalog = await asyncio.to_thread(service.fetch_catalog, subsidiary=subsidiary)
        plans = catalog.get("plans", [])
        # Build addon price lookup from the catalog's top-level addons array.
        # Each addon may have both a monthly price (interval=1) and a one-time
        # setup/installation fee (interval=0, intervalUnit='none'). We surface
        # both so the frontend can show the true cost of a purchase.
        addon_prices = {}
        for addon in catalog.get("addons", []):
            code = addon.get("planCode", "")
            if not code:
                continue
            entry = {
                "price": 0,
                "formattedPrice": "",
                "setup_price": 0,
                "setup_formattedPrice": "",
                "invoiceName": addon.get("invoiceName", ""),
            }
            found = False
            for pr in addon.get("pricings", []):
                if pr.get("mode") != "default":
                    continue
                # Monthly recurring price (interval=1, intervalUnit='month')
                if (
                    pr.get("interval") == 1
                    and pr.get("intervalUnit") == "month"
                    and isinstance(pr.get("price"), int)
                ):
                    entry["price"] = pr.get("price", 0)
                    entry["formattedPrice"] = pr.get("formattedPrice", "")
                    found = True
                # One-time setup/installation fee (interval=0, intervalUnit='none')
                elif (
                    pr.get("interval") == 0
                    and pr.get("intervalUnit") == "none"
                    and isinstance(pr.get("price"), int)
                ):
                    entry["setup_price"] = pr.get("price", 0)
                    entry["setup_formattedPrice"] = pr.get("formattedPrice", "")
                    found = True
            if found:
                addon_prices[code] = entry
        return {"plans": plans, "addonPrices": addon_prices}
        return {"plans": plans, "addonPrices": addon_prices}
    except OVHServiceError as e:
        raise_ovh_http_error(e)
