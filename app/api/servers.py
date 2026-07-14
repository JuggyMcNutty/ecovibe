"""Owned dedicated servers (read-only): list + detail for the Servers tab."""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.errors import raise_ovh_http_error
from app.services.ovh_service import OVHServiceError, get_active_ovh_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/servers", tags=["servers"])

# Detail-fetch budget for the list view: each server costs two OVH calls
# (detail + serviceInfos). Past the budget, servers degrade to bare names
# so an account with many servers can't hang the request (mirrors the
# name_budget pattern in orders.py).
_DETAIL_BUDGET = 12


def _summary_from_detail(
    name: str, detail: dict[str, Any], info: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "service_name": name,
        "display_name": detail.get("iam", {}).get("displayName") or detail.get("reverse") or name,
        "datacenter": detail.get("datacenter"),
        "os": detail.get("os"),
        "state": detail.get("state"),
        "commercial_range": detail.get("commercialRange"),
        "ip": detail.get("ip"),
        "expiration": (info or {}).get("expiration"),
        "renewal_type": (info or {}).get("renewalType"),
    }


@router.get("")
async def list_servers() -> dict[str, Any]:
    """List the account's dedicated servers, enriched within a call budget."""
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        names = await asyncio.to_thread(service.list_dedicated_servers)
    except OVHServiceError as e:
        raise_ovh_http_error(e)

    servers: list[dict[str, Any]] = []
    budget = _DETAIL_BUDGET
    for name in names or []:
        if budget <= 0:
            servers.append({"service_name": name})
            continue
        budget -= 1
        try:
            detail = await asyncio.to_thread(service.get_dedicated_server, name)
        except OVHServiceError:
            logger.debug("server detail fetch failed for %s", name, exc_info=True)
            servers.append({"service_name": name})
            continue
        info = None
        try:
            info = await asyncio.to_thread(service.get_server_service_info, name)
        except OVHServiceError:
            logger.debug("serviceInfos fetch failed for %s", name, exc_info=True)
        servers.append(_summary_from_detail(name, detail, info))
    return {"servers": servers}


@router.get("/{service_name}")
async def get_server(service_name: str) -> dict[str, Any]:
    """Full detail for one server: raw OVH detail merged with serviceInfos."""
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        detail = await asyncio.to_thread(service.get_dedicated_server, service_name)
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    info: dict[str, Any] = {}
    try:
        info = await asyncio.to_thread(service.get_server_service_info, service_name)
    except OVHServiceError:
        logger.debug("serviceInfos fetch failed for %s", service_name, exc_info=True)
    return {
        "summary": _summary_from_detail(service_name, detail, info),
        "detail": detail,
        "service_info": info,
    }
