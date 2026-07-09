"""Shared test fixtures: isolate singletons and DB per test."""

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Reset all module-level singletons and use a temp DB for each test."""
    monkeypatch.setenv("OVH_DB_PATH", str(tmp_path / "test.db"))

    import app.services.cache as cache_mod
    cache_mod._cache = None

    from app.services import currency as currency_mod
    currency_mod.reset_cache()

    import app.services.ovh_service as ovh_mod
    ovh_mod.reset_all_services()

    import app.services.storage as storage_mod
    storage_mod._storage = None

    # Stop any leaked monitor poller task before resetting the singleton.
    # Without this, background pollers from earlier tests keep running and
    # build real ovh.Client instances (network calls) once an account exists.
    import app.services.monitor as monitor_mod
    old = monitor_mod._monitor_service
    if old is not None and old._task is not None and not old._task.done():
        try:
            old._task.cancel()
        except RuntimeError:
            pass
    monitor_mod._monitor_service = None

    from app.config import get_settings
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()
