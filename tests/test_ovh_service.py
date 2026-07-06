"""Tests for OVHService error handling and request construction."""
import os
from unittest.mock import MagicMock, patch

import pytest

from app.services.ovh_service import OVHService, OVHServiceError


def _make_service():
    """Build an OVHService with a mocked client and dummy credentials."""
    with patch.dict(os.environ, {
        "OVH_APPLICATION_KEY": "ak",
        "OVH_APPLICATION_SECRET": "as",
        "OVH_CONSUMER_KEY": "ck",
        "OVH_ENDPOINT": "ovh-eu",
    }):
        from app.config import get_settings
        get_settings.cache_clear()
        with patch("app.services.ovh_service.ovh.Client") as MockClient:
            MockClient.return_value = MagicMock()
            svc = OVHService(use_cache=False)
    return svc


def test_not_configured_raises():
    with patch("app.services.ovh_service.ovh.Client"):
        svc = OVHService(use_cache=False)
        assert not svc.is_configured()
        with pytest.raises(OVHServiceError):
            svc.get("/anything")


def test_apierror_is_caught_and_mapped():
    from ovh.exceptions import ResourceNotFoundError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 404

    def raise_error(method, path, **kwargs):
        raise ResourceNotFoundError(response=fake_response)

    svc._client.call = raise_error
    with pytest.raises(OVHServiceError) as exc_info:
        svc.get("/order/cart/nope")
    assert exc_info.value.status_code == 404


def test_apierror_carries_query_id():
    from ovh.exceptions import APIError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.headers = {"X-OVH-QUERYID": "abc-123"}

    def raise_error(method, path, **kwargs):
        raise APIError("boom", response=fake_response)

    svc._client.call = raise_error
    with pytest.raises(OVHServiceError) as exc_info:
        svc.get("/x")
    assert exc_info.value.status_code == 500
    assert exc_info.value.query_id == "abc-123"


def test_create_cart_passes_description():
    svc = _make_service()
    svc._client.call = MagicMock(return_value={"cartId": "C123"})
    result = svc.create_cart(description="my cart")
    assert result == {"cartId": "C123"}
    svc._client.call.assert_called_once_with("POST", "/order/cart", description="my cart")


def test_add_server_to_cart_body():
    svc = _make_service()
    svc._client.call = MagicMock(return_value={"itemId": 1})
    svc.add_server_to_cart("C1", "24sk10", duration="P12M", quantity=1)
    args, kwargs = svc._client.call.call_args
    assert args[0] == "POST"
    assert args[1] == "/order/cart/C1/eco"
    assert kwargs == {
        "planCode": "24sk10",
        "duration": "P12M",
        "pricingMode": "default",
        "quantity": 1,
    }


def test_checkout_cart_body():
    svc = _make_service()
    svc._client.call = MagicMock(return_value={"orderId": 42})
    svc.checkout_cart("C1", auto_pay=True, waive_retractation=True)
    args, kwargs = svc._client.call.call_args
    assert kwargs == {
        "autoPayWithPreferredPaymentMethod": True,
        "waiveRetractationPeriod": True,
    }
