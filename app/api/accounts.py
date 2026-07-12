"""Multi-account API: CRUD for OVH credentials + active-account switching.

Each account stores a label, region (endpoint), and the three OVH API
keys. One account is "active" at a time — all read/monitor/checkout
operations run against the active account. The sniper uses the account
bound to each alert (see monitor wiring).
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.errors import raise_ovh_http_error
from app.services.ovh_service import (
    OVHServiceError,
    get_ovh_service,
    reset_ovh_service,
)
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

_VALID_ENDPOINTS = ("ovh-eu", "ovh-us", "ovh-ca")


def _mask(value: str | None) -> str | None:
    """Mask a secret, showing only the first 4 and last 4 characters."""
    if not value:
        return None
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


class AccountRequest(BaseModel):
    """Body for POST/PUT /api/accounts. application_secret may be empty on
    update to preserve the stored secret (masked-edit flow)."""
    label: str
    endpoint: str
    application_key: str
    application_secret: str = ""
    consumer_key: str


class AccountResponse(BaseModel):
    """Masked account view — application_secret is never returned in full."""
    id: str
    label: str
    endpoint: str
    application_key_masked: str | None = None
    consumer_key_masked: str | None = None
    application_secret_configured: bool = False
    created_at: str


def _to_response(acct: dict) -> AccountResponse:
    return AccountResponse(
        id=acct["id"],
        label=acct["label"],
        endpoint=acct["endpoint"],
        application_key_masked=_mask(acct.get("application_key")),
        consumer_key_masked=_mask(acct.get("consumer_key")),
        application_secret_configured=bool(acct.get("application_secret")),
        created_at=acct["created_at"],
    )


@router.get("", response_model=list[AccountResponse])
async def list_accounts() -> list[AccountResponse]:
    """List all stored accounts (secrets masked). The active account id is
    available via GET /api/accounts/active."""
    storage = get_storage()
    return [_to_response(a) for a in storage.list_accounts()]


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(request: AccountRequest) -> AccountResponse:
    """Create a new account. The first account created becomes active."""
    if request.endpoint not in _VALID_ENDPOINTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid endpoint. Must be one of: {', '.join(_VALID_ENDPOINTS)}.",
        )
    if not request.application_key or not request.consumer_key:
        raise HTTPException(status_code=400, detail="application_key and consumer_key are required.")
    if not request.application_secret:
        raise HTTPException(status_code=400, detail="application_secret is required when creating an account.")
    if not request.label.strip():
        raise HTTPException(status_code=400, detail="label is required.")
    storage = get_storage()
    acct_id = storage.save_account(
        account_id=None,
        label=request.label.strip(),
        endpoint=request.endpoint,
        application_key=request.application_key,
        application_secret=request.application_secret,
        consumer_key=request.consumer_key,
    )
    # First account becomes active automatically.
    if storage.get_active_account_id() is None:
        storage.set_active_account_id(acct_id)
    acct = storage.get_account(acct_id)
    logger.info("Account created: %s (%s)", acct_id, request.label)
    return _to_response(acct)


@router.get("/active")
async def get_active() -> dict:
    """Return the active account id (or null) plus a masked preview."""
    storage = get_storage()
    active_id = storage.get_active_account_id()
    if active_id is None:
        return {"active_account_id": None, "account": None}
    acct = storage.get_account(active_id)
    if acct is None:
        # Stale active id — clear it.
        storage.set_active_account_id(None)
        return {"active_account_id": None, "account": None}
    return {"active_account_id": active_id, "account": _to_response(acct).model_dump()}


class ActiveRequest(BaseModel):
    account_id: str


@router.put("/active")
async def set_active(request: ActiveRequest) -> dict:
    """Switch the active account and reload the monitor for it.

    If the monitor reload fails, the active account is reverted so the
    monitor doesn't poll the new account with the old account's alerts.
    """
    storage = get_storage()
    if storage.get_account(request.account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    previous_active = storage.get_active_account_id()
    storage.set_active_account_id(request.account_id)
    try:
        from app.services.monitor import get_monitor_service
        await get_monitor_service().reload()
    except Exception:
        logger.warning("monitor reload after account switch failed; reverting active account", exc_info=True)
        storage.set_active_account_id(previous_active)
        raise HTTPException(
            status_code=500,
            detail="Failed to reload monitor for the new account. Active account reverted.",
        ) from None
    logger.info("Active account switched to: %s", request.account_id)
    return {"status": "ok", "active_account_id": request.account_id}


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(account_id: str, request: AccountRequest) -> AccountResponse:
    """Update an account. An empty application_secret preserves the stored
    secret (so masked edits don't wipe it). The service for this account is
    reset so the new credentials take effect immediately."""
    if request.endpoint not in _VALID_ENDPOINTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid endpoint. Must be one of: {', '.join(_VALID_ENDPOINTS)}.",
        )
    storage = get_storage()
    if storage.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    storage.save_account(
        account_id=account_id,
        label=request.label.strip(),
        endpoint=request.endpoint,
        application_key=request.application_key,
        application_secret=request.application_secret,
        consumer_key=request.consumer_key,
    )
    reset_ovh_service(account_id)
    acct = storage.get_account(account_id)
    logger.info("Account updated: %s", account_id)
    return _to_response(acct)


@router.delete("/{account_id}")
async def delete_account(account_id: str) -> dict:
    """Delete an account. If it was active, fall back to another account or
    clear the active id (back to unconfigured)."""
    storage = get_storage()
    if storage.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    was_active = storage.get_active_account_id() == account_id
    storage.delete_account(account_id)
    reset_ovh_service(account_id)
    if was_active:
        remaining = storage.list_accounts()
        storage.set_active_account_id(remaining[0]["id"] if remaining else None)
        reset_ovh_service(None)
        # The active account changed, so the monitor must drop the deleted
        # account's alerts + stock cache and re-read the new active account's
        # (mirrors PUT /accounts/active). Without this the poller keeps
        # watching the deleted account's plans under the fallback account.
        try:
            from app.services.monitor import get_monitor_service
            await get_monitor_service().reload()
        except Exception:
            logger.warning("monitor reload after account deletion failed", exc_info=True)
    logger.info("Account deleted: %s (was_active=%s)", account_id, was_active)
    return {"status": "deleted"}


@router.post("/{account_id}/test")
async def test_account(account_id: str) -> dict:
    """Test an account's credentials by calling GET /me on the OVH API.
    Does not switch the active account."""
    storage = get_storage()
    if storage.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    service = get_ovh_service(account_id)
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="Account is not fully configured.")
    try:
        me = await asyncio.to_thread(service.get, "/me")
        return {
            "status": "ok",
            "nichandle": me.get("nichandle"),
            "firstname": me.get("firstname"),
            "name": me.get("name"),
            "email": me.get("email"),
            "endpoint": service.endpoint,
        }
    except OVHServiceError as e:
        raise_ovh_http_error(e)
