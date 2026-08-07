"""The connection pragmas `Storage` opens with.

These are easy to lose in a refactor and the damage is silent: without WAL a
committing writer blocks every reader, and the poller writes continuously on a
background thread while HTTP requests read. Measured on a rollback journal, a
reader beside a live writer got 0 reads/s with a worst case of ~3 seconds; on
WAL the same reader saw ~2ms. So pin it.

The autouse `isolated_state` fixture swaps `CONNECTION_PRAGMAS` for non-durable
ones (a per-test DB does not need fsync), and it applies to these tests too. So
the production tuple is captured at import time below -- collection runs before
any fixture does -- and the tests that care about production read that.
"""
import sqlite3

import pytest

from app.services import storage as storage_mod

# Captured before `isolated_state` can swap in the test pragmas.
PRODUCTION_PRAGMAS = storage_mod.CONNECTION_PRAGMAS
_PRODUCTION = " ".join(PRODUCTION_PRAGMAS).lower()


def test_production_opens_in_wal_with_a_busy_timeout():
    """Not just 'some pragmas' -- these two specifically."""
    assert "journal_mode=wal" in _PRODUCTION
    assert "busy_timeout=" in _PRODUCTION


def test_production_does_not_weaken_synchronous():
    """WAL alone fixes the concurrency stall. Dropping `synchronous` below its
    FULL default trades durability for write latency this app has no need of,
    so it must be a deliberate decision, not something that drifts in."""
    assert "synchronous" not in _PRODUCTION


def test_the_pragmas_actually_apply_to_a_real_database(tmp_path, monkeypatch):
    """Asserting on the constant proves intent; this proves effect.

    `journal_mode` is persisted in the database file, so a second connection
    that sets no pragmas at all still reports WAL -- which is what makes this a
    check on the file rather than on the connection that wrote it.
    """
    monkeypatch.setattr(storage_mod, "CONNECTION_PRAGMAS", PRODUCTION_PRAGMAS)
    db = tmp_path / "wal.db"
    store = storage_mod.Storage(str(db))
    store.init()
    try:
        plain = sqlite3.connect(str(db))
        assert plain.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        plain.close()
    finally:
        store.close()


def test_tests_themselves_run_without_fsync():
    """The suite went from 163s to ~10s by not fsyncing throwaway DBs. If this
    fails, the fixture's override stopped taking effect and the suite is about
    to get 16x slower for no reason -- which previously read as a hang."""
    pragmas = " ".join(storage_mod.CONNECTION_PRAGMAS).lower()
    assert "synchronous=off" in pragmas
    assert "journal_mode=memory" in pragmas


@pytest.mark.parametrize("pattern", ["*.db-wal", "*.db-shm"])
def test_wal_sidecars_are_gitignored(pattern):
    """WAL adds `<db>-wal` and `<db>-shm` next to the database, and neither
    matches the existing `*.db` rule -- they would show up as untracked."""
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".gitignore")) as f:
        assert pattern in f.read()
