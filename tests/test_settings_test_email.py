"""Tests for POST /api/settings/notifications/test-email."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import notifier
from app.services.storage import get_storage

XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def smtp_settings():
    storage = get_storage()
    for key, val in {
        "smtp_host": "smtp.example.com",
        "smtp_port": "587",
        "smtp_from": "alerts@example.com",
        "notify_email_to": "me@example.com",
    }.items():
        storage.set_setting(f"notifier_{key}", val)
    return storage


def test_test_email_returns_recipient_on_success(client, smtp_settings, monkeypatch):
    monkeypatch.setattr(notifier, "send_test_email", lambda: "me@example.com")

    r = client.post("/api/settings/notifications/test-email", headers=XHR)

    assert r.status_code == 200
    assert r.json() == {"status": "sent", "recipient": "me@example.com"}


def test_test_email_400_when_not_configured(client):
    """No SMTP settings at all - a client error, and the response must say
    which fields are missing."""
    r = client.post("/api/settings/notifications/test-email", headers=XHR)

    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "SMTP host" in detail
    assert "To address" in detail


def test_test_email_502_when_server_rejects(client, smtp_settings, monkeypatch):
    """An upstream mail failure is not the app's fault - 502, with the
    underlying reason passed through to the UI."""
    def _fail():
        raise notifier.EmailSendError("Authentication failed (535).")

    monkeypatch.setattr(notifier, "send_test_email", _fail)

    r = client.post("/api/settings/notifications/test-email", headers=XHR)

    assert r.status_code == 502
    assert "Authentication failed (535)." in r.json()["detail"]


def test_test_email_blocks_cross_origin_without_xhr(client, smtp_settings, monkeypatch):
    """This endpoint triggers an outbound send, so a third-party page must not
    be able to fire it. A cross-origin Origin without X-Requested-With is the
    forgeable case the CSRF middleware exists to stop."""
    sent = []
    monkeypatch.setattr(notifier, "send_test_email", lambda: sent.append(1) or "me@example.com")

    r = client.post(
        "/api/settings/notifications/test-email",
        headers={"Origin": "https://evil.example"},
    )

    assert r.status_code == 403
    assert sent == []  # blocked before any mail was sent
