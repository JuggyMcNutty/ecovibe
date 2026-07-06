"""Tests for the account & billing endpoints."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_get_me_unconfigured(client):
    r = client.get("/api/account/me")
    assert r.status_code == 503


def test_payment_methods_unconfigured(client):
    r = client.get("/api/account/payment-methods")
    assert r.status_code == 503


def test_checkout_defaults_default(client):
    r = client.get("/api/account/checkout-defaults")
    assert r.status_code == 200
    body = r.json()
    assert body["auto_pay"] is False
    assert body["waive_retractation"] is True
    assert body["duration"] == "P1M"


def test_checkout_defaults_save_and_load(client):
    r = client.put("/api/account/checkout-defaults", json={
        "auto_pay": True,
        "waive_retractation": False,
        "duration": "P12M",
        "max_price": 9000000000,
    })
    assert r.status_code == 200

    r = client.get("/api/account/checkout-defaults")
    assert r.status_code == 200
    body = r.json()
    assert body["auto_pay"] is True
    assert body["waive_retractation"] is False
    assert body["duration"] == "P12M"
    assert body["max_price"] == 9000000000
