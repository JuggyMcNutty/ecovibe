"""Tests for CSRF protection middleware and localhost-default bind."""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def _client():
    return TestClient(app)


def test_get_allowed_without_header():
    """GET /api/health must always pass — no CSRF check on safe methods."""
    r = _client().get("/api/health")
    assert r.status_code == 200


def test_post_allowed_with_xhr_header():
    """POST with X-Requested-With header is allowed (SPA pattern)."""
    c = _client()
    r = c.post(
        "/api/setup/credentials",
        json={
            "endpoint": "ovh-eu",
            "application_key": "ak",
            "application_secret": "as",
            "consumer_key": "ck",
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    # Either 200 (saved) or 400 (validation) — both mean middleware allowed it.
    assert r.status_code != 403


def test_post_blocked_cross_origin():
    """POST with a cross-origin Origin header and no X-Requested-With → 403."""
    c = _client()
    r = c.post(
        "/api/setup/credentials",
        json={
            "endpoint": "ovh-eu",
            "application_key": "ak",
            "application_secret": "as",
            "consumer_key": "ck",
        },
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert "Cross-origin" in r.json()["detail"]


def test_post_blocked_cross_origin_referer():
    """POST with a cross-origin Referer and no X-Requested-With → 403."""
    c = _client()
    r = c.post(
        "/api/setup/credentials",
        json={
            "endpoint": "ovh-eu",
            "application_key": "ak",
            "application_secret": "as",
            "consumer_key": "ck",
        },
        headers={"Referer": "https://evil.example/"},
    )
    assert r.status_code == 403


def test_post_allowed_same_origin():
    """POST with same-origin Origin header is allowed even without XHR header."""
    c = _client()
    r = c.post(
        "/api/setup/credentials",
        json={
            "endpoint": "ovh-eu",
            "application_key": "ak",
            "application_secret": "as",
            "consumer_key": "ck",
        },
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code != 403


def test_post_allowed_no_origin_no_referer():
    """POST with neither Origin nor Referer is allowed (non-browser clients).

    The Starlette TestClient sends no Origin/Referer by default, so the
    existing test suite passes without modification.
    """
    c = _client()
    r = c.post(
        "/api/setup/credentials",
        json={
            "endpoint": "ovh-eu",
            "application_key": "ak",
            "application_secret": "as",
            "consumer_key": "ck",
        },
    )
    assert r.status_code != 403


def test_delete_blocked_cross_origin():
    """DELETE with cross-origin Origin and no X-Requested-With → 403."""
    c = _client()
    # Seed credentials so the delete target exists.
    c.post(
        "/api/setup/credentials",
        json={
            "endpoint": "ovh-eu",
            "application_key": "ak",
            "application_secret": "as",
            "consumer_key": "ck",
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    r = c.delete(
        "/api/setup/credentials",
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_put_blocked_cross_origin():
    """PUT with cross-origin Origin and no X-Requested-With → 403."""
    c = _client()
    r = c.put(
        "/api/settings/notifications",
        json={"telegram_bot_token": "xxx"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_xhr_header_overrides_cross_origin():
    """X-Requested-With allows the request even with a cross-origin Origin.

    CORS preflight (empty allow_origins by default) blocks the actual
    cross-origin fetch from reaching the server; the X-Requested-With
    check is the in-app backstop.
    """
    c = _client()
    r = c.post(
        "/api/setup/credentials",
        json={
            "endpoint": "ovh-eu",
            "application_key": "ak",
            "application_secret": "as",
            "consumer_key": "ck",
        },
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://evil.example",
        },
    )
    assert r.status_code != 403


def test_non_api_path_not_checked():
    """POST to a non-/api/ path (e.g. root) is not CSRF-checked."""
    c = _client()
    # Root only accepts GET; 405 is expected, not 403.
    r = c.post("/", headers={"Origin": "https://evil.example"})
    assert r.status_code != 403


def test_default_host_is_localhost():
    """Default bind is 127.0.0.1 so `python run.py` is never public."""
    s = get_settings()
    assert s.host == "127.0.0.1"
    assert s.port == 8000
