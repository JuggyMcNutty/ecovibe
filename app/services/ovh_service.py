"""Thin wrapper around the official `ovh` Python SDK client.

This service encapsulates every OVH REST API call used by the application.
The API layer (`app/api/*`) talks exclusively to `OVHService` rather than
the raw SDK, which keeps OVH-specific concerns (camelCase kwargs, error
mapping, caching) in one place.

All methods are synchronous because the `ovh` SDK uses `requests` under the
hood. Callers in async route handlers must wrap calls with
`await asyncio.to_thread(...)` to avoid blocking the event loop.
"""
import logging
from typing import Any

import ovh
from ovh.exceptions import APIError

from app.config import get_settings
from app.services.cache import get_cache

logger = logging.getLogger(__name__)


class OVHServiceError(Exception):
    """Raised on any OVH API failure.

    Carries the upstream HTTP status code (when available) and OVH query ID
    so the API layer can map to an appropriate HTTP response and log the
    query ID for support follow-up.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        query_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.query_id = query_id


class OVHService:
    """Wraps a single `ovh.Client` instance.

    The client is only constructed when all three OVH credentials are present;
    otherwise `is_configured()` returns False and every call raises
    `OVHServiceError`. This lets the API layer return a clean 503 to clients
    that hit endpoints before configuring credentials.
    """

    def __init__(self, use_cache: bool | None = None) -> None:
        settings = get_settings()
        # Allow per-instance override of the global cache setting; otherwise
        # fall back to the value in Settings.
        self._use_cache = settings.use_cache if use_cache is None else use_cache
        self._client: ovh.Client | None = None
        self._setup_client()

    def _setup_client(self) -> None:
        """Construct the OVH client if all required credentials are present.

        Logs a warning naming any missing credential so misconfiguration is
        visible without needing to inspect the /health endpoint.
        """
        settings = get_settings()
        missing = [
            name
            for name, val in (
                ("application_key", settings.application_key),
                ("application_secret", settings.application_secret),
                ("consumer_key", settings.consumer_key),
            )
            if not val
        ]
        if missing:
            logger.warning(
                "OVH client not initialised: missing credentials: %s", ", ".join(missing)
            )
            return
        self._client = ovh.Client(
            endpoint=settings.endpoint,
            application_key=settings.application_key,
            application_secret=settings.application_secret,
            consumer_key=settings.consumer_key,
        )

    def is_configured(self) -> bool:
        """True iff the OVH client was constructed (all credentials were present)."""
        return self._client is not None

    def _call(self, method: str, path: str, **kwargs) -> Any:
        """Central dispatch: invoke the SDK and translate `APIError` → `OVHServiceError`.

        Routes to the SDK's verb-specific methods (`get`/`post`/`put`/`delete`)
        rather than `call()` directly, because the verb wrappers handle kwargs
        correctly: GET/DELETE kwargs become query string params, while
        POST/PUT kwargs become the JSON body. `call()` only accepts a `data`
        positional arg and would raise `TypeError` on any kwargs.

        The original `APIError` is chained via `from e` so the full traceback
        is preserved in server logs.
        """
        if not self._client:
            raise OVHServiceError("OVH API not configured. Please set credentials.")
        try:
            verb = method.upper()
            if verb == "GET":
                return self._client.get(path, **kwargs)
            elif verb == "POST":
                return self._client.post(path, **kwargs)
            elif verb == "PUT":
                return self._client.put(path, **kwargs)
            elif verb == "DELETE":
                return self._client.delete(path, **kwargs)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
        except APIError as e:
            status = None
            if e.response is not None:
                status = getattr(e.response, "status_code", None)
            raise OVHServiceError(str(e), status_code=status, query_id=e.query_id) from e

    # ---- HTTP verb convenience wrappers ----

    def get(self, path: str, **kwargs) -> Any:
        return self._call("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        return self._call("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> Any:
        return self._call("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> Any:
        return self._call("DELETE", path, **kwargs)

    # ---- Catalog & availability ----

    def fetch_catalog(self, subsidiary: str = "IE", force: bool = False) -> dict[str, Any]:
        """Fetch the public ECO server catalog for a given OVH subsidiary.

        Results are cached per-subsidiary when `use_cache` is enabled. Pass
        `force=True` to bypass the cache (e.g. for an explicit refresh button).
        The TTL is read from `Settings.cache_ttl` so `OVH_CACHE_TTL` is honoured.
        """
        cache_key = f"catalog_{subsidiary}"
        cache = get_cache(ttl=get_settings().cache_ttl)

        if self._use_cache and not force:
            cached = cache.get(cache_key)
            if cached:
                return cached

        catalog = self.get("/order/catalog/public/eco", ovhSubsidiary=subsidiary)

        if self._use_cache:
            cache.set(cache_key, catalog)

        return catalog

    def get_availability(self, plan_code: str) -> list[dict[str, Any]]:
        """Return the list of currently orderable FQN configurations for a plan.

        OVH only returns configs that are *actually* orderable right now, so
        absence of an FQN from this list means it is out of stock.
        """
        return self.get("/order/eco/availableConfiguration", planCode=plan_code)

    def get_plan_price(self, plan_code: str, subsidiary: str = "IE") -> int | None:
        """Look up the default price (in microcents of euro) for a plan.

        Walks the catalog to find the matching planCode, then its `default`
        price entry. Returns None if the plan is not in the catalog or has
        no usable price field. Used by the price-tracking + max-price cap.
        """
        catalog = self.fetch_catalog(subsidiary=subsidiary)
        for plan in catalog.get("plans", []):
            if plan.get("planCode") == plan_code:
                prices = plan.get("prices", [])
                for p in prices:
                    if p.get("label") == "default":
                        price = p.get("price", {})
                        ucents = price.get("priceInUcents")
                        if isinstance(ucents, int):
                            return ucents
        return None

    # ---- Cart lifecycle ----
    #
    # OVH's cart flow is: create → assign → add server → add options →
    # add configuration → checkout. Each step is a separate REST call.
    # The frontend uses the one-shot `/api/checkout/rush` endpoint rather
    # than calling these granular methods directly.

    def create_cart(self, description: str = "") -> dict[str, Any]:
        """Create a new shopping cart. Returns the cart payload including `cartId`."""
        return self.post("/order/cart", description=description)

    def assign_cart(self, cart_id: str) -> None:
        """Bind a cart to the authenticated account. Required before adding items."""
        self.post(f"/order/cart/{cart_id}/assign")

    def add_server_to_cart(
        self, cart_id: str, plan_code: str, duration: str = "P1M", quantity: int = 1
    ) -> dict[str, Any]:
        """Add an ECO server line item to the cart. Returns the item payload
        (containing `itemId`, needed by `add_option_to_cart` / configuration)."""
        return self.post(
            f"/order/cart/{cart_id}/eco",
            planCode=plan_code,
            duration=duration,
            pricingMode="default",
            quantity=quantity,
        )

    def add_option_to_cart(
        self, cart_id: str, item_id: int, plan_code: str, duration: str = "P1M"
    ) -> dict[str, Any]:
        """Attach an option (RAM/storage/bandwidth upgrade) to an existing line item."""
        return self.post(
            f"/order/cart/{cart_id}/eco/options",
            itemId=item_id,
            planCode=plan_code,
            duration=duration,
            pricingMode="default",
            quantity=1,
        )

    def add_configuration_to_cart(
        self, cart_id: str, item_id: int, label: str, value: str
    ) -> None:
        """Attach a configuration key/value pair to an item.

        Used for `dedicated_datacenter`, `region`, `dedicated_os`, etc.
        """
        self.post(
            f"/order/cart/{cart_id}/eco/configuration",
            itemId=item_id,
            label=label,
            value=value,
        )

    def get_cart(self, cart_id: str) -> dict[str, Any]:
        """Fetch the current state of a cart (items, prices, expiry)."""
        return self.get(f"/order/cart/{cart_id}")

    def get_cart_summary(self, cart_id: str) -> dict[str, Any]:
        """Fetch the checkout summary (totals, taxes, payment URL preview)."""
        return self.get(f"/order/cart/{cart_id}/summary")

    def checkout_cart(
        self, cart_id: str, auto_pay: bool = False, waive_retractation: bool = False
    ) -> dict[str, Any]:
        """Finalise the cart into an order.

        `auto_pay=True` charges the account's preferred payment method
        immediately. `waive_retractation=True` skips the legal withdrawal
        period — essential for flash sales where you want the server now.
        """
        return self.post(
            f"/order/cart/{cart_id}/checkout",
            autoPayWithPreferredPaymentMethod=auto_pay,
            waiveRetractationPeriod=waive_retractation,
        )

    def delete_cart(self, cart_id: str) -> None:
        """Delete an abandoned or failed cart. Best-effort cleanup."""
        self.delete(f"/order/cart/{cart_id}")


# Module-level singleton. The first call constructs the service; later calls
# reuse it. Reset by setting `_ovh_service = None` (tests do this).
_ovh_service: OVHService | None = None


def get_ovh_service(use_cache: bool | None = None) -> OVHService:
    """Return the shared OVHService singleton, creating it on first use."""
    global _ovh_service
    if _ovh_service is None:
        _ovh_service = OVHService(use_cache=use_cache)
    return _ovh_service
