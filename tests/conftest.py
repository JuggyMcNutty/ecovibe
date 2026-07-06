"""Shared test fixtures: isolate singletons and DB per test."""

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Reset all module-level singletons and use a temp DB for each test."""
    monkeypatch.setenv("OVH_DB_PATH", str(tmp_path / "test.db"))

    import app.services.cache as cache_mod
    cache_mod._cache = None

    import app.services.ovh_service as ovh_mod
    ovh_mod._ovh_service = None

    import app.services.storage as storage_mod
    storage_mod._storage = None

    import app.services.monitor as monitor_mod
    monitor_mod._monitor_service = None

    from app.config import get_settings
    get_settings.cache_clear()

    yield

    get_settings.cache_clear()
