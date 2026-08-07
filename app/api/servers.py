"""Owned dedicated servers: list, detail, capability discovery and control.

Every control here is capability-gated. OVH's schema advertises 98 paths but a
given machine only implements some of them — see
``app/services/server_features.py`` for why and how that is discovered.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.errors import raise_ovh_http_error
from app.services.ovh_service import OVHServiceError, get_active_ovh_service
from app.services.server_features import (
    CAPABILITY_TTL_DAYS,
    SERVER_RESOURCES,
    derive_capabilities,
    probe_server_capabilities,
)
from app.services.storage import get_storage

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


def _configured_service():
    """The active OVH service, or a 503 if no account is configured."""
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    return service


def _is_stale(probed_at: str | None) -> bool:
    if not probed_at:
        return True
    try:
        when = datetime.fromisoformat(probed_at)
    except (TypeError, ValueError):
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when > timedelta(days=CAPABILITY_TTL_DAYS)


@router.get("/{service_name}/capabilities")
async def get_capabilities(
    service_name: str, refresh: bool = Query(default=False),
) -> dict[str, Any]:
    """Which optional OVH features this server actually has.

    Probing costs one OVH call per optional resource, so the result is cached
    in SQLite and only re-probed when missing, stale
    (``CAPABILITY_TTL_DAYS``), or explicitly refreshed — hardware doesn't grow
    a KVM overnight.
    """
    service = _configured_service()
    storage = get_storage()
    account_id = service.account_id

    cached = await asyncio.to_thread(
        storage.load_server_capabilities, service_name, account_id
    )
    if not refresh and cached and not _is_stale(cached.get("probed_at")):
        return {
            "service_name": service_name,
            "capabilities": cached["capabilities"],
            "probed_at": cached["probed_at"],
            "cached": True,
        }

    try:
        caps = await asyncio.to_thread(
            probe_server_capabilities, service, service_name
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)

    # /features/ipmi already says which console types work; read it rather than
    # assuming "has IPMI" means "has a browser console".
    ipmi = None
    if caps.get("ipmi"):
        try:
            ipmi = await asyncio.to_thread(
                service.server_get, service_name, "/features/ipmi"
            )
        except OVHServiceError:
            logger.debug("ipmi detail fetch failed for %s", service_name, exc_info=True)
    caps = derive_capabilities(caps, ipmi)

    now = datetime.now(timezone.utc)
    try:
        await asyncio.to_thread(
            storage.save_server_capabilities, service_name, caps, now, account_id
        )
    except Exception:
        logger.warning("failed to cache capabilities for %s", service_name, exc_info=True)
    return {
        "service_name": service_name,
        "capabilities": caps,
        "probed_at": now.isoformat(),
        "cached": False,
    }


@router.get("/{service_name}/resource/{key}")
async def get_server_resource(
    service_name: str, key: str,
    period: str | None = Query(default=None),
    type: str | None = Query(default=None),
) -> dict[str, Any]:
    """Read one registry-defined sub-resource.

    One endpoint instead of twenty near-identical ones. The key must be in
    ``SERVER_RESOURCES`` — this is deliberately **not** a passthrough to an
    arbitrary OVH path, which would let a caller reach any endpoint the
    credentials can touch.
    """
    res = SERVER_RESOURCES.get(key)
    if res is None:
        raise HTTPException(status_code=404, detail=f"Unknown server resource: {key}")
    service = _configured_service()

    supplied = {"period": period, "type": type}
    params = {k: v for k, v in supplied.items() if k in res.params and v is not None}
    missing = [p for p in res.params if p not in params]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"{key} requires: {', '.join(missing)}",
        )
    try:
        data = await asyncio.to_thread(
            service.server_get, service_name, res.path, **params
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    return {"key": key, "label": res.label, "data": data}
