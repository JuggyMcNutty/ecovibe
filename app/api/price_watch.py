"""Price-watch CRUD: notify when a plan's monthly price drops to a cap.

Watches are account-scoped and evaluated by the monitor's periodic
price/promo check (see MonitorService._maybe_check_prices_and_promos).
"""
import logging

from fastapi import APIRouter, HTTPException

from app.models.schemas import PriceWatchCreate
from app.services.ovh_service import get_active_ovh_service
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/price-watches", tags=["price-watches"])


@router.get("")
async def list_price_watches() -> dict:
    """List the active account's price watches."""
    storage = get_storage()
    service = get_active_ovh_service()
    return {"watches": storage.load_price_watches(account_id=service.account_id)}


@router.post("", status_code=201)
async def create_price_watch(request: PriceWatchCreate) -> dict:
    """Create (or update) the price watch for a plan.

    One watch per plan per account: re-posting the same plan updates the
    threshold and re-arms the notification state.
    """
    storage = get_storage()
    service = get_active_ovh_service()
    watch_id = storage.upsert_price_watch(
        None, request.plan_code, request.threshold_ucents,
        currency_code=service.default_currency_code(),
        account_id=service.account_id,
    )
    logger.info(
        "price watch saved: %s below %d ucents",
        request.plan_code, request.threshold_ucents,
    )
    watches = storage.load_price_watches(account_id=service.account_id)
    watch = next((w for w in watches if w["id"] == watch_id), None)
    return {"watch": watch}


@router.delete("/{watch_id}")
async def delete_price_watch(watch_id: str) -> dict:
    """Delete a price watch. 404 if not found under the active account."""
    storage = get_storage()
    service = get_active_ovh_service()
    if not storage.delete_price_watch(watch_id, account_id=service.account_id):
        raise HTTPException(status_code=404, detail="Price watch not found")
    return {"status": "deleted", "watch_id": watch_id}
