"""Checkout endpoints - place orders and one-shot rush orders."""
import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.errors import raise_ovh_http_error
from app.models.schemas import CheckoutRequest
from app.services.ovh_service import OVHServiceError, get_active_ovh_service
from app.services.storage import get_storage

logger = logging.getLogger(__name__)


def _trace(msg: str) -> None:
    """Print to stderr so it always shows in the uvicorn console,
    bypassing the logging config that filters non-uvicorn loggers."""
    print(f"[rush] {msg}", file=sys.stderr, flush=True)

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
    datacenters = list(req.datacenters)
    # If no DCs were selected, auto-discover the plan's available DCs from
    # the catalog. OVH requires `dedicated_datacenter` before checkout or
    # it returns 400 "in customFields there must be a field with name
    # 'dedicated_datacenter'".
    if not any(d for d in datacenters):
        try:
            plan_dcs = await asyncio.to_thread(
                service.get_plan_datacenters, req.plan_code
            )
            if plan_dcs:
                datacenters = plan_dcs
                _trace(f"no DCs selected - auto-trying plan DCs: {plan_dcs}")
        except OVHServiceError:
            pass
    if not any(d for d in datacenters):
        datacenters = [""]
    _trace(f"start: plan={req.plan_code} endpoint={service.endpoint} region={req.region}")
    cart = await asyncio.to_thread(service.create_cart, "Rush Order")
    _trace(f"created cart {cart.get('cartId')} for plan {req.plan_code}")
    try:
        await asyncio.to_thread(service.assign_cart, cart["cartId"])
        _trace(f"assigned cart {cart.get('cartId')}")
    except OVHServiceError as e:
        _trace(f"assign FAILED for cart {cart.get('cartId')}: {e}")
        try:
            await asyncio.to_thread(service.delete_cart, cart["cartId"])
        except OVHServiceError:
            pass
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
        _trace(f"added server {req.plan_code} to cart {cart.get('cartId')} (item {item_id})")

        # Add each selected addon (RAM/storage/bandwidth) sequentially.
        for addon in filter(None, [req.ram, req.storage, req.bandwidth]):
            _trace(f"adding addon {addon} to cart {cart.get('cartId')}")
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
                _trace(f"dc {dc} accepted for cart {cart.get('cartId')}")
                break
            except OVHServiceError as e:
                last_dc_error = e
                logger.info("datacenter %s rejected for %s: %s", dc, req.plan_code, e)
        # If no DC was accepted, surface the error. OVH requires
        # dedicated_datacenter before checkout - proceeding without one
        # results in a 400 or a misleading 500.
        if not dc_set:
            if last_dc_error:
                raise last_dc_error
            raise OVHServiceError(
                "No datacenter available for this plan. "
                "Select a datacenter in the rush order form."
            )

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
        _trace(f"configs set (region={req.region}, os={req.os}) for cart {cart.get('cartId')}")

        result = await asyncio.to_thread(
            service.checkout_cart,
            cart_id=cart["cartId"],
            auto_pay=req.auto_pay,
            waive_retractation=req.waive_retractation,
        )
        _trace(f"checkout OK for cart {cart.get('cartId')}")
        return result
    except OVHServiceError as e:
        _trace(f"step FAILED for cart {cart.get('cartId')}: {e}")
        try:
            await asyncio.to_thread(service.delete_cart, cart["cartId"])
        except OVHServiceError:
            pass
        raise


@router.post("/rush")
async def rush_checkout(request: RushOrderRequest) -> dict[str, Any]:
    """One-shot rush order: build cart, add server/options/configs, checkout.

    Tries each datacenter in `datacenters` list until one succeeds.
    Enforces `max_price` if set (refuses checkout if the current catalog
    price exceeds the threshold). Logs the order to SQLite on success.
    """
    service = get_active_ovh_service()
    if not service.is_configured():
        raise HTTPException(status_code=503, detail="OVH API not configured")

    # Budget guard: refuse to checkout if the price has spiked past the cap.
    if request.max_price is not None:
        try:
            price = await asyncio.to_thread(service.get_plan_price, request.plan_code)
        except OVHServiceError:
            price = None
        if price is not None and price > request.max_price:
            cur = service.default_currency_code()
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Price {price / 100_000_000:.2f} {cur} exceeds "
                    f"max {request.max_price / 100_000_000:.2f} {cur}"
                ),
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
        account_id=service.account_id,
    )
    return result


@router.post("/{cart_id}")
async def checkout(cart_id: str, request: CheckoutRequest) -> dict[str, Any]:
    """Legacy single-cart checkout (for pre-built carts)."""
    service = get_active_ovh_service()
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
        account_id=service.account_id,
    )
    return result
