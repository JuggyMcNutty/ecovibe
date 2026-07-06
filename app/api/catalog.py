"""Catalog endpoints — browse the OVH ECO server catalog and availability.

All endpoints return 503 when OVH credentials are not configured, and the
appropriate HTTP status (via `raise_ovh_http_error`) on upstream failures.
OVH SDK calls run in a thread via `asyncio.to_thread` to avoid blocking
the event loop.
"""
import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.errors import raise_ovh_http_error
from app.services.ovh_service import OVHServiceError, get_ovh_service

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("")
async def get_catalog(
    country: str = Query(default="IE", description="OVH subsidiary country code"),
    force_refresh: bool = Query(default=False, description="Force cache refresh"),
) -> dict[str, Any]:
    """Return the full ECO catalog for a subsidiary (large payload)."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        return await asyncio.to_thread(
            service.fetch_catalog, subsidiary=country, force=force_refresh
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
    country: str = Query(default="IE", min_length=1, max_length=4),
) -> list[dict[str, Any]]:
    """Return just the `plans` array from the catalog (lighter than /catalog)."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        catalog = await asyncio.to_thread(service.fetch_catalog, subsidiary=country)
        return catalog.get("plans", [])
    except OVHServiceError as e:
        raise_ovh_http_error(e)
