"""Account info, payment methods, and default checkout preferences."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.errors import raise_ovh_http_error
from app.services.ovh_service import OVHServiceError, get_ovh_service
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])


class CheckoutDefaults(BaseModel):
    """Default checkout preferences persisted in the settings table."""
    auto_pay: bool = False
    waive_retractation: bool = True
    duration: str = "P1M"
    max_price: int | None = None


@router.get("/me")
async def get_me() -> dict:
    """Fetch the OVH account info (nichandle, name, email, currency, etc.)."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        return await asyncio.to_thread(service.get, "/me")
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.get("/payment-methods")
async def get_payment_methods() -> dict:
    """List available payment methods on the OVH account."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        ids = await asyncio.to_thread(service.get, "/me/paymentMethod")
        methods = []
        for pid in ids[:10]:
            try:
                detail = await asyncio.to_thread(service.get, f"/me/paymentMethod/{pid}")
                methods.append(detail)
            except OVHServiceError:
                pass
        return {"payment_methods": methods}
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.get("/checkout-defaults")
async def get_checkout_defaults() -> CheckoutDefaults:
    """Return the persisted default checkout preferences."""
    storage = get_storage()
    import json
    raw = storage.get_setting("checkout_defaults")
    if raw:
        try:
            data = json.loads(raw)
            return CheckoutDefaults(**data)
        except Exception:
            pass
    return CheckoutDefaults()


@router.put("/checkout-defaults")
async def set_checkout_defaults(request: CheckoutDefaults) -> dict:
    """Save default checkout preferences (used by catalog order + sniper)."""
    storage = get_storage()
    import json
    storage.set_setting("checkout_defaults", json.dumps(request.model_dump()))
    return {"status": "saved", "defaults": request.model_dump()}
