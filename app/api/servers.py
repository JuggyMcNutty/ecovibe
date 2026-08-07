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
from pydantic import BaseModel

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


# ---------------------------------------------------------------------------
# Power, boot and server flags
# ---------------------------------------------------------------------------

# The writable half of dedicated.server.Dedicated. Everything else OVH returns
# on the server object (state, powerState, rack, supportLevel, os, ip, ...) is
# read-only; accepting them would silently no-op or 400 at OVH.
WRITABLE_PROPERTIES = (
    "monitoring",
    "noIntervention",
    "rescueMail",
    "rescueSshKey",
    "bootScript",
    "rootDevice",
    "efiBootloaderPath",
    "bootId",
)

# dedicated.TaskStatusEnum values that mean the task will not move again.
TERMINAL_TASK_STATUSES = frozenset({"done", "cancelled", "customerError", "ovhError"})


class ServerProperties(BaseModel):
    """Body for PUT /api/servers/{name}/properties — all fields optional.

    Only the writable properties are modelled, so a caller cannot smuggle a
    read-only field through to OVH.
    """
    monitoring: bool | None = None
    no_intervention: bool | None = None
    rescue_mail: str | None = None
    rescue_ssh_key: str | None = None
    boot_script: str | None = None
    root_device: str | None = None
    efi_bootloader_path: str | None = None

    def to_ovh(self) -> dict[str, Any]:
        """Map snake_case API fields onto OVH's camelCase, omitting unset ones."""
        mapping = {
            "monitoring": self.monitoring,
            "noIntervention": self.no_intervention,
            "rescueMail": self.rescue_mail,
            "rescueSshKey": self.rescue_ssh_key,
            "bootScript": self.boot_script,
            "rootDevice": self.root_device,
            "efiBootloaderPath": self.efi_bootloader_path,
        }
        return {k: v for k, v in mapping.items() if v is not None}


class BootRequest(BaseModel):
    """Body for PUT /api/servers/{name}/boot."""
    boot_id: int
    # Boot changes only take effect on the next boot, so the rescue-mode flow
    # is "set bootId, then reboot". Opt in explicitly — a reboot is downtime.
    reboot: bool = False


@router.get("/{service_name}/boot")
async def list_boot_options(service_name: str) -> dict[str, Any]:
    """The netboots this server can use, resolved to their details.

    OVH returns bare ids from ``/boot``; each needs a second call to learn
    whether it is harddisk, rescue or power-off. The list is short (three
    entries on the KS-C checked), so resolving them all is cheap.
    """
    service = _configured_service()
    try:
        ids = await asyncio.to_thread(service.server_get, service_name, "/boot")
    except OVHServiceError as e:
        raise_ovh_http_error(e)

    options: list[dict[str, Any]] = []
    for boot_id in ids or []:
        try:
            detail = await asyncio.to_thread(
                service.server_get, service_name, f"/boot/{boot_id}"
            )
        except OVHServiceError:
            logger.debug("boot detail failed for %s", boot_id, exc_info=True)
            options.append({"boot_id": boot_id})
            continue
        options.append({
            "boot_id": boot_id,
            "boot_type": detail.get("bootType"),
            "kernel": detail.get("kernel"),
            "description": detail.get("description"),
        })
    return {"options": options}


@router.put("/{service_name}/boot")
async def set_boot(service_name: str, request: BootRequest) -> dict[str, Any]:
    """Select the netboot for the next boot, optionally rebooting into it."""
    service = _configured_service()
    try:
        await asyncio.to_thread(
            service.server_put, service_name, "", bootId=request.boot_id
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    logger.info("server %s boot set to %s", service_name, request.boot_id)

    task = None
    if request.reboot:
        try:
            task = await asyncio.to_thread(service.server_post, service_name, "/reboot")
        except OVHServiceError as e:
            # The boot change DID apply — report that rather than a bare error,
            # so the caller doesn't retry it and double-apply.
            raise HTTPException(
                status_code=502,
                detail=f"Boot set to {request.boot_id}, but the reboot failed: {e}",
            ) from e
        logger.info("server %s rebooting into boot %s", service_name, request.boot_id)
    return {"boot_id": request.boot_id, "rebooted": request.reboot, "task": task}


@router.post("/{service_name}/reboot")
async def reboot_server(service_name: str) -> dict[str, Any]:
    """Hard reboot. Returns the OVH task so the caller can follow it."""
    service = _configured_service()
    try:
        task = await asyncio.to_thread(service.server_post, service_name, "/reboot")
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    logger.info("server %s hard reboot requested", service_name)
    return {"task": task}


@router.put("/{service_name}/properties")
async def set_properties(service_name: str, request: ServerProperties) -> dict[str, Any]:
    """Update the writable server properties (monitoring, rescue details, ...)."""
    service = _configured_service()
    payload = request.to_ovh()
    if not payload:
        raise HTTPException(status_code=422, detail="No properties supplied")
    try:
        await asyncio.to_thread(service.server_put, service_name, "", **payload)
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    logger.info("server %s properties updated: %s", service_name, ", ".join(payload))
    return {"updated": payload}


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskSchedule(BaseModel):
    """Body for POST /api/servers/{name}/tasks/{id}/schedule."""
    wanted_begining_date: str          # OVH's spelling, kept to match the API
    has_performed_backup: bool = False


@router.get("/{service_name}/tasks")
async def list_tasks(
    service_name: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Recent tasks, newest first, resolved to their details within a budget."""
    service = _configured_service()
    params = {"status": status} if status else {}
    try:
        ids = await asyncio.to_thread(service.server_get, service_name, "/task", **params)
    except OVHServiceError as e:
        raise_ovh_http_error(e)

    tasks: list[dict[str, Any]] = []
    for task_id in sorted(ids or [], reverse=True)[:limit]:
        try:
            tasks.append(await asyncio.to_thread(
                service.server_get, service_name, f"/task/{task_id}"
            ))
        except OVHServiceError:
            logger.debug("task detail failed for %s", task_id, exc_info=True)
            tasks.append({"taskId": task_id, "status": "unknown"})
    return {"tasks": tasks}


@router.get("/{service_name}/tasks/{task_id}")
async def get_task(service_name: str, task_id: int) -> dict[str, Any]:
    service = _configured_service()
    try:
        task = await asyncio.to_thread(
            service.server_get, service_name, f"/task/{task_id}"
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    return {
        "task": task,
        "terminal": str(task.get("status")) in TERMINAL_TASK_STATUSES,
    }


@router.post("/{service_name}/tasks/{task_id}/cancel")
async def cancel_task(service_name: str, task_id: int) -> dict[str, Any]:
    """Stop a task's progression where OVH allows it."""
    service = _configured_service()
    try:
        await asyncio.to_thread(
            service.server_post, service_name, f"/task/{task_id}/cancel"
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    logger.info("server %s task %s cancelled", service_name, task_id)
    return {"task_id": task_id, "cancelled": True}


@router.post("/{service_name}/tasks/{task_id}/schedule")
async def schedule_task(
    service_name: str, task_id: int, request: TaskSchedule,
) -> dict[str, Any]:
    """Book an intervention slot for a task that needs scheduling."""
    service = _configured_service()
    try:
        await asyncio.to_thread(
            service.server_post, service_name, f"/task/{task_id}/schedule",
            wantedBeginingDate=request.wanted_begining_date,
            hasPerformedBackup=request.has_performed_backup,
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    logger.info("server %s task %s scheduled for %s",
                service_name, task_id, request.wanted_begining_date)
    return {"task_id": task_id, "scheduled": True}


@router.get("/{service_name}/tasks/{task_id}/timeslots")
async def task_timeslots(service_name: str, task_id: int) -> dict[str, Any]:
    """Intervention slots OVH offers for a task awaiting scheduling."""
    service = _configured_service()
    try:
        slots = await asyncio.to_thread(
            service.server_get, service_name, f"/task/{task_id}/availableTimeslots"
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    return {"timeslots": slots}


# ---------------------------------------------------------------------------
# Reinstall
# ---------------------------------------------------------------------------


class ReinstallStorage(BaseModel):
    """One entry of dedicated.server.reinstall.Storage."""
    disk_group_id: int | None = None
    hardware_raid: list[dict[str, Any]] | None = None
    partitioning: dict[str, Any] | None = None

    def to_ovh(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.disk_group_id is not None:
            out["diskGroupId"] = self.disk_group_id
        if self.hardware_raid is not None:
            out["hardwareRaid"] = self.hardware_raid
        if self.partitioning is not None:
            out["partitioning"] = self.partitioning
        return out


class ReinstallCustomizations(BaseModel):
    """dedicated.server.reinstall.Customizations, snake_cased."""
    hostname: str | None = None
    ssh_key: str | None = None
    language: str | None = None
    post_installation_script: str | None = None
    post_installation_script_extension: str | None = None
    config_drive_user_data: str | None = None
    config_drive_metadata: dict[str, str] | None = None
    efi_bootloader_path: str | None = None
    enable_lacp_bonding: bool | None = None
    # Bring-your-own-image
    image_url: str | None = None
    image_type: str | None = None
    image_check_sum: str | None = None
    image_check_sum_type: str | None = None
    http_headers: dict[str, str] | None = None

    def to_ovh(self) -> dict[str, Any]:
        mapping = {
            "hostname": self.hostname,
            "sshKey": self.ssh_key,
            "language": self.language,
            "postInstallationScript": self.post_installation_script,
            "postInstallationScriptExtension": self.post_installation_script_extension,
            "configDriveUserData": self.config_drive_user_data,
            "configDriveMetadata": self.config_drive_metadata,
            "efiBootloaderPath": self.efi_bootloader_path,
            "enableLacpBonding": self.enable_lacp_bonding,
            "imageURL": self.image_url,
            "imageType": self.image_type,
            "imageCheckSum": self.image_check_sum,
            "imageCheckSumType": self.image_check_sum_type,
            "httpHeaders": self.http_headers,
        }
        return {k: v for k, v in mapping.items() if v is not None}


class ReinstallRequest(BaseModel):
    """Body for POST /api/servers/{name}/reinstall."""
    operating_system: str
    customizations: ReinstallCustomizations | None = None
    storage: list[ReinstallStorage] | None = None


@router.get("/{service_name}/install/templates")
async def install_templates(service_name: str) -> dict[str, Any]:
    """OS templates compatible with this hardware (OVH's + any personal ones)."""
    service = _configured_service()
    try:
        data = await asyncio.to_thread(
            service.server_get, service_name, "/install/compatibleTemplates"
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    # OVH returns {"ovh": [...], "personal": [...]}; flatten for the picker
    # while keeping the split so the UI can group them.
    groups = data if isinstance(data, dict) else {"ovh": data or []}
    return {"groups": groups, "all": sorted({t for v in groups.values() for t in (v or [])})}


@router.get("/{service_name}/install/partition-schemes")
async def install_partition_schemes(
    service_name: str, template: str = Query(...),
) -> dict[str, Any]:
    """Partition schemes for a template. OVH 400s without templateName."""
    service = _configured_service()
    try:
        schemes = await asyncio.to_thread(
            service.server_get, service_name,
            "/install/compatibleTemplatePartitionSchemes", templateName=template,
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    return {"template": template, "schemes": schemes}


@router.get("/{service_name}/install/raid-profile")
async def install_raid_profile(service_name: str) -> dict[str, Any]:
    """Hardware RAID profile, or ``supported: false`` where there is none.

    OVH answers 403 "Hardware RAID is not supported by this server" on
    entry-level hardware — that is an answer, not an error.
    """
    service = _configured_service()
    try:
        profile = await asyncio.to_thread(
            service.server_get, service_name, "/install/hardwareRaidProfile"
        )
    except OVHServiceError as e:
        if e.status_code in (403, 404):
            return {"supported": False, "profile": None}
        raise_ovh_http_error(e)
    return {"supported": True, "profile": profile}


@router.get("/{service_name}/install/status")
async def install_status(service_name: str) -> dict[str, Any]:
    """Installation progress.

    OVH 404s with "Server is not being installed or reinstalled at the moment"
    when idle. That is the normal state, so it maps to ``installing: false``
    rather than bubbling up as an error the UI would have to special-case.
    """
    service = _configured_service()
    try:
        status = await asyncio.to_thread(
            service.server_get, service_name, "/install/status"
        )
    except OVHServiceError as e:
        if e.status_code == 404:
            return {"installing": False, "status": None}
        raise_ovh_http_error(e)
    return {"installing": True, "status": status}


@router.post("/{service_name}/reinstall")
async def reinstall_server(
    service_name: str, request: ReinstallRequest,
) -> dict[str, Any]:
    """Install or reinstall an OS. **Wipes the server.**

    POST is never retried by ``OVHService._call`` on a 5xx, which matters here
    more than anywhere else in the app: a retried reinstall would restart a
    wipe that may already be running.
    """
    service = _configured_service()
    payload: dict[str, Any] = {"operatingSystem": request.operating_system}
    if request.customizations:
        custom = request.customizations.to_ovh()
        if custom:
            payload["customizations"] = custom
    if request.storage:
        storage = [s.to_ovh() for s in request.storage]
        storage = [s for s in storage if s]
        if storage:
            payload["storage"] = storage

    logger.info(
        "server %s reinstall requested: os=%s customizations=%s storage=%d",
        service_name, request.operating_system,
        sorted(payload.get("customizations", {})), len(payload.get("storage", [])),
    )
    try:
        task = await asyncio.to_thread(
            service.server_post, service_name, "/reinstall", **payload
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    return {"task": task, "operating_system": request.operating_system}


# ---------------------------------------------------------------------------
# IPMI / console
# ---------------------------------------------------------------------------

# dedicated.server.IpmiAccessTypeEnum. Which of these actually work varies by
# machine — /features/ipmi reports supportedFeatures, and the frontend offers
# only the supported ones (on a KS-C that is kvmipJnlp alone).
IPMI_ACCESS_TYPES = ("kvmipHtml5URL", "kvmipJnlp", "serialOverLanURL", "serialOverLanSshKey")
# dedicated.server.CacheTTLEnum — session lifetime in minutes.
IPMI_TTLS = (1, 3, 5, 10, 15)


class IpmiAccessRequest(BaseModel):
    """Body for POST /api/servers/{name}/ipmi/access."""
    type: str
    ttl: int = 15
    ip_to_allow: str | None = None
    ssh_key: str | None = None


@router.get("/{service_name}/ipmi")
async def get_ipmi(service_name: str) -> dict[str, Any]:
    """IPMI state plus which console types this machine actually supports."""
    service = _configured_service()
    try:
        data = await asyncio.to_thread(service.server_get, service_name, "/features/ipmi")
    except OVHServiceError as e:
        if e.status_code in (403, 404):
            return {"available": False, "ipmi": None, "supported_features": {}}
        raise_ovh_http_error(e)
    return {
        "available": True,
        "ipmi": data,
        "supported_features": data.get("supportedFeatures") or {},
    }


@router.post("/{service_name}/ipmi/access")
async def create_ipmi_access(
    service_name: str, request: IpmiAccessRequest,
) -> dict[str, Any]:
    """Open an IPMI console session. Returns the task that prepares it."""
    if request.type not in IPMI_ACCESS_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"type must be one of: {', '.join(IPMI_ACCESS_TYPES)}",
        )
    if request.ttl not in IPMI_TTLS:
        raise HTTPException(
            status_code=422,
            detail=f"ttl must be one of: {', '.join(str(t) for t in IPMI_TTLS)}",
        )
    service = _configured_service()
    payload: dict[str, Any] = {"type": request.type, "ttl": request.ttl}
    if request.ip_to_allow:
        payload["ipToAllow"] = request.ip_to_allow
    if request.ssh_key:
        payload["sshKey"] = request.ssh_key
    try:
        task = await asyncio.to_thread(
            service.server_post, service_name, "/features/ipmi/access", **payload
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    logger.info("server %s IPMI access requested (%s)", service_name, request.type)
    return {"task": task}


@router.get("/{service_name}/ipmi/access")
async def get_ipmi_access(
    service_name: str, type: str = Query(...),
) -> dict[str, Any]:
    """Fetch the prepared console URL/JNLP once the access task has run."""
    if type not in IPMI_ACCESS_TYPES:
        raise HTTPException(status_code=422, detail="Unknown IPMI access type")
    service = _configured_service()
    try:
        value = await asyncio.to_thread(
            service.server_get, service_name, "/features/ipmi/access", type=type,
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    return {"type": type, "access": value}


@router.post("/{service_name}/ipmi/{action}")
async def ipmi_action(service_name: str, action: str) -> dict[str, Any]:
    """Reset the IPMI interface or its sessions, or run OVH's IPMI self-test."""
    paths = {
        "reset-interface": "/features/ipmi/resetInterface",
        "reset-sessions": "/features/ipmi/resetSessions",
        "test": "/features/ipmi/test",
    }
    subpath = paths.get(action)
    if subpath is None:
        raise HTTPException(status_code=404, detail=f"Unknown IPMI action: {action}")
    service = _configured_service()
    try:
        result = await asyncio.to_thread(service.server_post, service_name, subpath)
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    logger.info("server %s IPMI %s", service_name, action)
    return {"action": action, "result": result}


# ---------------------------------------------------------------------------
# Network: OLA aggregation, virtual network interfaces, IP moves
# ---------------------------------------------------------------------------


class OlaGroupRequest(BaseModel):
    name: str
    virtual_network_interfaces: list[str]


class OlaInterfaceRequest(BaseModel):
    virtual_network_interface: str


class IpMoveRequest(BaseModel):
    ip: str


class IpBlockMergeRequest(BaseModel):
    block: str


@router.post("/{service_name}/ola/{action}")
async def ola_action(
    service_name: str, action: str, request: dict[str, Any],
) -> dict[str, Any]:
    """OLA (OVH Link Aggregation) interface grouping.

    Reconfigures the physical uplinks, so a wrong call can leave the server
    unreachable — the UI gates ``reset`` and ``ungroup`` behind a typed
    confirmation for that reason.
    """
    service = _configured_service()
    if action in ("group", "aggregation"):
        body = OlaGroupRequest(**request)
        payload = {
            "name": body.name,
            "virtualNetworkInterfaces": body.virtual_network_interfaces,
        }
        subpath = f"/ola/{action}"
    elif action in ("reset", "ungroup"):
        body = OlaInterfaceRequest(**request)
        payload = {"virtualNetworkInterface": body.virtual_network_interface}
        subpath = f"/ola/{action}"
    else:
        raise HTTPException(status_code=404, detail=f"Unknown OLA action: {action}")

    try:
        task = await asyncio.to_thread(
            service.server_post, service_name, subpath, **payload
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    logger.info("server %s OLA %s: %s", service_name, action, payload)
    return {"action": action, "task": task}


@router.post("/{service_name}/vni/{uuid}/{action}")
async def vni_action(service_name: str, uuid: str, action: str) -> dict[str, Any]:
    """Enable or disable one virtual network interface."""
    if action not in ("enable", "disable"):
        raise HTTPException(status_code=404, detail=f"Unknown VNI action: {action}")
    service = _configured_service()
    try:
        task = await asyncio.to_thread(
            service.server_post, service_name,
            f"/virtualNetworkInterface/{uuid}/{action}",
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    logger.info("server %s VNI %s %sd", service_name, uuid, action)
    return {"uuid": uuid, "action": action, "task": task}


@router.put("/{service_name}/vni/{uuid}")
async def update_vni(
    service_name: str, uuid: str, request: dict[str, Any],
) -> dict[str, Any]:
    """Alter a virtual network interface (name, mode)."""
    service = _configured_service()
    try:
        await asyncio.to_thread(
            service.server_put, service_name,
            f"/virtualNetworkInterface/{uuid}", **request,
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    return {"uuid": uuid, "updated": request}


@router.post("/{service_name}/ip-move")
async def move_ip(service_name: str, request: IpMoveRequest) -> dict[str, Any]:
    """Move a failover IP onto this server."""
    service = _configured_service()
    try:
        task = await asyncio.to_thread(
            service.server_post, service_name, "/ipMove", ip=request.ip
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    logger.info("server %s: moving IP %s here", service_name, request.ip)
    return {"ip": request.ip, "task": task}


@router.post("/{service_name}/ip-block-merge")
async def merge_ip_block(
    service_name: str, request: IpBlockMergeRequest,
) -> dict[str, Any]:
    """Merge a split IP block back and route it here.

    OVH's own wording: "You cannot undo this operation." The UI gates it behind
    a typed confirmation.
    """
    service = _configured_service()
    try:
        task = await asyncio.to_thread(
            service.server_post, service_name, "/ipBlockMerge", block=request.block
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    logger.info(
        "server %s: merging IP block %s (irreversible)", service_name, request.block
    )
    return {"block": request.block, "task": task}
