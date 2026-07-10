"""Smoke tests for the FastAPI app endpoints."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.ovh_service import get_active_ovh_service


@pytest.fixture
def client():
    return TestClient(app)


XHR = {"X-Requested-With": "XMLHttpRequest"}


def _create_account(client, label="EU personal", endpoint="ovh-eu"):
    r = client.post(
        "/api/accounts",
        json={
            "label": label,
            "endpoint": endpoint,
            "application_key": "ak",
            "application_secret": "secret123",
            "consumer_key": "ck",
        },
        headers=XHR,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "configured" in body
    assert "endpoint" in body


def test_root_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "ECOVibe" in r.text


def test_catalog_unconfigured_returns_503(client):
    r = client.get("/api/catalog")
    assert r.status_code == 503


def test_cart_unconfigured_returns_503(client):
    r = client.post("/api/cart", json={"description": "x"})
    assert r.status_code == 503


def test_checkout_unconfigured_returns_503(client):
    r = client.post("/api/checkout/nope", json={"auto_pay": False, "waive_retractation": False})
    assert r.status_code == 503


def test_monitor_status(client):
    r = client.get("/api/monitor/status")
    assert r.status_code == 200
    body = r.json()
    assert "poll_interval" in body
    assert "alerts_count" in body
    assert "monitored_plans" in body


def test_set_poll_interval(client):
    r = client.put("/api/monitor/poll-interval", json={"poll_interval": 5})
    assert r.status_code == 200
    assert r.json()["poll_interval"] == 5


def test_set_poll_interval_validation(client):
    r = client.put("/api/monitor/poll-interval", json={"poll_interval": 99})
    assert r.status_code == 422
    r = client.put("/api/monitor/poll-interval", json={"poll_interval": 0})
    assert r.status_code == 422


def test_alert_crud(client):
    r = client.post("/api/alerts", json={"plan_code": "24sk10", "fqn_pattern": "*"})
    assert r.status_code == 201
    alert_id = r.json()["id"]

    r = client.get("/api/alerts")
    assert r.status_code == 200
    assert len(r.json()) >= 1

    r = client.get(f"/api/alerts/{alert_id}")
    assert r.status_code == 200
    assert r.json()["plan_code"] == "24sk10"

    r = client.put(f"/api/alerts/{alert_id}/disable")
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    r = client.put(f"/api/alerts/{alert_id}/enable")
    assert r.status_code == 200
    assert r.json()["enabled"] is True

    r = client.delete(f"/api/alerts/{alert_id}")
    assert r.status_code == 200

    r = client.get(f"/api/alerts/{alert_id}")
    assert r.status_code == 404


def test_alert_duplicate_returns_409(client):
    client.post("/api/alerts", json={"plan_code": "25sk10", "fqn_pattern": "*"})
    r = client.post("/api/alerts", json={"plan_code": "25sk10", "fqn_pattern": "*"})
    assert r.status_code == 409


def test_delete_missing_alert_404(client):
    r = client.delete("/api/alerts/nonexistent-uuid")
    assert r.status_code == 404


def _mock_active_catalog(service):
    service.fetch_catalog = MagicMock(return_value={"plans": [], "addons": [], "products": []})


def _mock_active_catalog_with_locale(service, *, addons, locale_cc):
    """Mock fetch_catalog to return a catalog with a locale and addons.

    Mirrors the ovh-ca shape: pricing entries carry NO currencyCode, and the
    native currency lives only in the top-level `locale.currencyCode`.
    """
    service.fetch_catalog = MagicMock(return_value={
        "plans": [],
        "addons": addons,
        "products": [],
        "locale": {"currencyCode": locale_cc, "subsidiary": "CA", "taxRate": 0},
    })


def test_catalog_subsidiary_falls_back_when_invalid_for_endpoint(client):
    """A foreign subsidiary must fall back to the endpoint's default rather
    than be forwarded to OVH (which would 400 'invalid ovhSubsidiary'). A
    CA-world account billed in USD would otherwise send ?country=US to
    ca.api.ovh.com."""
    _create_account(client, label="CA", endpoint="ovh-ca")
    svc = get_active_ovh_service()
    _mock_active_catalog(svc)

    r = client.get("/api/catalog/plans?country=US")
    assert r.status_code == 200
    svc.fetch_catalog.assert_called_once()
    assert svc.fetch_catalog.call_args.kwargs["subsidiary"] == "CA"


def test_catalog_subsidiary_passes_valid_value_through(client):
    """A subsidiary that is valid for the active endpoint is forwarded as-is
    (no fallback)."""
    _create_account(client, label="EU", endpoint="ovh-eu")
    svc = get_active_ovh_service()
    _mock_active_catalog(svc)

    r = client.get("/api/catalog/plans?country=FR")
    assert r.status_code == 200
    assert svc.fetch_catalog.call_args.kwargs["subsidiary"] == "FR"


def test_catalog_subsidiary_defaults_when_no_country(client):
    """Omitting country resolves to the endpoint's default subsidiary."""
    _create_account(client, label="CA", endpoint="ovh-ca")
    svc = get_active_ovh_service()
    _mock_active_catalog(svc)

    r = client.get("/api/catalog/plans")
    assert r.status_code == 200
    assert svc.fetch_catalog.call_args.kwargs["subsidiary"] == "CA"


def test_catalog_currency_propagated_from_locale_when_pricings_omit_it(client):
    """ovh-ca leaves currencyCode null on pricing entries and exposes the
    native currency only via the catalog's top-level locale. The backend
    must propagate it so the frontend can FX-convert prices for display
    (without this, prices would be mislabelled as EUR and mis-converted)."""
    _create_account(client, label="CA", endpoint="ovh-ca")
    svc = get_active_ovh_service()
    _mock_active_catalog_with_locale(
        svc,
        addons=[{
            "planCode": "ram-16g",
            "invoiceName": "16GB ECC",
            "pricings": [
                {"mode": "default", "interval": 1, "intervalUnit": "month",
                 "price": 500000000, "formattedPrice": "$5 CAD", "currencyCode": None},
            ],
        }],
        locale_cc="CAD",
    )

    r = client.get("/api/catalog/plans?country=CA")
    assert r.status_code == 200
    body = r.json()
    assert body["currencyCode"] == "CAD"
    assert body["addonPrices"]["ram-16g"]["currencyCode"] == "CAD"


def test_catalog_currency_uses_pricing_currency_code_when_present(client):
    """When OVH does populate currencyCode on pricings (e.g. ovh-eu), that
    value is used and locale is only a fallback."""
    _create_account(client, label="EU", endpoint="ovh-eu")
    svc = get_active_ovh_service()
    _mock_active_catalog_with_locale(
        svc,
        addons=[{
            "planCode": "ram-16g",
            "invoiceName": "16GB ECC",
            "pricings": [
                {"mode": "default", "interval": 1, "intervalUnit": "month",
                 "price": 500000000, "formattedPrice": "5.00€", "currencyCode": "EUR"},
            ],
        }],
        locale_cc="EUR",
    )

    r = client.get("/api/catalog/plans?country=IE")
    assert r.status_code == 200
    assert r.json()["addonPrices"]["ram-16g"]["currencyCode"] == "EUR"
