"""Tests for the new feature endpoints: profiles, sniper, insights."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.monitor import get_sniper_service
from app.services.ovh_service import OVHServiceError, get_active_ovh_service


@pytest.fixture
def client():
    return TestClient(app)


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
    )
    assert r.status_code == 201, r.text
    return r.json()


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


def test_assign_profile_requires_valid_profile(client):
    """A bogus profile_id must 404 immediately, not silently attach and
    only surface as an opaque sniper error much later."""
    alert = client.post("/api/alerts", json={"plan_code": "24sk10"}).json()
    r = client.put(
        f"/api/alerts/{alert['id']}/profile",
        json={"profile_id": "does-not-exist"},
    )
    assert r.status_code == 404


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


def _mock_rush(monkeypatch, return_value):
    """Replace _execute_rush_order with a spying stub.

    Returns a dict tracking call_count and the last request it saw.
    """
    calls = {"count": 0, "last": None}

    async def _fake_execute(service, req):
        calls["count"] += 1
        calls["last"] = req
        return return_value

    monkeypatch.setattr("app.api.checkout._execute_rush_order", _fake_execute)
    return calls


def test_rush_arm_if_oos_arms_when_out_of_stock(client, monkeypatch):
    """When the requested FQN is not orderable, arm_if_oos arms the sniper
    instead of firing a doomed order that OVH rejects."""
    _create_account(client)
    svc = get_active_ovh_service()
    svc.get_availability = MagicMock(return_value=[{"fqn": "some-other-fqn"}])
    calls = _mock_rush(monkeypatch, {"orderId": 999, "url": "http://x"})

    r = client.post(
        "/api/checkout/rush",
        json={
            "plan_code": "24sk10",
            "fqn": "24sk10.ram-32g",
            "region": "europe",
            "os": "none_64.en",
            "duration": "P1M",
            "arm_if_oos": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "armed"
    assert body["plan_code"] == "24sk10"
    assert body["fqn"] == "24sk10.ram-32g"
    assert "alert_id" in body and "profile_id" in body

    # The order must NOT have fired.
    assert calls["count"] == 0

    # Sniper should report one armed alert.
    status = client.get("/api/sniper/status").json()
    assert len(status["armed"]) == 1
    assert status["armed"][0]["alert_id"] == body["alert_id"]


def test_rush_arm_if_oos_fires_when_in_stock(client, monkeypatch):
    """When the FQN IS orderable, arm_if_oos lets the rush proceed normally."""
    _create_account(client)
    svc = get_active_ovh_service()
    svc.get_availability = MagicMock(
        return_value=[{"fqn": "24sk10.ram-32g"}, {"fqn": "other"}]
    )
    calls = _mock_rush(monkeypatch, {"orderId": 123, "url": "http://o"})

    r = client.post(
        "/api/checkout/rush",
        json={
            "plan_code": "24sk10",
            "fqn": "24sk10.ram-32g",
            "region": "europe",
            "os": "none_64.en",
            "duration": "P1M",
            "arm_if_oos": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["orderId"] == 123
    assert calls["count"] == 1

    # Sniper should NOT be armed.
    status = client.get("/api/sniper/status").json()
    assert len(status["armed"]) == 0


def test_rush_arm_if_oos_reuses_existing_alert(client, monkeypatch):
    """Re-arming for the same plan+FQN reuses the existing alert (no dupes)."""
    _create_account(client)
    svc = get_active_ovh_service()
    svc.get_availability = MagicMock(return_value=[{"fqn": "nope"}])
    _mock_rush(monkeypatch, {"orderId": 1})

    body1 = client.post(
        "/api/checkout/rush",
        json={
            "plan_code": "25sk10",
            "fqn": "25sk10.ram-64g",
            "region": "europe",
            "os": "none_64.en",
            "duration": "P1M",
            "arm_if_oos": True,
        },
    ).json()

    body2 = client.post(
        "/api/checkout/rush",
        json={
            "plan_code": "25sk10",
            "fqn": "25sk10.ram-64g",
            "region": "europe",
            "os": "none_64.en",
            "duration": "P1M",
            "arm_if_oos": True,
        },
    ).json()

    # Same alert reused; profile is a fresh one each arm.
    assert body1["alert_id"] == body2["alert_id"]
    assert body1["profile_id"] != body2["profile_id"]

    alerts = client.get("/api/alerts").json()
    matching = [a for a in alerts if a["plan_code"] == "25sk10" and a["fqn_pattern"] == "25sk10.ram-64g"]
    assert len(matching) == 1


# ----- max_price budget guard -----
#
# The guard used to live only in the rush_checkout route handler, so the
# sniper (which calls _execute_rush_order directly) could auto-order at any
# price. It now lives inside _execute_rush_order itself so every caller -
# manual rush order or sniper - enforces it identically.

def _rush_body(max_price=None, **overrides):
    body = {
        "plan_code": "24sk10",
        "fqn": "24sk10.ram-32g",
        "region": "europe",
        "os": "none_64.en",
        "duration": "P1M",
    }
    if max_price is not None:
        body["max_price"] = max_price
    body.update(overrides)
    return body


def test_rush_max_price_blocks_when_price_exceeds_cap(client):
    """The manual rush route refuses to order when price > max_price."""
    _create_account(client)
    svc = get_active_ovh_service()
    svc.get_plan_price = MagicMock(return_value=9_900_000_000)  # 99.00 in the account's currency
    svc.create_cart = MagicMock(side_effect=AssertionError("cart must not be created over budget"))

    r = client.post("/api/checkout/rush", json=_rush_body(max_price=5_000_000_000))
    assert r.status_code == 409
    assert svc.create_cart.call_count == 0


def test_rush_max_price_fails_closed_on_price_lookup_error(client):
    """If the price can't be verified, the order must be refused, not let
    through unchecked - a cap that can't be confirmed is not a cap."""
    _create_account(client)
    svc = get_active_ovh_service()
    svc.get_plan_price = MagicMock(side_effect=OVHServiceError("upstream flaked", status_code=500))
    svc.create_cart = MagicMock(side_effect=AssertionError("cart must not be created when price is unverifiable"))

    r = client.post("/api/checkout/rush", json=_rush_body(max_price=5_000_000_000))
    assert r.status_code == 409
    assert svc.create_cart.call_count == 0


def test_rush_max_price_allows_when_price_under_cap(client, monkeypatch):
    """Sanity check: the guard doesn't block legitimate orders under cap."""
    _create_account(client)
    svc = get_active_ovh_service()
    svc.get_plan_price = MagicMock(return_value=1_000_000_000)
    calls = _mock_rush(monkeypatch, {"orderId": 42, "url": "http://x"})

    r = client.post("/api/checkout/rush", json=_rush_body(max_price=5_000_000_000))
    assert r.status_code == 200
    assert calls["count"] == 1


async def test_sniper_fire_enforces_max_price(client):
    """Regression test for the sniper bypass: arm a profile with a max_price
    below the current (mocked) price and fire the sniper directly through
    SniperService._fire (the same path the background poller uses) -
    confirm no cart is ever created and the result records the refusal."""
    account = _create_account(client)
    profile = client.post(
        "/api/profiles",
        json={
            "name": "capped",
            "plan_code": "24sk10",
            "fqn": "24sk10.ram-32g",
            "region": "europe",
            "os": "none_64.en",
            "duration": "P1M",
            "max_price": 5_000_000_000,
        },
    ).json()
    alert = client.post("/api/alerts", json={"plan_code": "24sk10"}).json()

    svc = get_active_ovh_service()
    svc.get_plan_price = MagicMock(return_value=9_900_000_000)
    svc.create_cart = MagicMock(side_effect=AssertionError("cart must not be created over budget"))

    sniper = get_sniper_service()
    sniper.arm(alert["id"], profile["id"])
    await sniper._fire(
        alert["id"], "24sk10", "24sk10.ram-32g", profile["id"],
        account_id=account["id"],
    )

    assert svc.create_cart.call_count == 0
    result = sniper.status()["results"][alert["id"]]
    assert result["status"] == "error"
    assert "exceeds" in result["message"] or "max" in result["message"].lower()
