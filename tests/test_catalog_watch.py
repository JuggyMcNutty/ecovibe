"""Catalog watch: new/retired plans are detected, recorded, and notified.

The diff runs inside the price/promo cycle (same catalog fetch, no extra OVH
call). Two rules carry the weight: the FIRST scan for an account only records a
baseline (a fresh install would otherwise report the entire ~700-plan catalog as
"added"), and a truncated catalog response must never read as a mass retirement.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.monitor import MonitorService
from app.services.storage import get_storage

XHR = {"X-Requested-With": "XMLHttpRequest"}


def _catalog(*plan_codes, price=6_000_000_000):
    """A catalog shaped like OVH's, with a monthly pricing per plan."""
    return {
        "plans": [
            {
                "planCode": code,
                "invoiceName": f"Server {code}",
                "pricings": [{
                    "mode": "default", "interval": 1, "intervalUnit": "month",
                    "price": price,
                }],
            }
            for code in plan_codes
        ]
    }


def _service(account_id=None):
    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = account_id
    fake.default_currency_code.return_value = "USD"
    return fake


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    monkeypatch.setenv("OVH_DB_PATH", str(tmp_path / "test.db"))
    import app.services.storage as storage_mod
    storage_mod._storage = None
    import app.services.monitor as monitor_mod
    monitor_mod._monitor_service = None
    return MonitorService()


@pytest.fixture
def sent(monkeypatch):
    """Capture catalog-change notifications instead of sending them."""
    calls = []

    async def _notify(added, removed, account_label=None):
        calls.append({
            "added": [r["plan_code"] for r in added],
            "removed": [r["plan_code"] for r in removed],
            "account_label": account_label,
        })

    monkeypatch.setattr("app.services.notifier.notify_catalog_change", _notify)
    return calls


# ----- priming -----


@pytest.mark.asyncio
async def test_first_scan_primes_without_events_or_notifications(monitor, sent):
    """A fresh account has no baseline, so "everything is new" is an artefact
    of that, not a catalog change. Record the snapshot, report nothing."""
    storage = monitor._storage_get()

    await monitor._diff_catalog(_service(), storage, _catalog("a", "b", "c"))

    assert set(storage.load_catalog_snapshot()) == {"a", "b", "c"}
    assert storage.load_catalog_changes(_since()) == []
    assert sent == []


@pytest.mark.asyncio
async def test_unchanged_catalog_records_nothing(monitor, sent):
    storage = monitor._storage_get()
    service = _service()
    await monitor._diff_catalog(service, storage, _catalog("a", "b"))

    await monitor._diff_catalog(service, storage, _catalog("a", "b"))

    assert storage.load_catalog_changes(_since()) == []
    assert sent == []


# ----- detection -----


@pytest.mark.asyncio
async def test_added_plan_is_recorded_and_notified(monitor, sent):
    storage = monitor._storage_get()
    service = _service()
    await monitor._diff_catalog(service, storage, _catalog("a"))

    await monitor._diff_catalog(service, storage, _catalog("a", "new-plan"))

    changes = storage.load_catalog_changes(_since())
    assert len(changes) == 1
    assert changes[0]["plan_code"] == "new-plan"
    assert changes[0]["change_type"] == "added"
    # The price comes free from the catalog already in hand.
    assert changes[0]["price_in_ucents"] == 6_000_000_000
    assert changes[0]["currency_code"] == "USD"
    assert changes[0]["invoice_name"] == "Server new-plan"
    assert set(storage.load_catalog_snapshot()) == {"a", "new-plan"}
    assert sent == [{"added": ["new-plan"], "removed": [], "account_label": None}]


@pytest.mark.asyncio
async def test_removed_plan_is_recorded_and_dropped_from_snapshot(monitor, sent):
    storage = monitor._storage_get()
    service = _service()
    await monitor._diff_catalog(service, storage, _catalog("a", "b", "gone"))

    await monitor._diff_catalog(service, storage, _catalog("a", "b"))

    changes = storage.load_catalog_changes(_since())
    assert [(c["plan_code"], c["change_type"]) for c in changes] == [("gone", "removed")]
    assert set(storage.load_catalog_snapshot()) == {"a", "b"}
    assert sent == [{"added": [], "removed": ["gone"], "account_label": None}]


@pytest.mark.asyncio
async def test_one_notification_per_cycle_not_per_plan(monitor, sent):
    """A range launch touches many plan codes at once; the promo bug (17
    identical messages for one sale) must not repeat here."""
    storage = monitor._storage_get()
    service = _service()
    await monitor._diff_catalog(service, storage, _catalog("a"))

    await monitor._diff_catalog(
        service, storage, _catalog("a", *[f"new{i}" for i in range(12)])
    )

    assert len(sent) == 1
    assert len(sent[0]["added"]) == 12
    assert len(storage.load_catalog_changes(_since(), limit=100)) == 12


@pytest.mark.asyncio
async def test_snapshot_survives_a_restart(monitor, sent, tmp_path, monkeypatch):
    """The snapshot lives in SQLite precisely so a restart compares against the
    last known catalog instead of re-priming (and swallowing the change)."""
    storage = monitor._storage_get()
    await monitor._diff_catalog(_service(), storage, _catalog("a"))

    fresh = MonitorService()
    await fresh._diff_catalog(_service(), fresh._storage_get(), _catalog("a", "b"))

    assert [c["plan_code"] for c in storage.load_catalog_changes(_since())] == ["b"]
    assert len(sent) == 1


# ----- bad-response guards -----


@pytest.mark.asyncio
async def test_empty_catalog_leaves_the_snapshot_alone(monitor, sent):
    """An empty catalog is a bad response, never a retired region."""
    storage = monitor._storage_get()
    service = _service()
    await monitor._diff_catalog(service, storage, _catalog("a", "b"))

    await monitor._diff_catalog(service, storage, {"plans": []})

    assert set(storage.load_catalog_snapshot()) == {"a", "b"}
    assert storage.load_catalog_changes(_since()) == []
    assert sent == []


@pytest.mark.asyncio
async def test_mass_removal_is_treated_as_a_bad_response(monitor, sent):
    """More than half the catalog vanishing is a truncated fetch, not OVH
    retiring its range — same rule as a failed batch availability fetch
    keeping every plan's baseline."""
    storage = monitor._storage_get()
    service = _service()
    await monitor._diff_catalog(service, storage, _catalog("a", "b", "c", "d"))

    await monitor._diff_catalog(service, storage, _catalog("a"))

    assert set(storage.load_catalog_snapshot()) == {"a", "b", "c", "d"}
    assert storage.load_catalog_changes(_since()) == []
    assert sent == []


@pytest.mark.asyncio
async def test_removal_under_the_guard_threshold_still_reports(monitor, sent):
    storage = monitor._storage_get()
    service = _service()
    await monitor._diff_catalog(service, storage, _catalog("a", "b", "c", "d"))

    await monitor._diff_catalog(service, storage, _catalog("a", "b", "c"))

    assert [c["plan_code"] for c in storage.load_catalog_changes(_since())] == ["d"]
    assert len(sent) == 1


# ----- the two switches -----


@pytest.mark.asyncio
async def test_tracking_disabled_does_no_work(monitor, sent):
    storage = monitor._storage_get()
    storage.set_setting("app_catalog_watch_enabled", "false")

    await monitor._diff_catalog(_service(), storage, _catalog("a", "b"))

    assert storage.load_catalog_snapshot() == {}
    assert sent == []


@pytest.mark.asyncio
async def test_notify_disabled_still_records(monitor, sent):
    """Tracking and notifying are separate switches: the panel keeps filling
    up with the channels muted."""
    storage = monitor._storage_get()
    storage.set_setting("app_catalog_watch_notify", "false")
    service = _service()
    await monitor._diff_catalog(service, storage, _catalog("a"))

    await monitor._diff_catalog(service, storage, _catalog("a", "b"))

    assert [c["plan_code"] for c in storage.load_catalog_changes(_since())] == ["b"]
    assert sent == []


# ----- account scoping + the API -----


@pytest.mark.asyncio
async def test_accounts_have_independent_catalogs(monitor, sent):
    """Two accounts in different regions list different plans; one's snapshot
    must never make the other's plans look added or removed."""
    storage = monitor._storage_get()
    id_a = storage.save_account(None, "EU", "ovh-eu", "k", "s", "c")
    id_b = storage.save_account(None, "US", "ovh-us", "k", "s", "c")
    a, b = _service(id_a), _service(id_b)
    await monitor._diff_catalog(a, storage, _catalog("eu-1"))
    await monitor._diff_catalog(b, storage, _catalog("us-1"))

    await monitor._diff_catalog(a, storage, _catalog("eu-1", "eu-2"))

    assert set(storage.load_catalog_snapshot(id_a)) == {"eu-1", "eu-2"}
    assert set(storage.load_catalog_snapshot(id_b)) == {"us-1"}
    assert storage.load_catalog_changes(_since(), account_id=id_b) == []
    changes = storage.load_catalog_changes(_since(), account_id=id_a)
    assert [c["plan_code"] for c in changes] == ["eu-2"]
    # The notification names the account it came from — every account is
    # scanned, so an unlabelled alert would be ambiguous.
    assert sent == [{"added": ["eu-2"], "removed": [], "account_label": "EU"}]


def test_catalog_changes_endpoint_filters(client):
    storage = get_storage()
    now = datetime.now(timezone.utc)
    storage.apply_catalog_diff(
        None, [{"plan_code": "fresh", "invoice_name": "Fresh"}], [], [], now,
    )
    storage.apply_catalog_diff(
        None, [], [{"plan_code": "old", "invoice_name": "Old"}], [],
        now - timedelta(days=40),
    )

    r = client.get("/api/insights/catalog-changes?days=30")
    assert r.status_code == 200
    assert [c["plan_code"] for c in r.json()["changes"]] == ["fresh"]

    changes = client.get("/api/insights/catalog-changes?days=365").json()["changes"]
    assert [c["plan_code"] for c in changes] == ["fresh", "old"]

    changes = client.get(
        "/api/insights/catalog-changes?days=365&change_type=removed"
    ).json()["changes"]
    assert [c["plan_code"] for c in changes] == ["old"]

    assert client.get(
        "/api/insights/catalog-changes?change_type=bogus"
    ).status_code == 422


def _since(days: int = 30) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)
