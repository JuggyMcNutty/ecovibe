"""The stock monitor can be switched off at startup and toggled live.

monitor_enabled is read once by the lifespan (boot state) and applied
immediately by the PUT /api/settings/app hook, so the stored value is both
the startup state and the running state.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.app_settings import APP_SETTINGS, app_setting_bool, get_effective
from app.services.storage import get_storage

XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _body(**overrides):
    """A full PUT body (the endpoint replaces every option) with overrides."""
    body = {key: get_effective(key) for key in APP_SETTINGS}
    body.update(overrides)
    return body


# ----- startup -----


def test_monitor_runs_by_default():
    """Default must stay on: an unconfigured install still watches stock."""
    with TestClient(app) as c:
        assert c.get("/api/monitor/status").json()["running"] is True


def test_monitor_does_not_start_when_disabled():
    """The reported ask: skip the poller entirely at startup."""
    get_storage().set_setting("app_monitor_enabled", "false")

    with TestClient(app) as c:
        assert c.get("/api/monitor/status").json()["running"] is False


def test_rest_of_app_still_works_with_monitor_off():
    """Turning polling off must not take the whole app down."""
    get_storage().set_setting("app_monitor_enabled", "false")

    with TestClient(app) as c:
        assert c.get("/api/health").json()["status"] == "ok"
        assert c.get("/api/monitor/status").status_code == 200
        assert c.get("/api/settings/app").status_code == 200


@pytest.mark.parametrize("raw,expected", [
    ("false", False), ("0", False), ("no", False), ("off", False),
    ("true", True), ("1", True), ("yes", True), ("on", True),
])
def test_setting_parses_truthy_forms(raw, expected):
    """Stored via the UI as true/false, but env fallback accepts the wider
    set that app_setting_bool documents."""
    get_storage().set_setting("app_monitor_enabled", raw)

    assert app_setting_bool("monitor_enabled") is expected


# ----- live toggle -----


def test_toggling_off_stops_the_poller(client):
    assert client.get("/api/monitor/status").json()["running"] is True

    r = client.put("/api/settings/app", json=_body(monitor_enabled=False), headers=XHR)

    assert r.status_code == 200
    assert "stock monitor stopped" in r.json()["applied"]
    assert client.get("/api/monitor/status").json()["running"] is False


def test_toggling_back_on_restarts_the_poller(client):
    """Turning it off must not be a dead end that needs a restart to undo."""
    client.put("/api/settings/app", json=_body(monitor_enabled=False), headers=XHR)
    assert client.get("/api/monitor/status").json()["running"] is False

    r = client.put("/api/settings/app", json=_body(monitor_enabled=True), headers=XHR)

    assert "stock monitor started" in r.json()["applied"]
    assert client.get("/api/monitor/status").json()["running"] is True


def test_unchanged_value_does_not_fire_the_hook(client):
    """Saving other options must not bounce the poller."""
    r = client.put("/api/settings/app", json=_body(cache_ttl=1234), headers=XHR)

    applied = r.json()["applied"]
    assert not any("monitor" in a for a in applied)
    assert client.get("/api/monitor/status").json()["running"] is True


def test_toggle_persists_to_db(client):
    """The stored value is what the next startup reads."""
    client.put("/api/settings/app", json=_body(monitor_enabled=False), headers=XHR)

    assert get_storage().get_setting("app_monitor_enabled") == "false"
    assert app_setting_bool("monitor_enabled") is False
    assert client.get("/api/settings/app").json()["settings"]["monitor_enabled"] is False


def test_rejects_non_boolean(client):
    r = client.put("/api/settings/app", json=_body(monitor_enabled="maybe"), headers=XHR)

    assert r.status_code == 422
