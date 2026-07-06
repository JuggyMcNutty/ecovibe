"""Tests for OVHService error handling and request construction."""
from unittest.mock import MagicMock, patch

import pytest

from app.services.ovh_service import OVHService, OVHServiceError


def _make_service():
    """Build an OVHService with a mocked client and dummy credentials.

    Credentials are stored in a mock storage (since they now come from the
    database, not env vars).
    """
    fake_creds = {
        "endpoint": "ovh-eu",
        "application_key": "ak",
        "application_secret": "as",
        "consumer_key": "ck",
    }
    with patch("app.services.ovh_service.ovh.Client") as MockClient, \
         patch.object(OVHService, "_load_credentials", staticmethod(lambda: fake_creds)):
        MockClient.return_value = MagicMock()
        svc = OVHService(use_cache=False)
    return svc


def test_not_configured_raises():
    with patch.object(OVHService, "_load_credentials", staticmethod(lambda: None)), \
         patch("app.services.ovh_service.ovh.Client"):
        svc = OVHService(use_cache=False)
        assert not svc.is_configured()
        with pytest.raises(OVHServiceError):
            svc.get("/anything")


def test_apierror_is_caught_and_mapped():
    from ovh.exceptions import ResourceNotFoundError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 404

    def raise_error(path, **kwargs):
        raise ResourceNotFoundError(response=fake_response)

    svc._client.get = raise_error
    with pytest.raises(OVHServiceError) as exc_info:
        svc.get("/order/cart/nope")
    assert exc_info.value.status_code == 404


def test_apierror_carries_query_id():
    from ovh.exceptions import APIError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.headers = {"X-OVH-QUERYID": "abc-123"}

    def raise_error(path, **kwargs):
        raise APIError("boom", response=fake_response)

    svc._client.get = raise_error
    with pytest.raises(OVHServiceError) as exc_info:
        svc.get("/x")
    assert exc_info.value.status_code == 500
    assert exc_info.value.query_id == "abc-123"


def test_create_cart_passes_description():
    svc = _make_service()
    svc._client.post = MagicMock(return_value={"cartId": "C123"})
    result = svc.create_cart(description="my cart")
    assert result == {"cartId": "C123"}
    svc._client.post.assert_called_once_with("/order/cart", description="my cart")


def test_add_server_to_cart_body():
    svc = _make_service()
    svc._client.post = MagicMock(return_value={"itemId": 1})
    svc.add_server_to_cart("C1", "24sk10", duration="P12M", quantity=1)
    args, kwargs = svc._client.post.call_args
    assert args[0] == "/order/cart/C1/eco"
    assert kwargs == {
        "planCode": "24sk10",
        "duration": "P12M",
        "pricingMode": "default",
        "quantity": 1,
    }


def test_checkout_cart_body():
    svc = _make_service()
    svc._client.post = MagicMock(return_value={"orderId": 42})
    svc.checkout_cart("C1", auto_pay=True, waive_retractation=True)
    args, kwargs = svc._client.post.call_args
    assert args[0] == "/order/cart/C1/checkout"
    assert kwargs == {
        "autoPayWithPreferredPaymentMethod": True,
        "waiveRetractationPeriod": True,
    }


def test_get_passes_query_params():
    """GET kwargs should be passed to Client.get() as query string params."""
    svc = _make_service()
    svc._client.get = MagicMock(return_value=[{"fqn": "test"}])
    svc.get("/order/eco/availableConfiguration", planCode="24sk10")
    args, kwargs = svc._client.get.call_args
    assert args[0] == "/order/eco/availableConfiguration"
    assert kwargs == {"planCode": "24sk10"}


def test_delete_uses_client_delete():
    """DELETE should route to Client.delete(), not Client.call()."""
    svc = _make_service()
    svc._client.delete = MagicMock(return_value=None)
    svc.delete("/order/cart/C1")
    svc._client.delete.assert_called_once_with("/order/cart/C1")


def test_put_uses_client_put():
    """PUT should route to Client.put()."""
    svc = _make_service()
    svc._client.put = MagicMock(return_value={"ok": True})
    svc.put("/some/path", field="value")
    svc._client.put.assert_called_once_with("/some/path", field="value")


def test_endpoint_is_stored():
    """The configured endpoint should be accessible via the endpoint property."""
    svc = _make_service()
    assert svc.endpoint == "ovh-eu"
    assert svc.is_configured()


def test_reconfigure_rebuilds_client():
    """reconfigure() should rebuild the client with new credentials."""
    svc = _make_service()
    assert svc.is_configured()
    # Simulate credentials being deleted
    with patch.object(OVHService, "_load_credentials", staticmethod(lambda: None)):
        svc.reconfigure()
    assert not svc.is_configured()
