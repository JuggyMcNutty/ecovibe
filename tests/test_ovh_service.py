"""Tests for OVHService error handling and request construction."""
from unittest.mock import MagicMock, patch

import pytest

from app.services.ovh_service import OVHService, OVHServiceError


def _make_service(endpoint="ovh-eu"):
    """Build an OVHService with a mocked client and dummy credentials.

    Credentials are passed directly to the constructor (decoupled from
    storage), and the ovh.Client is mocked so no network call is made.
    """
    with patch("app.services.ovh_service.ovh.Client") as MockClient:
        MockClient.return_value = MagicMock()
        svc = OVHService(
            endpoint=endpoint,
            application_key="ak",
            application_secret="as",
            consumer_key="ck",
            use_cache=False,
        )
    return svc


def test_not_configured_raises():
    with patch("app.services.ovh_service.ovh.Client"):
        svc = OVHService("ovh-eu", "", "", "", use_cache=False)
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
    svc._client.post.assert_called_once_with(
        "/order/cart", description="my cart", ovhSubsidiary="IE"
    )


def test_create_cart_us_endpoint_passes_us_subsidiary():
    """OVH US requires ovhSubsidiary=US at cart creation or all
    subsequent cart calls return 404 'Invalid Cart ID'."""
    with patch("app.services.ovh_service.ovh.Client") as MockClient:
        MockClient.return_value = MagicMock()
        svc = OVHService(
            endpoint="ovh-us",
            application_key="ak",
            application_secret="as",
            consumer_key="ck",
            use_cache=False,
        )
    svc._client.post = MagicMock(return_value={"cartId": "C-US"})
    svc.create_cart(description="rush")
    args, kwargs = svc._client.post.call_args
    assert kwargs["ovhSubsidiary"] == "US"


def test_create_cart_ca_endpoint_passes_ca_subsidiary():
    """OVH CA accepts only the CA subsidiary; any other (US/IE/WORLD/...) is
    rejected with HTTP 400 'invalid ovhSubsidiary'. Verified live: the public
    catalog endpoint returns 200 for CA and 400 for WORLD/US/FR/IE."""
    with patch("app.services.ovh_service.ovh.Client") as MockClient:
        MockClient.return_value = MagicMock()
        svc = OVHService(
            endpoint="ovh-ca",
            application_key="ak",
            application_secret="as",
            consumer_key="ck",
            use_cache=False,
        )
    svc._client.post = MagicMock(return_value={"cartId": "C-CA"})
    svc.create_cart(description="rush")
    args, kwargs = svc._client.post.call_args
    assert kwargs["ovhSubsidiary"] == "CA"


def test_valid_subsidiaries_per_endpoint():
    """valid_subsidiaries() returns the accepted set for each endpoint and
    a safe default (IE) for unknown endpoints."""
    assert _make_service("ovh-eu").valid_subsidiaries() == {
        "IE", "FR", "DE", "GB", "ES", "PL", "IT", "PT", "CZ", "FI"
    }
    assert _make_service("ovh-us").valid_subsidiaries() == {"US"}
    assert _make_service("ovh-ca").valid_subsidiaries() == {"CA"}
    assert _make_service("ovh-xx").valid_subsidiaries() == {"IE"}


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


def test_add_configuration_uses_item_level_path():
    """Configuration must use /item/{itemId}/configuration (works on EU and US),
    not /eco/configuration (EU-only, 404s on US with 'invalid URL')."""
    svc = _make_service()
    svc._client.post = MagicMock(return_value={"id": 1})
    svc.add_configuration_to_cart("C1", 42, "dedicated_datacenter", "hil")
    args, kwargs = svc._client.post.call_args
    assert args[0] == "/order/cart/C1/item/42/configuration"
    assert kwargs == {"label": "dedicated_datacenter", "value": "hil"}
    # itemId must NOT be in the body — it's in the URL path now.
    assert "itemId" not in kwargs


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
    # Simulate credentials being deleted by reconfiguring with empties
    with patch("app.services.ovh_service.ovh.Client"):
        svc.reconfigure("ovh-eu", "", "", "")
    assert not svc.is_configured()


def test_403_invalid_key_refreshes_time_delta_and_retries():
    """A 403 'This application key is invalid' should reset the cached
    server-time delta and retry once - OVH reports a signature mismatch
    (caused by a stale timestamp) as an invalid application key."""
    from ovh.exceptions import APIError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 403
    fake_response.headers = {}

    calls = []

    def side_effect(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise APIError("This application key is invalid", response=fake_response)
        return {"ok": True}

    svc._client.get = side_effect
    svc._client._time_delta = 1234

    result = svc.get("/me")
    assert result == {"ok": True}
    assert len(calls) == 2
    # The cached time delta must have been cleared so the SDK recomputes it.
    assert svc._client._time_delta is None


def test_403_invalid_key_does_not_retry_indefinitely():
    """If the retry also fails with the same error, surface it - no loop."""
    from ovh.exceptions import APIError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 403
    fake_response.headers = {}

    calls = []

    def side_effect(path, **kwargs):
        calls.append(path)
        raise APIError("This application key is invalid", response=fake_response)

    svc._client.get = side_effect
    with pytest.raises(OVHServiceError) as exc_info:
        svc.get("/me")
    assert exc_info.value.status_code == 403
    # Exactly two attempts: original + one retry.
    assert len(calls) == 2


def test_403_other_error_does_not_retry():
    """A 403 that isn't the stale-signature message should not trigger a retry."""
    from ovh.exceptions import APIError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 403
    fake_response.headers = {}

    calls = []

    def side_effect(path, **kwargs):
        calls.append(path)
        raise APIError("This call is not granted", response=fake_response)

    svc._client.get = side_effect
    with pytest.raises(OVHServiceError) as exc_info:
        svc.get("/me")
    assert exc_info.value.status_code == 403
    assert len(calls) == 1


def test_calls_are_serialised_by_lock():
    """Concurrent _call invocations must not overlap: the lock serialises
    access to the shared ovh.Client so the SDK's requests.Session and
    cached time_delta are never used from two threads at once."""
    import threading

    svc = _make_service()
    svc._client.get = MagicMock(return_value={"ok": True})

    in_call = []
    max_concurrent = [0]
    current = [0]
    lock = threading.Lock()
    barrier = threading.Event()

    def counting_get(path, **kwargs):
        with lock:
            current[0] += 1
            max_concurrent[0] = max(max_concurrent[0], current[0])
            in_call.append(threading.current_thread().name)
        # Hold long enough that a second thread would overlap if unserialised.
        barrier.wait(timeout=2)
        with lock:
            current[0] -= 1
        return {"ok": True}

    svc._client.get = counting_get

    results = []
    threads = []
    for i in range(5):

        def worker(idx=i):
            results.append(svc.get(f"/p{idx}"))

        t = threading.Thread(target=worker, name=f"worker-{i}")
        threads.append(t)
        t.start()

    # Let all threads start, then release them together.
    import time

    time.sleep(0.1)
    barrier.set()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 5
    assert max_concurrent[0] == 1


def test_500_retries_once():
    """A 500 from OVH should be retried once (transient server error)."""
    from ovh.exceptions import APIError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.headers = {}

    calls = []

    def side_effect(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise APIError("Internal server error", response=fake_response)
        return {"ok": True}

    svc._client.get = side_effect
    result = svc.get("/me")
    assert result == {"ok": True}
    assert len(calls) == 2


def test_500_does_not_retry_indefinitely():
    """If the 500 persists, surface it after one retry."""
    from ovh.exceptions import APIError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.headers = {}

    calls = []

    def side_effect(path, **kwargs):
        calls.append(path)
        raise APIError("Internal server error", response=fake_response)

    svc._client.get = side_effect
    with pytest.raises(OVHServiceError) as exc_info:
        svc.get("/me")
    assert exc_info.value.status_code == 500
    assert len(calls) == 2


def test_post_is_not_retried_on_500():
    """POST must never retry on 5xx: OVH may have processed the request
    (e.g. a checkout) before the response was lost, and a replay would
    duplicate the order."""
    from ovh.exceptions import APIError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.headers = {}

    calls = []

    def side_effect(path, **kwargs):
        calls.append(path)
        raise APIError("Internal server error", response=fake_response)

    svc._client.post = side_effect
    with pytest.raises(OVHServiceError) as exc_info:
        svc.post("/order/cart/x/checkout")
    assert exc_info.value.status_code == 500
    assert len(calls) == 1


def test_post_is_retried_on_stale_signature_403():
    """POST IS retried on a stale-signature 403: the rejection happens at
    the auth layer before OVH processes the request, so a replay cannot
    duplicate an order — and without it the sniper's rush order would stay
    broken after clock drift until an unrelated GET healed the delta."""
    from ovh.exceptions import APIError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 403
    fake_response.headers = {}

    calls = []

    def side_effect(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise APIError("This application key is invalid", response=fake_response)
        return {"orderId": 1}

    svc._client.post = side_effect
    svc._client._time_delta = 1234

    result = svc.post("/order/cart/x/checkout")
    assert result == {"orderId": 1}
    assert len(calls) == 2
    assert svc._client._time_delta is None


def test_400_does_not_retry():
    """Only 500-599 should retry, not client errors like 400."""
    from ovh.exceptions import APIError

    svc = _make_service()
    fake_response = MagicMock()
    fake_response.status_code = 400
    fake_response.headers = {}

    calls = []

    def side_effect(path, **kwargs):
        calls.append(path)
        raise APIError("Bad request", response=fake_response)

    svc._client.get = side_effect
    with pytest.raises(OVHServiceError) as exc_info:
        svc.get("/me")
    assert exc_info.value.status_code == 400
    assert len(calls) == 1


def test_get_plan_datacenters():
    """get_plan_datacenters should extract DC values from the catalog's
    dedicated_datacenter configuration entry."""
    svc = _make_service()
    fake_catalog = {
        "plans": [
            {
                "planCode": "24sys012-v1-us",
                "configurations": [
                    {"name": "dedicated_datacenter", "values": ["hil", "vin"]},
                    {"name": "region", "values": ["united_states"]},
                ],
            }
        ]
    }
    svc.fetch_catalog = MagicMock(return_value=fake_catalog)
    dcs = svc.get_plan_datacenters("24sys012-v1-us")
    assert dcs == ["hil", "vin"]


def test_get_plan_datacenters_returns_empty_for_unknown_plan():
    svc = _make_service()
    svc.fetch_catalog = MagicMock(return_value={"plans": []})
    assert svc.get_plan_datacenters("nope") == []


def test_get_stock_returns_raw_entries():
    """get_stock should return the raw availability entries from OVH,
    including per-DC availability status for each RAM+storage combo."""
    svc = _make_service()
    fake_avail = [
        {
            "fqn": "24sys012-v1-us.ram-32g-ecc-2666.softraid-2x4000sa",
            "planCode": "24sys012-v1-us",
            "memory": "ram-32g-ecc-2666",
            "storage": "softraid-2x4000sa",
            "datacenters": [
                {"availability": "unavailable", "datacenter": "hil"},
                {"availability": "unavailable", "datacenter": "vin"},
            ],
        },
        {
            "fqn": "24sys012-v1-us.ram-32g-ecc-2666.softraid-2x1920nvme",
            "planCode": "24sys012-v1-us",
            "memory": "ram-32g-ecc-2666",
            "storage": "softraid-2x1920nvme",
            "datacenters": [
                {"availability": "unavailable", "datacenter": "hil"},
                {"availability": "1H-low", "datacenter": "vin"},
            ],
        },
    ]
    svc._client.get = MagicMock(return_value=fake_avail)
    result = svc.get_stock("24sys012-v1-us")
    assert len(result) == 2
    assert result[0]["memory"] == "ram-32g-ecc-2666"
    assert result[0]["storage"] == "softraid-2x4000sa"
    assert result[1]["datacenters"][1]["availability"] == "1H-low"


def test_get_stock_returns_empty_for_unknown_plan():
    svc = _make_service()
    svc._client.get = MagicMock(return_value=[])
    assert svc.get_stock("nope") == []


def test_get_stock_without_plan_code_omits_filter():
    """get_stock(None) must call the availabilities endpoint with NO
    planCode kwarg — OVH then returns the entire region's stock, which
    the monitor uses for batch polling and the region ticker."""
    svc = _make_service()
    svc._client.get = MagicMock(return_value=[])
    svc.get_stock()
    svc._client.get.assert_called_once_with(
        "/dedicated/server/datacenter/availabilities"
    )


def test_orderable_entry_rule():
    """orderable_entry: in stock iff any DC availability is outside
    {unavailable, comingSoon} — comingSoon is NOT orderable."""
    from app.services.ovh_service import orderable_entry

    assert orderable_entry({"datacenters": [
        {"availability": "unavailable", "datacenter": "hil"},
        {"availability": "1H-low", "datacenter": "vin"},
    ]})
    assert not orderable_entry({"datacenters": [
        {"availability": "unavailable", "datacenter": "hil"},
        {"availability": "comingSoon", "datacenter": "vin"},
    ]})
    assert not orderable_entry({"datacenters": []})
    assert not orderable_entry({})
