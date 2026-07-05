import logging
from typing import Any, Dict, List, Optional

import ovh
from ovh.exceptions import APIError

from app.config import get_settings
from app.services.cache import get_cache

logger = logging.getLogger(__name__)


class OVHServiceError(Exception):
    """Raised on any OVH API failure. Carries upstream status code when available."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        query_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.query_id = query_id


class OVHService:
    def __init__(self, use_cache: Optional[bool] = None):
        settings = get_settings()
        self._use_cache = settings.use_cache if use_cache is None else use_cache
        self._client: Optional[ovh.Client] = None
        self._setup_client()

    def _setup_client(self) -> None:
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
        return self._client is not None

    def _call(self, method: str, path: str, **kwargs) -> Any:
        if not self._client:
            raise OVHServiceError("OVH API not configured. Please set credentials.")
        try:
            return self._client.call(method, path, **kwargs)
        except APIError as e:
            status = None
            if e.response is not None:
                status = getattr(e.response, "status_code", None)
            raise OVHServiceError(str(e), status_code=status, query_id=e.query_id) from e

    def get(self, path: str, **kwargs) -> Any:
        return self._call("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        return self._call("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> Any:
        return self._call("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> Any:
        return self._call("DELETE", path, **kwargs)

    def fetch_catalog(self, subsidiary: str = "IE", force: bool = False) -> Dict[str, Any]:
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

    def get_availability(self, plan_code: str) -> List[Dict[str, Any]]:
        return self.get("/order/eco/availableConfiguration", planCode=plan_code)

    def create_cart(self, description: str = "") -> Dict[str, Any]:
        return self.post("/order/cart", description=description)

    def assign_cart(self, cart_id: str) -> None:
        self.post(f"/order/cart/{cart_id}/assign")

    def add_server_to_cart(
        self, cart_id: str, plan_code: str, duration: str = "P1M", quantity: int = 1
    ) -> Dict[str, Any]:
        return self.post(
            f"/order/cart/{cart_id}/eco",
            planCode=plan_code,
            duration=duration,
            pricingMode="default",
            quantity=quantity,
        )

    def add_option_to_cart(
        self, cart_id: str, item_id: int, plan_code: str, duration: str = "P1M"
    ) -> Dict[str, Any]:
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
        self.post(
            f"/order/cart/{cart_id}/eco/configuration",
            itemId=item_id,
            label=label,
            value=value,
        )

    def get_cart(self, cart_id: str) -> Dict[str, Any]:
        return self.get(f"/order/cart/{cart_id}")

    def get_cart_summary(self, cart_id: str) -> Dict[str, Any]:
        return self.get(f"/order/cart/{cart_id}/summary")

    def checkout_cart(
        self, cart_id: str, auto_pay: bool = False, waive_retractation: bool = False
    ) -> Dict[str, Any]:
        return self.post(
            f"/order/cart/{cart_id}/checkout",
            autoPayWithPreferredPaymentMethod=auto_pay,
            waiveRetractationPeriod=waive_retractation,
        )

    def delete_cart(self, cart_id: str) -> None:
        self.delete(f"/order/cart/{cart_id}")


_ovh_service: Optional[OVHService] = None


def get_ovh_service(use_cache: Optional[bool] = None) -> OVHService:
    global _ovh_service
    if _ovh_service is None:
        _ovh_service = OVHService(use_cache=use_cache)
    return _ovh_service
