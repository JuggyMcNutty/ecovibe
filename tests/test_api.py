"""Smoke tests for the FastAPI app endpoints."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "configured" in body
    assert "endpoint" in body


def test_root_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "OVH Flash Sale Monitor" in r.text


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
