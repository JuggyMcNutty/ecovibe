"""Shared test fixtures: isolate singletons and DB per test."""

import sqlite3

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Reset all module-level singletons and use a temp DB for each test."""
    monkeypatch.setenv("OVH_DB_PATH", str(tmp_path / "test.db"))
    # Keep the rotating log file out of the project tree during tests.
    monkeypatch.setenv("OVH_LOG_FILE", str(tmp_path / "test.log"))

    # Every test builds a fresh 14-table schema, and by default each of those
    # commits fsyncs. On this host that costs ~389ms per test (~141s of a 163s
    # run) with the process parked in D state on the XFS journal -- which looks
    # exactly like a hang. A throwaway DB that dies with the test needs no
    # crash durability, so drop it: ~389ms -> ~5ms, and the suite runs in ~15s.
    # Nothing here depends on the on-disk journal (no test shells out or opens
    # the DB itself), and monkeypatch restores the real connect after each test.
    real_connect = sqlite3.connect

    def _fast_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA journal_mode=MEMORY")
        return conn

    monkeypatch.setattr(sqlite3, "connect", _fast_connect)

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
    monitor_mod._sniper_service = None

    from app.config import get_settings
    get_settings.cache_clear()

    # Account create/update verifies credentials against OVH's GET /me
    # before saving (hard block). Stub the seam so the many tests that
    # create accounts with fake keys keep working offline; tests for the
    # verification behavior itself re-patch this explicitly.
    import app.api.accounts as accounts_api

    async def _fake_verify(endpoint, application_key, application_secret, consumer_key):
        return {"nichandle": "test-user"}

    monkeypatch.setattr(accounts_api, "_verify_credentials", _fake_verify)

    # Reset the log bus and re-attach a fresh handler pointing at it, so each
    # test sees an isolated buffer (the handler from a prior test would still
    # target the previous bus otherwise).
    import app.services.logbus as logbus_mod
    logbus_mod._log_bus = None
    from app.logging_config import setup_logging
    setup_logging()

    yield

    get_settings.cache_clear()
