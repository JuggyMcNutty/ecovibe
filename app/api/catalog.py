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
) -> list[dict[str, Any]]:
    """Return just the `plans` array from the catalog (lighter than /catalog)."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    subsidiary = country if country else _default_subsidiary()
    try:
        catalog = await asyncio.to_thread(service.fetch_catalog, subsidiary=subsidiary)
        return catalog.get("plans", [])
    except OVHServiceError as e:
        raise_ovh_http_error(e)
