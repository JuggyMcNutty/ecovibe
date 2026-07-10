"""Catalog endpoints - browse ECO server catalog and availability."""
import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.errors import raise_ovh_http_error
from app.services.ovh_service import OVHServiceError, get_active_ovh_service

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def _resolve_subsidiary(country: str | None) -> str:
    """Resolve the OVH subsidiary to query for the active endpoint.

    Each endpoint only accepts its own subsidiaries (see
    ``OVHService.valid_subsidiaries``); a foreign one is rejected with HTTP 400
    "invalid ovhSubsidiary". When ``country`` is missing or not valid for the
    active endpoint, fall back to the endpoint's default subsidiary.
    """
    service = get_active_ovh_service()
    valid = service.valid_subsidiaries()
    if country and country in valid:
        return country
    return service._default_subsidiary()


def _build_product_specs(catalog: dict[str, Any]) -> dict[str, Any]:
    """Extract CPU and hardware specs from the catalog's top-level
    ``products`` array, keyed by product name.

    Each plan has a ``product`` field (e.g. ``"25sysle01"``) that maps
    to a product entry. The product's ``blobs.technical.server`` section
    contains CPU, chassis, and service details that are NOT available on
    the plan itself.
    """
    specs: dict[str, Any] = {}
    for product in catalog.get("products", []):
        name = product.get("name", "")
        if not name:
            continue
        tech = (product.get("blobs") or {}).get("technical", {})
        server = tech.get("server", {})
        cpu = server.get("cpu", {})
        frame = server.get("frame", {})
        services = server.get("services", {})
        specs[name] = {
            "description": product.get("description", ""),
            "cpu": {
                "brand": cpu.get("brand", ""),
                "model": cpu.get("model", ""),
                "cores": cpu.get("cores"),
                "threads": cpu.get("threads"),
                "frequency": cpu.get("frequency"),
                "boost": cpu.get("boost"),
                "number": cpu.get("number", 1),
                "score": cpu.get("score"),
            } if cpu else None,
            "frame": {
                "size": frame.get("size", ""),
                "model": frame.get("model", ""),
                "dualPowerSupply": frame.get("dualPowerSupply", False),
            } if frame else None,
            "range": server.get("range", ""),
            "services": {
                "sla": services.get("sla"),
                "antiddos": services.get("antiddos", ""),
            } if services else None,
        }
    return specs


@router.get("")
async def get_catalog(
    country: str | None = Query(default=None, description="OVH subsidiary country code"),
    force_refresh: bool = Query(default=False, description="Force cache refresh"),
) -> dict[str, Any]:
    """Return the full ECO catalog for a subsidiary (large payload)."""
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    subsidiary = _resolve_subsidiary(country)
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
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        return await asyncio.to_thread(service.get_availability, plan_code)
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.get("/stock")
async def get_stock(
    plan_code: str = Query(..., min_length=1, max_length=64, description="Server plan code"),
) -> list[dict[str, Any]]:
    """Return live stock levels per RAM+storage combo for a plan.

    Queries ``/dedicated/server/datacenter/availabilities`` and returns a
    list of ``{fqn, memory, storage, datacenters: [{datacenter, availability}]}``
    entries. The frontend uses this to show which configs are actually in
    stock before the user attempts an order.
    """
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        return await asyncio.to_thread(service.get_stock, plan_code)
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.get("/plans")
async def get_plans(
    country: str | None = Query(default=None),
    force_refresh: bool = Query(default=False, description="Force cache refresh"),
) -> dict[str, Any]:
    """Return plans + addon pricing from the catalog."""
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    subsidiary = _resolve_subsidiary(country)
    try:
        catalog = await asyncio.to_thread(
            service.fetch_catalog, subsidiary=subsidiary, force=force_refresh
        )
        plans = catalog.get("plans", [])
        # OVH only populates `currencyCode` on individual pricing entries for
        # some endpoints; ovh-ca leaves them null and exposes the catalog's
        # native currency solely via the top-level `locale.currencyCode`.
        # Use it as the authoritative fallback so the frontend can label and
        # FX-convert prices correctly regardless of endpoint.
        locale_cc = (catalog.get("locale") or {}).get("currencyCode", "") or ""
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
                "currencyCode": "",
            }
            found = False
            for pr in addon.get("pricings", []):
                if pr.get("mode") != "default":
                    continue
                # Carry the catalog's currency code (e.g. "EUR"/"USD"/"CAD")
                # so the frontend can convert prices for display.
                if not entry["currencyCode"] and pr.get("currencyCode"):
                    entry["currencyCode"] = pr["currencyCode"]
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
            # Endpoints like ovh-ca omit currencyCode on pricings; fall back to
            # the catalog's locale currency so display labels are correct.
            if not entry["currencyCode"]:
                entry["currencyCode"] = locale_cc
            if found:
                addon_prices[code] = entry
        return {
            "plans": plans,
            "addonPrices": addon_prices,
            "productSpecs": _build_product_specs(catalog),
            "currencyCode": locale_cc,
        }
    except OVHServiceError as e:
        raise_ovh_http_error(e)
