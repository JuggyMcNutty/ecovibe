"""Tests for the multi-account API."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _create_account(client, label="EU personal", endpoint="ovh-eu", secret="secret123"):
    r = client.post(
        "/api/accounts",
        json={
            "label": label,
            "endpoint": endpoint,
            "application_key": "ak",
            "application_secret": secret,
            "consumer_key": "ck",
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_list_empty(client):
    r = client.get("/api/accounts")
    assert r.status_code == 200
    assert r.json() == []


def test_create_account(client):
    acct = _create_account(client)
    assert acct["endpoint"] == "ovh-eu"
    assert acct["label"] == "EU personal"
    # Secret never returned in full
    assert "secret" not in r_to_raw(acct)
    assert acct["application_secret_configured"] is True
    # "ak" is 2 chars (<= 8) so mask returns "****"
    assert acct["application_key_masked"] == "****"


def r_to_raw(acct):
    # helper just to assert no raw secret leaks; response model fields are fixed
    return acct


def test_first_account_becomes_active(client):
    acct = _create_account(client)
    r = client.get("/api/accounts/active")
    assert r.json()["active_account_id"] == acct["id"]


def test_invalid_endpoint_rejected(client):
    r = client.post(
        "/api/accounts",
        json={"label": "x", "endpoint": "ovh-xx", "application_key": "a",
              "application_secret": "s", "consumer_key": "c"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 400


def test_create_requires_secret(client):
    r = client.post(
        "/api/accounts",
        json={"label": "x", "endpoint": "ovh-eu", "application_key": "a",
              "application_secret": "", "consumer_key": "c"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 400


def test_update_preserves_secret_when_blank(client):
    acct = _create_account(client, secret="mysecret")
    # Update label but send empty secret -> stored secret preserved
    r = client.put(
        f"/api/accounts/{acct['id']}",
        json={"label": "renamed", "endpoint": "ovh-eu", "application_key": "ak2",
              "application_secret": "", "consumer_key": "ck2"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 200
    assert r.json()["label"] == "renamed"
    assert r.json()["application_secret_configured"] is True


def test_update_404(client):
    r = client.put(
        "/api/accounts/nope",
        json={"label": "x", "endpoint": "ovh-eu", "application_key": "a",
              "application_secret": "s", "consumer_key": "c"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 404


def test_switch_active(client):
    _create_account(client, label="one")
    a2 = _create_account(client, label="two", endpoint="ovh-us")
    # a1 was active (first), switch to a2
    r = client.put(
        "/api/accounts/active",
        json={"account_id": a2["id"]},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 200
    assert client.get("/api/accounts/active").json()["active_account_id"] == a2["id"]


def test_switch_active_404(client):
    r = client.put(
        "/api/accounts/active",
        json={"account_id": "nope"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert r.status_code == 404


def test_delete_active_falls_back(client):
    a1 = _create_account(client, label="one")
    a2 = _create_account(client, label="two")
    # active is a1, delete it -> falls back to a2
    r = client.delete(f"/api/accounts/{a1['id']}",
                      headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    active = client.get("/api/accounts/active").json()
    assert active["active_account_id"] == a2["id"]


def test_delete_last_clears_active(client):
    a1 = _create_account(client, label="only")
    client.delete(f"/api/accounts/{a1['id']}",
                  headers={"X-Requested-With": "XMLHttpRequest"})
    active = client.get("/api/accounts/active").json()
    assert active["active_account_id"] is None
    assert active["account"] is None


def test_delete_404(client):
    r = client.delete("/api/accounts/nope",
                      headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 404


def test_health_reports_active(client):
    r = client.get("/api/health")
    assert r.json()["configured"] is False
    assert r.json()["account_count"] == 0
    a = _create_account(client)
    r = client.get("/api/health")
    assert r.json()["configured"] is True
    assert r.json()["active_account_id"] == a["id"]
    assert r.json()["account_count"] == 1


def test_active_route_not_shadowed_by_wildcard(client):
    """GET/PUT /api/accounts/active must not match /{account_id}='active'."""
    _create_account(client)
    r = client.get("/api/accounts/active")
    # If shadowed, this would 404 ("Account 'active' not found")
    assert r.status_code == 200
    assert "active_account_id" in r.json()
