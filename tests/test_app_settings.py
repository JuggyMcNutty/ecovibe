"""Tests for the app_settings registry: DB-first reads, validation, API."""
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.app_settings import (
    APP_SETTINGS,
    app_setting_bool,
    app_setting_int,
    app_setting_str,
    get_effective,
    validate_value,
)
from app.services.storage import get_storage

XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def client():
    return TestClient(app)


# ----- typed readers -----

def test_env_fallback_when_db_empty(client, monkeypatch):
    monkeypatch.setenv("OVH_PRICE_CHECK_INTERVAL", "120")
    from app.config import get_settings
    get_settings.cache_clear()
    assert app_setting_int("price_check_interval") == 120
    get_settings.cache_clear()


def test_db_override_beats_env(client, monkeypatch):
    monkeypatch.setenv("OVH_PRICE_CHECK_INTERVAL", "120")
    from app.config import get_settings
    get_settings.cache_clear()
    get_storage().set_setting("app_price_check_interval", "600")
    assert app_setting_int("price_check_interval") == 600
    get_settings.cache_clear()


def test_bool_parsing_variants(client):
    storage = get_storage()
    for raw, expected in [("1", True), ("true", True), ("YES", True),
                          ("on", True), ("0", False), ("false", False),
                          ("off", False), ("nope", False)]:
        storage.set_setting("app_use_cache", raw)
        assert app_setting_bool("use_cache") is expected, raw


def test_corrupt_int_falls_back(client):
    get_storage().set_setting("app_cache_ttl", "abc")
    assert app_setting_int("cache_ttl") == 300  # env default


def test_ui_pref_default_without_env(client):
    # ui_* keys are not env-backed: fallback is the registry default.
    assert app_setting_int("ui_orders_days") == 90
    get_storage().set_setting("app_ui_orders_days", "14")
    assert app_setting_int("ui_orders_days") == 14


def test_str_reader_and_effective(client):
    assert app_setting_str("log_level") == "INFO"
    get_storage().set_setting("app_log_level", "DEBUG")
    assert get_effective("log_level") == "DEBUG"
    assert isinstance(get_effective("use_cache"), bool)


# ----- validation -----

def test_validate_boundaries():
    assert validate_value("cache_ttl", 10) == 10
    assert validate_value("cache_ttl", 86_400) == 86_400
    with pytest.raises(ValueError):
        validate_value("cache_ttl", 9)
    with pytest.raises(ValueError):
        validate_value("cache_ttl", 86_401)


def test_validate_allow_zero():
    assert validate_value("price_check_interval", 0) == 0
    assert validate_value("price_check_interval", 60) == 60
    with pytest.raises(ValueError):
        validate_value("price_check_interval", 30)  # 1-59 invalid
    assert validate_value("ui_alert_autohide_ms", 0) == 0


def test_validate_choices_normalises():
    assert validate_value("log_level", "debug") == "DEBUG"
    with pytest.raises(ValueError):
        validate_value("log_level", "TRACE")


def test_validate_type_errors():
    with pytest.raises(ValueError):
        validate_value("cache_ttl", "300")  # str where int expected
    with pytest.raises(ValueError):
        validate_value("use_cache", 1)      # int where bool expected
    with pytest.raises(ValueError):
        validate_value("cache_ttl", True)   # bool is not an int here


def test_registry_covers_expected_keys():
    assert set(APP_SETTINGS) == {
        "monitor_enabled",
        "price_check_interval", "stock_event_retention_days",
        "stock_event_max_rows", "catalog_watch_enabled", "catalog_watch_notify",
        "use_cache", "cache_ttl", "log_level",
        "log_file_max_bytes", "log_backup_count", "log_buffer_size",
        "ui_alert_autohide_ms", "ui_orders_days", "ui_orders_limit",
        "ui_logs_limit", "ui_region_feed_cap", "ui_recent_alerts_shown",
    }


# ----- DB-first read sites: cache -----

def test_ovh_service_use_cache_from_db(client):
    from unittest.mock import MagicMock, patch

    from app.services.ovh_service import OVHService

    get_storage().set_setting("app_use_cache", "true")
    with patch("app.services.ovh_service.ovh.Client") as MockClient:
        MockClient.return_value = MagicMock()
        svc = OVHService("ovh-eu", "ak", "as", "ck")
    assert svc._use_cache is True
    # Explicit constructor argument still wins (test seam).
    with patch("app.services.ovh_service.ovh.Client") as MockClient:
        MockClient.return_value = MagicMock()
        svc = OVHService("ovh-eu", "ak", "as", "ck", use_cache=False)
    assert svc._use_cache is False


def test_fetch_catalog_uses_db_cache_ttl(client):
    from unittest.mock import MagicMock, patch

    import app.services.cache as cache_mod
    from app.services.ovh_service import OVHService

    cache_mod._cache = None
    get_storage().set_setting("app_cache_ttl", "77")
    with patch("app.services.ovh_service.ovh.Client") as MockClient:
        MockClient.return_value = MagicMock()
        svc = OVHService("ovh-eu", "ak", "as", "ck", use_cache=True)
    svc._client.get = MagicMock(return_value={"plans": []})
    svc.fetch_catalog()
    assert cache_mod.get_cache()._ttl == 77
    cache_mod._cache = None


# ----- DB-first read sites: logging -----

def test_logbus_resize_keeps_newest(client):
    from collections import deque

    from app.services.logbus import LogBus

    bus = LogBus(maxlen=10)
    for i in range(10):
        bus._buffer.append({"n": i})
    bus.resize(3)
    assert bus._buffer.maxlen == 3
    assert [e["n"] for e in bus._buffer] == [7, 8, 9]  # newest kept
    bus.resize(20)
    assert bus._buffer.maxlen == 20
    assert len(bus._buffer) == 3
    assert isinstance(bus._buffer, deque)


def test_setup_logging_applies_db_log_level(client):
    import logging

    from app.logging_config import setup_logging

    get_storage().set_setting("app_log_level", "DEBUG")
    setup_logging()
    app_logger = logging.getLogger("app")
    assert app_logger.level == logging.DEBUG
    tagged = [h for h in app_logger.handlers if getattr(h, "_ecovibe_log_handler", False)]
    assert tagged and all(h.level == logging.DEBUG for h in tagged)

    # And back — proves the re-run applies changes both ways.
    get_storage().set_setting("app_log_level", "WARNING")
    setup_logging()
    assert logging.getLogger("app").level == logging.WARNING


# ----- DB-first read sites: monitor -----

@pytest.mark.asyncio
async def test_prune_uses_db_retention_settings(client, monkeypatch):
    from unittest.mock import MagicMock

    from app.services.monitor import MonitorService

    storage = get_storage()
    storage.set_setting("app_stock_event_retention_days", "7")
    storage.set_setting("app_stock_event_max_rows", "2000")

    monitor = MonitorService()
    # Must be relative to time.monotonic(), which is time since BOOT: a
    # literal 0.0 only clears the hourly guard on a host that happens to
    # have been up for over an hour, so this failed on a freshly booted
    # machine.
    monitor._last_prune = time.monotonic() - 3601
    spy = MagicMock(return_value=0)
    monkeypatch.setattr(storage, "prune_stock_events", spy)

    await monitor._maybe_prune_events()
    spy.assert_called_once_with(7, 2000)


@pytest.mark.asyncio
async def test_price_check_disabled_via_db(client, monkeypatch):
    from unittest.mock import MagicMock

    from app.services.monitor import MonitorService

    get_storage().set_setting("app_price_check_interval", "0")
    monitor = MonitorService()
    monitor._last_price_check = 0.0
    spy = MagicMock()
    monkeypatch.setattr(monitor, "_check_prices_and_promos", spy)

    await monitor._maybe_check_prices_and_promos()
    spy.assert_not_called()


# ----- GET/PUT /api/settings/app -----

def _default_body():
    return {key: get_effective(key) for key in APP_SETTINGS}


def test_get_app_settings_shape(client):
    r = client.get("/api/settings/app")
    assert r.status_code == 200
    body = r.json()
    assert set(body["settings"]) == set(APP_SETTINGS)
    assert body["settings"]["cache_ttl"] == 300
    assert body["settings"]["use_cache"] is False
    assert body["settings"]["ui_orders_days"] == 90
    # Read-only env card.
    assert set(body["env"]) == {"host", "port", "db_path", "log_file", "cors_origins"}
    assert body["env"]["host"] == "127.0.0.1"


def test_put_app_settings_round_trip(client):
    body = _default_body()
    body["price_check_interval"] = 300
    body["ui_orders_days"] = 30
    r = client.put("/api/settings/app", json=body, headers=XHR)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "saved"
    got = client.get("/api/settings/app").json()["settings"]
    assert got["price_check_interval"] == 300
    assert got["ui_orders_days"] == 30
    # Values survive: they're in the DB, not just the lru cache.
    assert get_storage().get_setting("app_price_check_interval") == "300"


def test_put_app_settings_round_trips_catalog_watch(client):
    """The two catalog-watch switches are independent: muting the channels
    must not also stop the tracking that feeds the Insights panel."""
    body = _default_body()
    assert body["catalog_watch_enabled"] is True  # on by default
    body["catalog_watch_notify"] = False
    r = client.put("/api/settings/app", json=body, headers=XHR)
    assert r.status_code == 200, r.text
    got = client.get("/api/settings/app").json()["settings"]
    assert got["catalog_watch_enabled"] is True
    assert got["catalog_watch_notify"] is False
    assert get_storage().get_setting("app_catalog_watch_notify") == "false"


def test_put_app_settings_invalid_writes_nothing(client):
    body = _default_body()
    body["cache_ttl"] = 5           # below min 10
    body["ui_orders_days"] = 30     # valid — must NOT be persisted either
    r = client.put("/api/settings/app", json=body, headers=XHR)
    assert r.status_code == 422
    assert "cache_ttl" in r.json()["detail"]
    assert get_storage().get_setting("app_ui_orders_days") is None


def test_put_app_settings_hooks_fire_only_on_change(client, monkeypatch):
    from unittest.mock import MagicMock

    import app.api.settings as settings_api  # noqa: F401  (hook targets are imported lazily)

    reset_spy = MagicMock()
    setup_spy = MagicMock()
    monkeypatch.setattr("app.services.ovh_service.reset_ovh_service", reset_spy)
    monkeypatch.setattr("app.logging_config.setup_logging", setup_spy)

    # No-change PUT: no hooks.
    body = _default_body()
    r = client.put("/api/settings/app", json=body, headers=XHR)
    assert r.status_code == 200
    assert r.json()["applied"] == []
    reset_spy.assert_not_called()
    setup_spy.assert_not_called()

    # Cache change → registry reset; log level change → setup_logging;
    # buffer change → LogBus resized.
    from app.services.logbus import get_log_bus
    body["use_cache"] = True
    body["log_level"] = "DEBUG"
    body["log_buffer_size"] = 250
    r = client.put("/api/settings/app", json=body, headers=XHR)
    assert r.status_code == 200
    applied = r.json()["applied"]
    assert len(applied) == 3
    reset_spy.assert_called_once_with(None)
    setup_spy.assert_called_once()
    assert get_log_bus()._buffer.maxlen == 250
