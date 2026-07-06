"""Granular cart lifecycle endpoints (legacy - prefer /api/checkout/rush)."""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.errors import raise_ovh_http_error
from app.models.schemas import (
    AddConfigRequest,
    AddOptionRequest,
    AddServerRequest,
    CreateCartRequest,
)
from app.services.ovh_service import OVHServiceError, get_ovh_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cart", tags=["cart"])


@router.post("")
async def create_cart(request: CreateCartRequest) -> dict[str, Any]:
    """Create a cart and immediately assign it to the authenticated account.

    If assignment fails, the orphaned cart is deleted to avoid leaving
    dangling carts on OVH's side.
    """
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        cart = await asyncio.to_thread(service.create_cart, description=request.description)
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    try:
        await asyncio.to_thread(service.assign_cart, cart["cartId"])
    except OVHServiceError as e:
        # Clean up the orphaned cart before surfacing the error.
        try:
            await asyncio.to_thread(service.delete_cart, cart["cartId"])
        except OVHServiceError:
            logger.warning("could not clean up orphaned cart %s", cart.get("cartId"))
        raise_ovh_http_error(e)
    return cart


@router.get("/{cart_id}")
async def get_cart(cart_id: str) -> dict[str, Any]:
    """Fetch the current state of a cart (items, prices, expiry)."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        return await asyncio.to_thread(service.get_cart, cart_id)
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.post("/{cart_id}/server")
async def add_server(cart_id: str, request: AddServerRequest) -> dict[str, Any]:
    """Add an ECO server line item to the cart. Returns the item (with `itemId`)."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        return await asyncio.to_thread(
            service.add_server_to_cart,
            cart_id=cart_id,
            plan_code=request.plan_code,
            duration=request.duration,
            quantity=request.quantity,
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.post("/{cart_id}/options")
async def add_option(cart_id: str, request: AddOptionRequest) -> dict[str, Any]:
    """Attach an option (RAM/storage/bandwidth upgrade) to a line item."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        return await asyncio.to_thread(
            service.add_option_to_cart,
            cart_id=cart_id,
            item_id=request.item_id,
            plan_code=request.plan_code,
            duration=request.duration,
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)


@router.post("/{cart_id}/config")
async def add_config(cart_id: str, request: AddConfigRequest) -> dict[str, Any]:
    """Attach a configuration key/value pair to an item.

    Used for `dedicated_datacenter`, `region`, `dedicated_os`, etc.
    """
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        await asyncio.to_thread(
            service.add_configuration_to_cart,
            cart_id=cart_id,
            item_id=request.item_id,
            label=request.label,
            value=request.value,
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    return {"status": "ok", "label": request.label, "value": request.value}


@router.get("/{cart_id}/summary")
async def get_summary(cart_id: str) -> dict[str, Any]:
    """Fetch the checkout summary (totals, taxes, payment URL preview)."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        return await asyncio.to_thread(service.get_cart_summary, cart_id)
    except OVHServiceError as e:
        raise_ovh_http_error(e)
