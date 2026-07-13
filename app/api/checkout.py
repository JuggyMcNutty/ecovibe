"""Checkout endpoints - place orders and one-shot rush orders."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.errors import raise_ovh_http_error
from app.models.schemas import CheckoutRequest
from app.services.monitor import DuplicateAlertError, get_monitor_service, get_sniper_service
from app.services.ovh_service import OVHServiceError, get_active_ovh_service
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/checkout", tags=["checkout"])


class RushOrderRequest(BaseModel):
    """Full description of a one-shot rush order.

    `datacenters` is ordered - the first one OVH accepts wins. `max_price`
    is in microcents of euro (matches OVH's `priceInUcents` field) and
    refuses checkout if the current price exceeds it.

    When `arm_if_oos` is true and the requested FQN is not currently
    orderable, the request does NOT fire an order. Instead it creates a
    checkout profile + alert and arms the sniper so the background poller
    auto-orders when OVH reports the config back in stock.
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
    arm_if_oos: bool = False


async def _enforce_max_price(service, req: RushOrderRequest) -> None:
    """Budget guard: refuse to order if the price has spiked past the cap.

    Called from `_execute_rush_order` so both the manual rush-order route
    and the sniper's auto-order path (which calls `_execute_rush_order`
    directly, bypassing the route handler) enforce the cap identically.
    Fails closed: if the price can't be verified (lookup error), the order
    is refused rather than allowed through unchecked - a cap that can't be
    confirmed is not a cap.
    """
    if req.max_price is None:
        return
    try:
        price = await asyncio.to_thread(service.get_plan_price, req.plan_code)
    except OVHServiceError as e:
        raise OVHServiceError(
            f"Could not verify price against max_price cap: {e.message}",
            status_code=409,
        ) from e
    cur = service.default_currency_code()
    if price is None:
        raise OVHServiceError(
            f"Could not verify price against max_price cap for {req.plan_code}",
            status_code=409,
        )
    if price > req.max_price:
        raise OVHServiceError(
            f"Price {price / 100_000_000:.2f} {cur} exceeds "
            f"max {req.max_price / 100_000_000:.2f} {cur}",
            status_code=409,
        )


async def _execute_rush_order(service, req: RushOrderRequest) -> dict[str, Any]:
    """Build a cart, add items + configs, and checkout.

    Enforces `req.max_price` (if set) before touching the cart. Tries each
    datacenter in `req.datacenters` in order until one succeeds. On any
    failure after cart creation, the cart is deleted to avoid orphans.
    Region and OS configs are applied in parallel since they are
    independent of each other.

    Raises `OVHServiceError` on any upstream failure (including a
    max_price breach) - the caller is responsible for mapping it to an
    HTTP response.
    """
    await _enforce_max_price(service, req)
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
                logger.info("no DCs selected - auto-trying plan DCs: %s", plan_dcs)
        except OVHServiceError:
            pass
    if not any(d for d in datacenters):
        datacenters = [""]
    logger.info("rush start: plan=%s endpoint=%s region=%s", req.plan_code, service.endpoint, req.region)
    cart = await asyncio.to_thread(service.create_cart, "Rush Order")
    logger.info("created cart %s for plan %s", cart.get('cartId'), req.plan_code)
    try:
        await asyncio.to_thread(service.assign_cart, cart["cartId"])
        logger.info("assigned cart %s", cart.get('cartId'))
    except OVHServiceError as e:
        logger.info("assign FAILED for cart %s: %s", cart.get('cartId'), e)
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
        logger.info("added server %s to cart %s (item %s)", req.plan_code, cart.get('cartId'), item_id)

        # Add each selected addon (RAM/storage/bandwidth) sequentially.
        for addon in filter(None, [req.ram, req.storage, req.bandwidth]):
            logger.info("adding addon %s to cart %s", addon, cart.get('cartId'))
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
                logger.info("dc %s accepted for cart %s", dc, cart.get('cartId'))
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
        logger.info("configs set (region=%s, os=%s) for cart %s", req.region, req.os, cart.get('cartId'))

        result = await asyncio.to_thread(
            service.checkout_cart,
            cart_id=cart["cartId"],
            auto_pay=req.auto_pay,
            waive_retractation=req.waive_retractation,
        )
        logger.info("checkout OK for cart %s", cart.get('cartId'))
        return result
    except OVHServiceError as e:
        logger.info("rush step FAILED for cart %s: %s", cart.get('cartId'), e)
        try:
            await asyncio.to_thread(service.delete_cart, cart["cartId"])
        except OVHServiceError:
            pass
        raise


async def _arm_sniper_from_rush(request: RushOrderRequest, service) -> dict[str, Any]:
    """Create a profile + alert and arm the sniper for an out-of-stock rush.

    Reuses an existing alert for the same (plan_code, fqn) when present so
    re-arming a previously-fired sniper doesn't create duplicates. The
    alert uses the exact FQN as its pattern so the sniper fires only when
    that precise configuration comes back in stock. Returns a payload
    describing the armed state; raises HTTPException on unrecoverable
    failure (and cleans up any profile it created).
    """
    storage = get_storage()
    account_id = service.account_id

    # 1) Save a checkout profile mirroring the rush request.
    profile_id = str(uuid.uuid4())
    profile_record = {
        "id": profile_id,
        "name": f"Quick: {request.plan_code}",
        "plan_code": request.plan_code,
        "fqn": request.fqn,
        "ram": request.ram,
        "storage": request.storage,
        "bandwidth": request.bandwidth,
        "datacenters": ",".join(request.datacenters),
        "region": request.region,
        "os": request.os,
        "duration": request.duration,
        "auto_pay": request.auto_pay,
        "waive_retractation": request.waive_retractation,
        "max_price": request.max_price,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
    }
    try:
        storage.upsert_profile(profile_record)
    except Exception as e:
        logger.warning("failed to save sniper profile: %s", e)
        raise HTTPException(status_code=500, detail="Failed to save checkout profile") from e

    # 2) Find or create an alert scoped to this exact FQN.
    monitor = get_monitor_service()
    alert_id: str | None = None
    for a in monitor.get_alerts():
        if a.plan_code == request.plan_code and a.fqn_pattern == request.fqn:
            alert_id = a.id
            break
    if alert_id is None:
        try:
            alert = await monitor.add_alert(request.plan_code, request.fqn)
            alert_id = alert.id
        except DuplicateAlertError:
            for a in monitor.get_alerts():
                if a.plan_code == request.plan_code and a.fqn_pattern == request.fqn:
                    alert_id = a.id
                    break
    if alert_id is None:
        try:
            storage.delete_profile(profile_id)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Failed to create alert")

    # 3) Arm the sniper - the background poller auto-orders on restock.
    # Pass the watch details so the poller keeps firing this sniper even after
    # the user switches away from its account (see MonitorService._sweep_snipers).
    sniper = get_sniper_service()
    armed_alert = monitor.get_alert(alert_id)
    sniper.arm(
        alert_id, profile_id,
        plan_code=request.plan_code, fqn_pattern=request.fqn,
        account_id=armed_alert.account_id if armed_alert else None,
    )
    logger.info(
        "armed sniper for OOS plan %s (alert=%s profile=%s)",
        request.plan_code, alert_id, profile_id,
    )
    return {
        "status": "armed",
        "alert_id": alert_id,
        "profile_id": profile_id,
        "plan_code": request.plan_code,
        "fqn": request.fqn,
        "message": (
            f"{request.plan_code} is out of stock. Sniper armed - the "
            "monitor will auto-order when it comes back in stock."
        ),
    }


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

    # When armed-mode is requested, check whether the FQN is currently
    # orderable before attempting an order. If it is out of stock, arm the
    # sniper instead of firing a doomed order that OVH rejects with
    # "... is not available in <dc>". The monitor's background poller
    # (independent of any browser connection) then auto-orders on restock.
    if request.arm_if_oos:
        try:
            avail_configs = await asyncio.to_thread(
                service.get_availability, request.plan_code
            )
        except OVHServiceError:
            # Availability endpoint unreachable - fall through to the
            # normal order attempt; the sniper can't fire without stock
            # data anyway, and the user gets the real OVH error.
            avail_configs = None
        if avail_configs is not None:
            orderable_fqns = {c.get("fqn", "") for c in avail_configs}
            if request.fqn not in orderable_fqns:
                return await _arm_sniper_from_rush(request, service)

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
    logger.info(
        "order placed: %s fqn=%s order=%s",
        request.plan_code, request.fqn, result.get("orderId"),
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
    logger.info("order placed from cart %s: order=%s", cart_id, result.get("orderId"))
    return result
