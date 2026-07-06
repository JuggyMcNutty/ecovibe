"""Tests for the new feature endpoints: profiles, sniper, insights."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_profile_crud(client):
    r = client.post(
        "/api/profiles",
        json={
            "name": "cheap",
            "plan_code": "24sk10",
            "fqn": "24sk10.ram-32g",
            "region": "europe",
            "os": "none_64.en",
            "duration": "P1M",
        },
    )
    assert r.status_code == 201
    profile = r.json()
    pid = profile["id"]
    assert profile["name"] == "cheap"

    r = client.get("/api/profiles")
    assert r.status_code == 200
    assert any(p["id"] == pid for p in r.json())

    r = client.get(f"/api/profiles/{pid}")
    assert r.status_code == 200
    assert r.json()["plan_code"] == "24sk10"

    r = client.put(
        f"/api/profiles/{pid}",
        json={
            "name": "expensive",
            "plan_code": "25sk10",
            "fqn": "25sk10.ram-64g",
            "region": "europe",
            "os": "debian_64",
            "duration": "P12M",
        },
    )
    assert r.status_code == 200
    assert r.json()["name"] == "expensive"

    r = client.delete(f"/api/profiles/{pid}")
    assert r.status_code == 200

    r = client.get(f"/api/profiles/{pid}")
    assert r.status_code == 404


def test_sniper_status_empty(client):
    r = client.get("/api/sniper/status")
    assert r.status_code == 200
    assert r.json()["armed"] == []


def test_sniper_arm_requires_valid_alert(client):
    r = client.post(
        "/api/sniper/arm",
        json={"alert_id": "nonexistent", "profile_id": "also-nope"},
    )
    assert r.status_code == 404


def test_sniper_arm_and_disarm(client):
    alert = client.post("/api/alerts", json={"plan_code": "24sk10"}).json()
    profile = client.post(
        "/api/profiles",
        json={
            "name": "test",
            "plan_code": "24sk10",
            "fqn": "24sk10.ram-32g",
            "region": "europe",
            "os": "none_64.en",
            "duration": "P1M",
        },
    ).json()

    r = client.post(
        "/api/sniper/arm",
        json={"alert_id": alert["id"], "profile_id": profile["id"]},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "armed"

    status = client.get("/api/sniper/status").json()
    assert len(status["armed"]) == 1
    assert status["armed"][0]["profile_id"] == profile["id"]

    r = client.post(f"/api/sniper/disarm/{alert['id']}")
    assert r.status_code == 200

    status = client.get("/api/sniper/status").json()
    assert len(status["armed"]) == 0


def test_assign_profile_to_alert(client):
    alert = client.post("/api/alerts", json={"plan_code": "24sk10"}).json()
    profile = client.post(
        "/api/profiles",
        json={
            "name": "p",
            "plan_code": "24sk10",
            "fqn": "24sk10.ram-32g",
            "region": "europe",
            "os": "none_64.en",
            "duration": "P1M",
        },
    ).json()

    r = client.put(
        f"/api/alerts/{alert['id']}/profile",
        json={"profile_id": profile["id"]},
    )
    assert r.status_code == 200
    assert r.json()["auto_order_profile_id"] == profile["id"]

    # Clear assignment
    r = client.put(
        f"/api/alerts/{alert['id']}/profile",
        json={"profile_id": None},
    )
    assert r.status_code == 200
    assert r.json()["auto_order_profile_id"] is None


def test_insights_endpoints(client):
    r = client.get("/api/insights/history/24sk10")
    assert r.status_code == 200
    assert "events" in r.json()

    r = client.get("/api/insights/patterns/24sk10")
    assert r.status_code == 200
    assert "hourly_counts" in r.json()

    r = client.get("/api/insights/price/24sk10")
    assert r.status_code == 200
    assert "history" in r.json()

    r = client.get("/api/insights/orders")
    assert r.status_code == 200
    assert "orders" in r.json()


def test_rush_order_unconfigured_returns_503(client):
    r = client.post(
        "/api/checkout/rush",
        json={
            "plan_code": "24sk10",
            "fqn": "24sk10.ram-32g",
            "region": "europe",
            "os": "none_64.en",
            "duration": "P1M",
        },
    )
    assert r.status_code == 503
