import asyncio
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from app.api.errors import raise_ovh_http_error
from app.services.ovh_service import OVHServiceError, get_ovh_service

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("")
async def get_catalog(
    country: str = Query(default="IE", description="OVH subsidiary country code"),
    force_refresh: bool = Query(default=False, description="Force cache refresh"),
) -> Dict[str, Any]:
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        return await asyncio.to_thread(service.fetch_catalog, subsidiary=country, force=force_refresh)
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.get("/availability")
async def get_availability(
    plan_code: str = Query(..., min_length=1, max_length=64, description="Server plan code (e.g., 24sk10)"),
) -> List[Dict[str, Any]]:
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
) -> List[Dict[str, Any]]:
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        catalog = await asyncio.to_thread(service.fetch_catalog, subsidiary=country)
        return catalog.get("plans", [])
    except OVHServiceError as e:
        raise_ovh_http_error(e)
