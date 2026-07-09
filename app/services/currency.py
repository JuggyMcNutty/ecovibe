"""FX rate service for display-only currency conversion.

Fetches daily ECB reference rates from Frankfurter (a free, no-API-key,
no-signup service backed by the European Central Bank). Rates are EUR-base
and cached in memory for 24 hours so repeated catalog loads don't hit the
upstream. All conversion is display-only — actual OVH charges happen in
the catalog's native currency regardless of the chosen display currency.

Usage:
    get_rates() -> {"base": "EUR", "date": "2026-07-09",
                    "rates": {"USD": 1.14, "GBP": 0.85, ...}} | None
    convert(amount, from_code, to_code, rates) -> float
"""
import logging
import threading
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_FRANKFURTER_URL = "https://api.frankfurter.app/latest"
_CACHE_TTL_SECONDS = 24 * 60 * 60  # daily rates: 24h is plenty

# In-memory cache (process-wide). Guarded by a lock so the monitor poller,
# rush orders and the catalog endpoint don't race on a cold cache.
_cache: dict[str, Any] | None = None
_cache_at: datetime | None = None
_lock = threading.Lock()


def get_rates() -> dict[str, Any] | None:
    """Return cached FX rates, fetching fresh ones if stale or absent.

    Returns ``{"base": "EUR", "date": "...", "rates": {...}}`` or ``None``
    if the upstream is unreachable (the caller should fall back to the
    catalog's native currency in that case). Never raises.
    """
    global _cache, _cache_at
    with _lock:
        now = datetime.now(timezone.utc)
        if (
            _cache is not None
            and _cache_at is not None
            and (now - _cache_at).total_seconds() < _CACHE_TTL_SECONDS
        ):
            return _cache
    # Fetch outside the lock so a slow upstream doesn't block other readers;
    # a concurrent caller may also fetch, but the last write wins and the
    # result is identical (daily rates don't change mid-fetch).
    fresh = _fetch_rates()
    if fresh is not None:
        with _lock:
            _cache = fresh
            _cache_at = datetime.now(timezone.utc)
    return fresh or _cache  # serve stale rather than failing if upstream is down


def _fetch_rates() -> dict[str, Any] | None:
    try:
        r = httpx.get(_FRANKFURTER_URL, timeout=10.0, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        if data.get("rates") and data.get("base"):
            return {"base": data["base"], "date": data.get("date", ""), "rates": data["rates"]}
        logger.warning("Frankfurter returned an unexpected payload: %s", data)
        return None
    except Exception:
        logger.warning("FX rates fetch failed (display conversion disabled)", exc_info=True)
        return None


def convert(amount: float, from_code: str, to_code: str, rates: dict[str, Any]) -> float:
    """Convert ``amount`` (in ``from_code`` units) to ``to_code`` units.

    ``rates`` is the Frankfurter payload (EUR-base). Same-currency is a
    no-op; missing rates fall back to the original amount so the UI never
    shows a blank.
    """
    if not from_code or not to_code or from_code == to_code:
        return amount
    rate_map = rates.get("rates", {}) if rates else {}
    base = rates.get("base", "EUR") if rates else "EUR"
    # amount_in_base = amount / (rate_of_from vs base); result = amount_in_base * (rate_of_to vs base)
    from_rate = rate_map.get(from_code) if from_code != base else 1.0
    to_rate = rate_map.get(to_code) if to_code != base else 1.0
    if not from_rate or not to_rate:
        return amount  # unknown currency: don't convert
    return amount * (to_rate / from_rate)


def reset_cache() -> None:
    """Clear the cache (used by tests)."""
    global _cache, _cache_at
    with _lock:
        _cache = None
        _cache_at = None
