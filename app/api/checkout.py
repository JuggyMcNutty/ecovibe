"""Checkout endpoints - place orders and one-shot rush orders."""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.errors import raise_ovh_http_error
from app.models.schemas import CheckoutRequest
from app.services.ovh_service import OVHServiceError, get_ovh_service
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/checkout", tags=["checkout"])


class RushOrderRequest(BaseModel):
    """Full description of a one-shot rush order.

    `datacenters` is ordered - the first one OVH accepts wins. `max_price`
    is in microcents of euro (matches OVH's `priceInUcents` field) and
    refuses checkout if the current price exceeds it.
    """

    plan_code: str
    fqn: str
    ram: str | None = None
    storage: str | None = None
    bandwidth: str | None = None
    datacenters: list[str] = []
    region: str = "europe"
    os: str = "none_64.en"
    duration: str = "P1M"
    auto_pay: bool = False
    waive_retractation: bool = False
    max_price: int | None = None


async def _execute_rush_order(service, req: RushOrderRequest) -> dict[str, Any]:
    """Build a cart, add items + configs, and checkout.

    Tries each datacenter in `req.datacenters` in order until one succeeds.
    On any failure after cart creation, the cart is deleted to avoid
    orphans. Region and OS configs are applied in parallel since they are
    independent of each other.

    Raises `OVHServiceError` on any upstream failure - the caller is
    responsible for mapping it to an HTTP response.
    """
    datacenters = req.datacenters or [""]
    cart = await asyncio.to_thread(service.create_cart, "Rush Order")
    logger.info("created cart %s for plan %s", cart.get("cartId"), req.plan_code)
    try:
        await asyncio.to_thread(service.assign_cart, cart["cartId"])
        logger.info("assigned cart %s", cart.get("cartId"))
    except OVHServiceError as e:
        logger.error("assign failed for cart %s: %s", cart.get("cartId"), e)
        try:
            await asyncio.to_thread(service.delete_cart, cart["cartId"])
        except OVHServiceError:
            logger.warning("could not clean up orphaned cart %s", cart.get("cartId"))
        raise

    try:
        server_item = await asyncio.to_thread(
            service.add_server_to_cart,
            cart_id=cart["cartId"],
            plan_code=req.plan_code,
            duration=req.duration,
            quantity=1,
        )
        item_id = server_item["itemId"]
        logger.info("added server %s to cart %s (item %s)", req.plan_code, cart.get("cartId"), item_id)

        # Add each selected addon (RAM/storage/bandwidth) sequentially.
        for addon in filter(None, [req.ram, req.storage, req.bandwidth]):
            await asyncio.to_thread(
                service.add_option_to_cart,
                cart_id=cart["cartId"],
                item_id=item_id,
                plan_code=addon,
                duration=req.duration,
            )

        # Multi-DC fallback: try each datacenter until one is accepted.
        dc_set = False
        last_dc_error: OVHServiceError | None = None
        for dc in datacenters:
            if not dc:
                continue
            try:
                await asyncio.to_thread(
                    service.add_configuration_to_cart,
                    cart_id=cart["cartId"],
                    item_id=item_id,
                    label="dedicated_datacenter",
                    value=dc,
                )
                dc_set = True
                break
            except OVHServiceError as e:
                last_dc_error = e
                logger.info("datacenter %s rejected for %s: %s", dc, req.plan_code, e)
        # If every DC was rejected, surface the last error.
        if not dc_set and datacenters and datacenters[0]:
            if last_dc_error:
                raise last_dc_error

        # Region + OS configs are independent - apply them in parallel.
        remaining_configs = [
            ("region", req.region),
            ("dedicated_os", req.os),
        ]
        await asyncio.gather(*[
            asyncio.to_thread(
                service.add_configuration_to_cart,
                cart_id=cart["cartId"],
                item_id=item_id,
                label=label,
                value=value,
            )
            for label, value in remaining_configs
        ])

        result = await asyncio.to_thread(
            service.checkout_cart,
            cart_id=cart["cartId"],
            auto_pay=req.auto_pay,
            waive_retractation=req.waive_retractation,
        )
        return result
    except OVHServiceError:
        # Any failure after cart creation → clean up the orphaned cart.
        try:
            await asyncio.to_thread(service.delete_cart, cart["cartId"])
        except OVHServiceError:
            pass
        raise


@router.post("/{cart_id}")
async def checkout(cart_id: str, request: CheckoutRequest) -> dict[str, Any]:
    """Legacy single-cart checkout (for pre-built carts)."""
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")
    try:
        result = await asyncio.to_thread(
            service.checkout_cart,
            cart_id=cart_id,
            auto_pay=request.auto_pay,
            waive_retractation=request.waive_retractation,
        )
    except OVHServiceError as e:
        raise_ovh_http_error(e)
    storage = get_storage()
    storage.log_order(
        order_id=result.get("orderId"),
        cart_id=cart_id,
        plan_code="",
        status=None,
        url=result.get("url"),
        placed_at=datetime.now(timezone.utc),
    )
    return result


@router.post("/rush")
async def rush_checkout(request: RushOrderRequest) -> dict[str, Any]:
    """One-shot rush order: build cart, add server/options/configs, checkout.

    Tries each datacenter in `datacenters` list until one succeeds.
    Enforces `max_price` if set (refuses checkout if the current catalog
    price exceeds the threshold). Logs the order to SQLite on success.
    """
    service = get_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")

    # Budget guard: refuse to checkout if the price has spiked past the cap.
    if request.max_price is not None:
        try:
            price = await asyncio.to_thread(service.get_plan_price, request.plan_code)
        except OVHServiceError:
            price = None
        if price is not None and price > request.max_price:
            raise HTTPException(
                status_code=409,
                detail=f"Price {price/1_000_000:.2f} exceeds max {request.max_price/1_000_000:.2f}",
            )

    try:
        result = await _execute_rush_order(service, request)
    except OVHServiceError as e:
        raise_ovh_http_error(e)

    storage = get_storage()
    storage.log_order(
        order_id=result.get("orderId"),
        cart_id="",
        plan_code=request.plan_code,
        status=None,
        url=result.get("url"),
        placed_at=datetime.now(timezone.utc),
    )
    return result
