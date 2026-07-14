"""Tests for the owned-servers and bills read endpoints."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.ovh_service import get_active_ovh_service

XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def client():
    return TestClient(app)


def _create_account(client, endpoint="ovh-us"):
    r = client.post(
        "/api/accounts",
        json={
            "label": "test", "endpoint": endpoint, "application_key": "ak",
            "application_secret": "secret123", "consumer_key": "ck",
        },
        headers=XHR,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_servers_unconfigured_returns_503(client):
    assert client.get("/api/servers").status_code == 503
    assert client.get("/api/account/bills").status_code == 503


def test_list_servers_enriched(client):
    _create_account(client)
    svc = get_active_ovh_service()
    svc.list_dedicated_servers = MagicMock(return_value=["ns1.example.net"])
    svc.get_dedicated_server = MagicMock(return_value={
        "reverse": "web1.example.com", "datacenter": "vin", "os": "debian12_64",
        "state": "ok", "commercialRange": "KS-LE-1", "ip": "1.2.3.4",
    })
    svc.get_server_service_info = MagicMock(return_value={
        "expiration": "2026-09-01", "renewalType": "automaticV2016",
    })

    r = client.get("/api/servers")
    assert r.status_code == 200
    servers = r.json()["servers"]
    assert servers == [{
        "service_name": "ns1.example.net",
        "display_name": "web1.example.com",
        "datacenter": "vin",
        "os": "debian12_64",
        "state": "ok",
        "commercial_range": "KS-LE-1",
        "ip": "1.2.3.4",
        "expiration": "2026-09-01",
        "renewal_type": "automaticV2016",
    }]


def test_list_servers_degrades_on_detail_failure(client):
    from app.services.ovh_service import OVHServiceError

    _create_account(client)
    svc = get_active_ovh_service()
    svc.list_dedicated_servers = MagicMock(return_value=["a", "b"])

    def _detail(name):
        if name == "a":
            raise OVHServiceError("boom", status_code=500)
        return {"datacenter": "vin", "state": "ok"}

    svc.get_dedicated_server = MagicMock(side_effect=_detail)
    svc.get_server_service_info = MagicMock(return_value={})

    servers = client.get("/api/servers").json()["servers"]
    assert servers[0] == {"service_name": "a"}       # degraded, not fatal
    assert servers[1]["service_name"] == "b"
    assert servers[1]["datacenter"] == "vin"


def test_server_detail(client):
    _create_account(client)
    svc = get_active_ovh_service()
    svc.get_dedicated_server = MagicMock(return_value={"state": "ok", "datacenter": "hil"})
    svc.get_server_service_info = MagicMock(return_value={"expiration": "2026-12-31"})

    r = client.get("/api/servers/ns1.example.net")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["expiration"] == "2026-12-31"
    assert body["detail"]["state"] == "ok"


def test_bills_list_caps_and_reads_defensively(client):
    _create_account(client)
    svc = get_active_ovh_service()
    svc.list_bills = MagicMock(return_value=["B1", "B2"])
    svc.get_bill = MagicMock(side_effect=lambda bid: {
        "billId": bid, "date": "2026-06-01T00:00:00Z",
        "priceWithTax": {"value": 40.0, "text": "$40.00", "currencyCode": "USD"},
        "pdfUrl": f"https://x/{bid}.pdf", "url": f"https://x/{bid}",
    })

    r = client.get("/api/account/bills?limit=1")
    assert r.status_code == 200
    bills = r.json()["bills"]
    # Cap of 1 keeps the most recent (OVH lists newest last).
    assert len(bills) == 1
    assert bills[0]["bill_id"] == "B2"
    assert bills[0]["price_with_tax"] == 40.0
    assert bills[0]["currency_code"] == "USD"
