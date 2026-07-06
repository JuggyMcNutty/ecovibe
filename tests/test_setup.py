"""Tests for the setup wizard endpoints."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_get_credentials_empty(client):
    """With no credentials stored, GET returns configured=false."""
    r = client.get("/api/setup/credentials")
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_save_and_get_credentials(client):
    """POST saves credentials, GET returns masked versions."""
    r = client.post("/api/setup/credentials", json={
        "endpoint": "ovh-eu",
        "application_key": "pcLPvgyGXgOsjFQU",
        "application_secret": "AsmWpxea54SgVU7D7tAg4QvkrCsgz6hp",
        "consumer_key": "yZT6MAhCd6XUJ5o2sQBF2rIJiCPjRxwg",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["endpoint"] == "ovh-eu"
    # Keys should be masked, not raw
    assert "pcLP" in body["application_key_masked"]
    assert "jFQU" in body["application_key_masked"]
    assert "AsmW" not in body["application_key_masked"]  # secret should not appear

    # GET should now show configured
    r = client.get("/api/setup/credentials")
    assert r.status_code == 200
    assert r.json()["configured"] is True


def test_save_credentials_validation(client):
    """Invalid endpoint should be rejected."""
    r = client.post("/api/setup/credentials", json={
        "endpoint": "ovh-xx",
        "application_key": "ak",
        "application_secret": "as",
        "consumer_key": "ck",
    })
    assert r.status_code == 400

    # Missing fields
    r = client.post("/api/setup/credentials", json={
        "endpoint": "ovh-eu",
        "application_key": "",
        "application_secret": "as",
        "consumer_key": "ck",
    })
    assert r.status_code == 400


def test_delete_credentials(client):
    """DELETE removes stored credentials."""
    client.post("/api/setup/credentials", json={
        "endpoint": "ovh-eu",
        "application_key": "ak",
        "application_secret": "as",
        "consumer_key": "ck",
    })
    assert client.get("/api/setup/credentials").json()["configured"] is True

    r = client.delete("/api/setup/credentials")
    assert r.status_code == 200
    assert client.get("/api/setup/credentials").json()["configured"] is False


def test_test_credentials_unconfigured(client):
    """POST /setup/test returns 503 when not configured."""
    r = client.post("/api/setup/test")
    assert r.status_code == 503


def test_health_reflects_credentials(client):
    """GET /api/health should report configured=false before, true after."""
    r = client.get("/api/health")
    assert r.json()["configured"] is False

    client.post("/api/setup/credentials", json={
        "endpoint": "ovh-eu",
        "application_key": "ak",
        "application_secret": "as",
        "consumer_key": "ck",
    })

    r = client.get("/api/health")
    assert r.json()["configured"] is True
    assert r.json()["endpoint"] == "ovh-eu"
