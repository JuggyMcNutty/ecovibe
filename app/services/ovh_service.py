"""Wrapper around the official ovh Python SDK.

All OVH API calls go through here so the rest of the app doesn't need to
know about camelCase kwargs, error mapping, or caching. Methods are sync
(ovh SDK uses requests) - wrap with asyncio.to_thread in async handlers.
"""
import logging
import threading
from typing import Any

import ovh
from ovh.exceptions import APIError

from app.config import get_settings
from app.services.cache import get_cache

logger = logging.getLogger(__name__)


class OVHServiceError(Exception):
    """Carries the OVH status code and query ID for error mapping."""

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
    """Wraps an ovh.Client. Credentials come from the DB, not env vars.

    Call reconfigure() after saving new credentials to rebuild the client
    without restarting.
    """

    def __init__(self, use_cache: bool | None = None) -> None:
        settings = get_settings()
        self._use_cache = settings.use_cache if use_cache is None else use_cache
        self._client: ovh.Client | None = None
        self._endpoint: str = settings.endpoint
        # Serialises all ovh.Client calls. The shared ovh.Client bundles a
        # requests.Session and a lazily-cached server-time delta that are
        # NOT safe under concurrent access (the monitor poller, rush orders
        # and account endpoints all call in via asyncio.to_thread). Without
        # the lock, concurrent requests.Session.prepare_request/send can
        # race on the shared cookie jar, and a stale time_delta makes every
        # signature wrong so OVH replies "This application key is invalid".
        self._lock = threading.Lock()
        self._setup_client()

    def _setup_client(self) -> None:
        """Build the OVH client from DB-stored credentials."""
        creds = self._load_credentials()
        if not creds:
            logger.info("No OVH credentials found in database - run the setup wizard.")
            return

        missing = [
            name
            for name, val in (
                ("application_key", creds.get("application_key")),
                ("application_secret", creds.get("application_secret")),
                ("consumer_key", creds.get("consumer_key")),
            )
            if not val
        ]
        if missing:
            logger.warning("OVH client not initialised: missing: %s", ", ".join(missing))
            return

        self._endpoint = creds.get("endpoint", get_settings().endpoint)
        self._client = ovh.Client(
            endpoint=self._endpoint,
            application_key=creds["application_key"],
            application_secret=creds["application_secret"],
            consumer_key=creds["consumer_key"],
        )
        logger.info("OVH client ready for endpoint: %s", self._endpoint)

    @staticmethod
    def _load_credentials() -> dict[str, str] | None:
        """Fetch credentials from the DB, or None if not configured."""
        try:
            from app.services.storage import get_storage
            storage = get_storage()
            return storage.load_credentials()
        except Exception:
            logger.warning("could not load credentials from storage", exc_info=True)
            return None

    def reconfigure(self) -> None:
        """Rebuild the OVH client after credentials have been saved/updated.

        Called by the setup wizard after POST /api/setup/credentials.
        """
        with self._lock:
            self._client = None
            self._setup_client()

    @property
    def endpoint(self) -> str:
        """Return the configured OVH endpoint (e.g. 'ovh-eu')."""
        return self._endpoint

    def is_configured(self) -> bool:
        return self._client is not None

    def _call(self, method: str, path: str, **kwargs) -> Any:
        """Route to the SDK's verb wrappers (not call() directly).

        The verb wrappers handle kwargs properly: GET/DELETE -> query string,
        POST/PUT -> JSON body. call() would TypeError on kwargs.

        All calls are serialised with ``self._lock`` because the shared
        ``ovh.Client`` (and its ``requests.Session`` + cached server-time
        delta) is not safe under concurrent access from the background
        poller, rush orders and account endpoints.

        On a 403 "This application key is invalid" we refresh the client's
        cached time delta and retry once: OVH reports a signature mismatch
        (which a stale timestamp causes) as an invalid application key, and
        the SDK caches the delta forever so it never self-corrects.
        """
        if not self._client:
            raise OVHServiceError("OVH API not configured. Please set credentials.")
        with self._lock:
            try:
                return self._do_call(method, path, **kwargs)
            except OVHServiceError as e:
                if e.status_code == 403 and self._is_stale_signature(e):
                    logger.warning(
                        "OVH 403 'application key is invalid' - refreshing time_delta and retrying (query_id=%s)",
                        e.query_id,
                    )
                    self._reset_time_delta()
                    return self._do_call(method, path, **kwargs)
                raise

    def _do_call(self, method: str, path: str, **kwargs) -> Any:
        """Single OVH API call. Caller must hold self._lock."""
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

    @staticmethod
    def _is_stale_signature(e: OVHServiceError) -> bool:
        """OVH returns 'This application key is invalid' for a bad signature,
        which is what a stale time_delta produces. Match it (case-insensitive)
        so we know when a time_delta refresh is worth trying."""
        msg = (e.message or "").lower()
        return "application key is invalid" in msg or "invalid application key" in msg

    def _reset_time_delta(self) -> None:
        """Force the ovh.Client to recompute its server-time delta on the next
        call. The SDK computes it lazily once and caches it forever, so any
        clock drift (NTP step, suspend/resume) permanently breaks signatures."""
        if self._client is not None:
            self._client._time_delta = None

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

    def _default_subsidiary(self) -> str:
        """Return the default subsidiary for the configured endpoint.

        Each OVH region only accepts certain subsidiaries:
          ovh-eu → IE (also FR, DE, GB, ES, PL, ...)
          ovh-us → US
          ovh-ca → CA
        """
        if self._endpoint == "ovh-us":
            return "US"
        if self._endpoint == "ovh-ca":
            return "CA"
        return "IE"

    def fetch_catalog(
        self, subsidiary: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        """Fetch the public ECO server catalog for a given OVH subsidiary.

        Results are cached per-subsidiary when `use_cache` is enabled. Pass
        `force=True` to bypass the cache (e.g. for an explicit refresh button).
        The TTL is read from `Settings.cache_ttl` so `OVH_CACHE_TTL` is honoured.
        """
        sub = subsidiary or self._default_subsidiary()
        cache_key = f"catalog_{sub}"
        cache = get_cache(ttl=get_settings().cache_ttl)

        if self._use_cache and not force:
            cached = cache.get(cache_key)
            if cached:
                return cached

        catalog = self.get("/order/catalog/public/eco", ovhSubsidiary=sub)

        if self._use_cache:
            cache.set(cache_key, catalog)

        return catalog

    def get_availability(self, plan_code: str) -> list[dict[str, Any]]:
        """Return the list of currently orderable FQN configurations for a plan.

        OVH only returns configs that are *actually* orderable right now, so
        absence of an FQN from this list means it is out of stock.
        """
        return self.get("/order/eco/availableConfiguration", planCode=plan_code)

    def get_plan_price(self, plan_code: str, subsidiary: str | None = None) -> int | None:
        """Look up the default monthly price (raw integer, in microcents) for a plan.

        OVH stores prices in `plan.pricings[]` where each entry has:
          - mode: 'default' | 'upfront12' | 'upfront24' (we want 'default')
          - interval: 0 (setup) | 1 (monthly) | 12 | 24 (we want 1)
          - intervalUnit: 'month' | 'none'
          - price: integer (divide by 10^8 for currency units)

        Returns None if the plan is not in the catalog or has no monthly
        pricing. Used by the price-tracking + max-price cap.
        """
        catalog = self.fetch_catalog(subsidiary=subsidiary)
        for plan in catalog.get("plans", []):
            if plan.get("planCode") == plan_code:
                for pr in plan.get("pricings", []):
                    if (
                        pr.get("mode") == "default"
                        and pr.get("interval") == 1
                        and pr.get("intervalUnit") == "month"
                        and isinstance(pr.get("price"), int)
                    ):
                        return pr["price"]
        return None

    # ---- Cart lifecycle ----
    #
    # OVH's cart flow is: create → assign → add server → add options →
    # add configuration → checkout. Each step is a separate REST call.
    # The frontend uses the one-shot `/api/checkout/rush` endpoint rather
    # than calling these granular methods directly.

    def create_cart(
        self, description: str = "", ovh_subsidiary: str | None = None
    ) -> dict[str, Any]:
        """Create a new shopping cart. Returns the cart payload including `cartId`.

        `ovh_subsidiary` must match the configured endpoint (US/CA/EU).
        Defaults to `_default_subsidiary()` so callers don't need to know.
        OVH US/CA reject all subsequent cart calls with 404 "Invalid Cart ID"
        if the cart is created without the right subsidiary.
        """
        sub = ovh_subsidiary or self._default_subsidiary()
        return self.post("/order/cart", description=description, ovhSubsidiary=sub)

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
        Uses the item-level path `/order/cart/{cart_id}/item/{item_id}/configuration`
        which is supported by both ovh-eu and ovh-us. The older
        `/order/cart/{cart_id}/eco/configuration` path is EU-only and 404s
        on the US endpoint with "Got an invalid (or empty) URL".
        """
        self.post(
            f"/order/cart/{cart_id}/item/{item_id}/configuration",
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
        period - essential for flash sales where you want the server now.
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
# reuse it. Call `reset_ovh_service()` to force a rebuild after credentials
# are saved/updated (the setup wizard does this).
_ovh_service: OVHService | None = None


def get_ovh_service(use_cache: bool | None = None) -> OVHService:
    """Return the shared OVHService singleton, creating it on first use."""
    global _ovh_service
    if _ovh_service is None:
        _ovh_service = OVHService(use_cache=use_cache)
    return _ovh_service


def reset_ovh_service() -> None:
    """Discard the current OVHService singleton so the next call rebuilds it.

    Called after credentials are saved/updated via the setup wizard so the
    new credentials take effect without a process restart.
    """
    global _ovh_service
    _ovh_service = None
