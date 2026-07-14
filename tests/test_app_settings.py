"""Tests for the app_settings registry: DB-first reads, validation, API."""
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
        "price_check_interval", "stock_event_retention_days",
        "stock_event_max_rows", "use_cache", "cache_ttl", "log_level",
        "log_file_max_bytes", "log_backup_count", "log_buffer_size",
        "ui_alert_autohide_ms", "ui_orders_days", "ui_orders_limit",
        "ui_logs_limit", "ui_region_feed_cap", "ui_recent_alerts_shown",
    }
