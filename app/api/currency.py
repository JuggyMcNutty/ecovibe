"""Currency endpoints: display-conversion FX rates."""
import logging

from fastapi import APIRouter, HTTPException

from app.services.currency import get_rates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/currency", tags=["currency"])


@router.get("/rates")
async def get_currency_rates() -> dict:
    """Return cached ECB/Frankfurter FX rates (EUR-base) for display conversion.

    Returns ``{"base": "EUR", "date": "...", "rates": {...}}``. 503 if the
    upstream is unreachable — the frontend falls back to the catalog's
    native currency in that case.
    """
    rates = get_rates()
    if rates is None:
        raise HTTPException(status_code=503, detail="FX rates unavailable; showing native catalog currency.")
    return rates
