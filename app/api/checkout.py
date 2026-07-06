import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.errors import raise_ovh_http_error
from app.models.schemas import CheckoutRequest
from app.services.ovh_service import OVHServiceError, get_ovh_service

router = APIRouter(prefix="/api/checkout", tags=["checkout"])


@router.post("/{cart_id}")
async def checkout(cart_id: str, request: CheckoutRequest) -> dict[str, Any]:
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        return await asyncio.to_thread(
            service.checkout_cart,
            cart_id=cart_id,
            auto_pay=request.auto_pay,
            waive_retractation=request.waive_retractation,
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
