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

# Datacenter availability values that are NOT orderable right now. Everything
# else (1H-low, 1H-high, 24H, 72H, ...) counts as in stock. Mirrors the
# catalog's OOS rule in static/js/app.js.
_NOT_ORDERABLE = {"unavailable", "comingSoon"}


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
    """Wraps an ovh.Client built directly from supplied credentials.

    Construction is decoupled from storage: callers (the registry or
    tests) pass the credentials + endpoint in. Call ``reconfigure()`` to
    rebuild the client after a credential update.
    """

    def __init__(
        self,
        endpoint: str,
        application_key: str,
        application_secret: str,
        consumer_key: str,
        account_id: str | None = None,
        use_cache: bool | None = None,
    ) -> None:
        settings = get_settings()
        self._use_cache = settings.use_cache if use_cache is None else use_cache
        self._endpoint: str = endpoint or settings.endpoint
        self._account_id = account_id
        # Retain credentials so the client can be rebuilt without an external
        # reconfigure() call (used by _reset_time_delta fallback).
        self._app_key = application_key
        self._app_secret = application_secret
        self._consumer_key = consumer_key
        # Serialises all ovh.Client calls. The shared ovh.Client bundles a
        # requests.Session and a lazily-cached server-time delta that are
        # NOT safe under concurrent access (the monitor poller, rush orders
        # and account endpoints all call in via asyncio.to_thread). Without
        # the lock, concurrent requests.Session.prepare_request/send can
        # race on the shared cookie jar, and a stale time_delta makes every
        # signature wrong so OVH replies "This application key is invalid".
        self._lock = threading.Lock()
        self._client: ovh.Client | None = None
        self._build_client(application_key, application_secret, consumer_key)

    def _build_client(
        self, application_key: str, application_secret: str, consumer_key: str
    ) -> None:
        """Construct the ovh.Client, or leave it None if creds are incomplete."""
        missing = [
            name
            for name, val in (
                ("application_key", application_key),
                ("application_secret", application_secret),
                ("consumer_key", consumer_key),
            )
            if not val
        ]
        if missing:
            if application_key or application_secret or consumer_key:
                logger.warning(
                    "OVH client not initialised (account=%s): missing: %s",
                    self._account_id, ", ".join(missing),
                )
            return
        self._client = ovh.Client(
            endpoint=self._endpoint,
            application_key=application_key,
            application_secret=application_secret,
            consumer_key=consumer_key,
        )
        logger.info("OVH client ready for endpoint: %s (account=%s)",
                    self._endpoint, self._account_id)

    @property
    def account_id(self) -> str | None:
        return self._account_id

    def reconfigure(
        self,
        endpoint: str,
        application_key: str,
        application_secret: str,
        consumer_key: str,
    ) -> None:
        """Rebuild the OVH client after credentials have been saved/updated."""
        with self._lock:
            self._client = None
            self._endpoint = endpoint or self._endpoint
            self._app_key = application_key
            self._app_secret = application_secret
            self._consumer_key = consumer_key
            self._build_client(application_key, application_secret, consumer_key)

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

        Two retry strategies (both single-retry, not looping):

        - 403 "This application key is invalid": refresh the client's cached
          time delta and retry. OVH reports a signature mismatch (caused by
          a stale timestamp) as an invalid application key, and the SDK
          caches the delta forever so it never self-corrects.

        - 500/502/503/504: transient OVH server errors. Retry once after a
          short backoff. OVH's API occasionally returns 500s that succeed
          on immediate retry.
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
                if e.status_code in (500, 502, 503, 504) and method.upper() != "POST":
                    # Only retry idempotent methods. Retrying POST (e.g.
                    # checkout) can duplicate an order if OVH already
                    # processed it but the response was lost.
                    logger.warning(
                        "OVH %s transient error - retrying once (query_id=%s)",
                        e.status_code, e.query_id,
                    )
                    import time
                    time.sleep(0.5)
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
            # DEBUG, not WARNING: many callers handle OVH failures gracefully
            # (e.g. the poller's per-plan availability check), so a blanket
            # warning here would spam the log every cycle. Notable failures are
            # logged by the caller (a failed order) or the retry paths above.
            logger.debug(
                "OVH %s %s failed (status=%s query_id=%s): %s",
                method.upper(), path, status, e.query_id, e,
            )
            raise OVHServiceError(str(e), status_code=status, query_id=e.query_id) from e
        except Exception as e:
            logger.debug("OVH %s %s failed: %s", method.upper(), path, e)
            raise OVHServiceError(str(e), status_code=None, query_id=None) from e

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
        clock drift (NTP step, suspend/resume) permanently breaks signatures.

        The SDK exposes no public API to invalidate the cached delta, so we
        access the private ``_time_delta`` attribute. If a future SDK version
        removes or renames it, fall back to a full client reconstruction
        (which drops the HTTP connection pool but is guaranteed to work).
        """
        if self._client is not None:
            if hasattr(self._client, "_time_delta"):
                self._client._time_delta = None
            else:
                logger.warning(
                    "ovh.Client has no _time_delta attr — rebuilding client "
                    "as fallback (account=%s)", self._account_id,
                )
                self._client = None
                self._build_client(self._app_key, self._app_secret, self._consumer_key)

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

    # Subsidiaries accepted by each endpoint. ovh-us and ovh-ca each accept
    # exactly one; ovh-eu accepts the European country subsidiaries. Sending
    # a subsidiary not in this set to an endpoint is rejected with HTTP 400
    # "invalid ovhSubsidiary" (verified live: ca.api.ovh.com accepts only CA;
    # WORLD/US/FR/IE all 400).
    _VALID_SUBSIDIARIES: dict[str, set[str]] = {
        "ovh-eu": {"IE", "FR", "DE", "GB", "ES", "PL", "IT", "PT", "CZ", "FI"},
        "ovh-us": {"US"},
        "ovh-ca": {"CA"},
    }

    def valid_subsidiaries(self) -> set[str]:
        """Return the subsidiary codes accepted by this endpoint."""
        return self._VALID_SUBSIDIARIES.get(self._endpoint, {"IE"})

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

    # Subsidiary → ISO 4217 currency code. OVH's catalog prices are denominated
    # in the subsidiary's currency; used to label prices/error messages.
    _SUBSIDIARY_CURRENCY = {
        "IE": "EUR", "FR": "EUR", "DE": "EUR", "ES": "EUR", "PL": "EUR",
        "IT": "EUR", "PT": "EUR", "CZ": "EUR", "FI": "EUR",
        "GB": "GBP",
        "US": "USD",
        "CA": "CAD",
    }

    def default_currency_code(self, subsidiary: str | None = None) -> str:
        """Return the ISO currency code for a subsidiary (default: the endpoint's)."""
        sub = subsidiary or self._default_subsidiary()
        return self._SUBSIDIARY_CURRENCY.get(sub, "EUR")

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

        Derived from the datacenter-availabilities endpoint (``get_stock``),
        which works on every region. The older ``/order/eco/availableConfiguration``
        path 404s on ovh-us and ovh-ca ("Got an invalid (or empty) URL"), so
        relying on it silently broke stock detection (and the sniper) on those
        regions. A config counts as orderable when at least one datacenter
        reports an availability other than ``unavailable``/``comingSoon`` —
        the same rule the catalog OOS badge uses (see static/js/app.js). The
        returned dicts keep the ``fqn`` key every caller reads, plus the richer
        ``memory``/``storage``/``datacenters`` fields.
        """
        out: list[dict[str, Any]] = []
        for entry in self.get_stock(plan_code):
            dcs = entry.get("datacenters") or []
            if any(dc.get("availability") not in _NOT_ORDERABLE for dc in dcs):
                out.append({
                    "fqn": entry.get("fqn", ""),
                    "memory": entry.get("memory"),
                    "storage": entry.get("storage"),
                    "datacenters": dcs,
                })
        return out

    def get_stock(self, plan_code: str) -> list[dict[str, Any]]:
        """Return live stock levels per RAM+storage combo for a plan.

        Queries ``/dedicated/server/datacenter/availabilities`` and returns
        the raw entries. Each entry has ``fqn``, ``memory``, ``storage``,
        and ``datacenters: [{datacenter, availability}]`` where
        ``availability`` is ``'unavailable'`` or a freshness tag like
        ``'1H-low'``, ``'24H'``, etc.

        The frontend uses this to show which configs are in stock before
        the user attempts an order - OVH returns a confusing 500 at
        checkout if the selected combo is out of stock.
        """
        return self.get(
            "/dedicated/server/datacenter/availabilities", planCode=plan_code
        )

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

    def get_plan_datacenters(self, plan_code: str, subsidiary: str | None = None) -> list[str]:
        """Return the list of datacenters where a plan is orderable.

        Extracted from the catalog's ``configurations`` array for the plan,
        looking for the ``dedicated_datacenter`` entry. Used as a fallback
        when the rush order has no DCs selected - OVH requires
        ``dedicated_datacenter`` to be set before checkout or it returns
        400 "in customFields there must be a field with name
        'dedicated_datacenter'".
        """
        catalog = self.fetch_catalog(subsidiary=subsidiary)
        for plan in catalog.get("plans", []):
            if plan.get("planCode") == plan_code:
                for cfg in plan.get("configurations", []):
                    if cfg.get("name") == "dedicated_datacenter":
                        return cfg.get("values", [])
        return []

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

    # ---- Orders ----

    def list_orders(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> list[int]:
        """Return all order IDs for the account, optionally date-filtered.

        OVH returns a list of longs (order IDs). Use ``date.from``/``date.to``
        (ISO 8601) to limit the window.
        """
        kwargs: dict[str, Any] = {}
        if date_from:
            kwargs["date.from"] = date_from
        if date_to:
            kwargs["date.to"] = date_to
        return self.get("/me/order", **kwargs)

    def get_order(self, order_id: int) -> dict[str, Any]:
        """Return the full order object (price, pdfUrl, dates, url)."""
        return self.get(f"/me/order/{order_id}")

    def get_order_status(self, order_id: int) -> str:
        """Return the order status string (e.g. 'delivering', 'delivered')."""
        return self.get(f"/me/order/{order_id}/status")

    def get_order_details(self, order_id: int) -> list[dict[str, Any]]:
        """Return line-item details for an order (server, installation, licenses)."""
        detail_ids = self.get(f"/me/order/{order_id}/details")
        details = []
        for did in detail_ids:
            detail = self.get(f"/me/order/{order_id}/details/{did}")
            details.append(detail)
        return details

    def get_order_followup(self, order_id: int) -> list[dict[str, Any]]:
        """Return the delivery follow-up timeline for an order."""
        return self.get(f"/me/order/{order_id}/followUp")

    def waive_order_retraction(self, order_id: int) -> None:
        """Waive the legal retraction period to speed up delivery."""
        self.post(f"/me/order/{order_id}/waiveRetraction")

    def cancel_order(self, order_id: int, reason: str = "other") -> None:
        """Cancel an order by exercising the right of retraction (withdrawal).

        OVH requires a ``reason`` from the RetractionReasonEnum:
        competitor, difficulty, expensive, other, performance,
        reliability, unused. Defaults to ``other``. Only available during
        the retraction period (before ``retractionDate``).
        """
        self.post(f"/me/order/{order_id}/retraction", reason=reason)


# ----- per-account service registry -----
#
# Replaces the old module singleton. Each account id maps to a cached
# OVHService (with its own ovh.Client + threading.Lock). A module-level
# lock guards the registry dict itself; each service's own lock guards
# its shared ovh.Client.
#
# ``get_ovh_service(account_id=None)`` resolves the active account when
# no id is given, so legacy callers that ignore account_id keep working
# during the multi-account rollout.

_services: dict[str, OVHService] = {}
_registry_lock = threading.Lock()
_unconfigured: OVHService | None = None


def _build_for_account(account_id: str) -> OVHService | None:
    """Construct an OVHService from the stored account, or None if missing."""
    from app.services.storage import get_storage
    acct = get_storage().get_account(account_id)
    if acct is None:
        return None
    return OVHService(
        endpoint=acct["endpoint"],
        application_key=acct["application_key"],
        application_secret=acct["application_secret"],
        consumer_key=acct["consumer_key"],
        account_id=acct["id"],
    )


def _unconfigured_service() -> OVHService:
    """Return a shared not-configured service (no client)."""
    global _unconfigured
    if _unconfigured is None:
        _unconfigured = OVHService(get_settings().endpoint, "", "", "")
    return _unconfigured


def get_ovh_service(account_id: str | None = None) -> OVHService:
    """Return the OVHService for an account.

    If ``account_id`` is None, the active account is used (legacy-friendly).
    Returns a not-configured service (``is_configured() is False``) when
    there is no active account or the account has been deleted.
    """
    if account_id is None:
        from app.services.storage import get_storage
        account_id = get_storage().get_active_account_id()
    if account_id is None:
        return _unconfigured_service()
    with _registry_lock:
        svc = _services.get(account_id)
        if svc is None:
            svc = _build_for_account(account_id)
            if svc is None:
                return _unconfigured_service()
            _services[account_id] = svc
        return svc


def get_active_ovh_service() -> OVHService:
    """Return the OVHService for the currently active account."""
    return get_ovh_service(None)


def reset_ovh_service(account_id: str | None = None) -> None:
    """Drop a cached service so the next call rebuilds it from storage.

    With no argument, clears the whole registry (e.g. after a credential
    change whose account id is unknown to the caller). Safe to call when
    nothing is cached.
    """
    with _registry_lock:
        if account_id is None:
            _services.clear()
        else:
            _services.pop(account_id, None)
        global _unconfigured
        _unconfigured = None


def reset_all_services() -> None:
    """Clear the entire registry (used by tests)."""
    reset_ovh_service(None)
