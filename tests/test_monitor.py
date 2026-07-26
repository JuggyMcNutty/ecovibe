"""Tests for MonitorService: stock diffing, alert matching, lifecycle."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.services.monitor import (
    DuplicateAlertError,
    MonitorService,
    StockStatus,
)


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    """A MonitorService with an isolated SQLite DB."""
    monkeypatch.setenv("OVH_DB_PATH", str(tmp_path / "test.db"))
    import app.services.storage as storage_mod
    storage_mod._storage = None
    import app.services.monitor as monitor_mod
    monitor_mod._monitor_service = None
    m = MonitorService()
    return m


def _status(plan, fqn, available=True):
    return StockStatus(plan_code=plan, fqn=fqn, available=available, last_check=datetime.now(timezone.utc))


def test_get_stock_diff_detects_newly_available(monitor):
    diff = monitor.get_stock_diff("24sk10", [_status("24sk10", "a"), _status("24sk10", "b")])
    assert set(diff["newly_available"]) == {"a", "b"}
    assert diff["now_unavailable"] == []
    assert set(diff["currently_available"]) == {"a", "b"}


def test_get_stock_diff_detects_unavailable(monitor):
    monitor.get_stock_diff("24sk10", [_status("24sk10", "a"), _status("24sk10", "b")])
    diff = monitor.get_stock_diff("24sk10", [_status("24sk10", "a")])
    assert diff["newly_available"] == []
    assert diff["now_unavailable"] == ["b"]


def test_get_stock_diff_no_change(monitor):
    monitor.get_stock_diff("24sk10", [_status("24sk10", "a")])
    diff = monitor.get_stock_diff("24sk10", [_status("24sk10", "a")])
    assert diff["newly_available"] == []
    assert diff["now_unavailable"] == []


def test_matches_pattern_glob(monitor):
    assert monitor._matches_pattern("24sk10.ram-32g.softraid-2x480ssd", "*")
    assert monitor._matches_pattern("24sk10.ram-32g.softraid-2x480ssd", "24sk10*ssd*")
    assert not monitor._matches_pattern("24sk100.ram", "24sk10")
    assert monitor._matches_pattern("24SK10.RAM-32G", "24sk10*")


@pytest.mark.asyncio
async def test_add_alert_assigns_uuid(monitor):
    alert = await monitor.add_alert("24sk10", "*")
    assert alert.id != "24sk10:*"
    assert len(alert.id) == 36


@pytest.mark.asyncio
async def test_add_alert_rejects_duplicate(monitor):
    await monitor.add_alert("24sk10", "*")
    with pytest.raises(DuplicateAlertError):
        await monitor.add_alert("24sk10", "*")


@pytest.mark.asyncio
async def test_add_alert_allows_same_plan_different_pattern(monitor):
    a1 = await monitor.add_alert("24sk10", "*")
    a2 = await monitor.add_alert("24sk10", "*ssd*")
    assert a1.id != a2.id


@pytest.mark.asyncio
async def test_remove_alert_cleans_stock_cache(monitor):
    await monitor.add_alert("24sk10", "*")
    monitor._stock_cache[(None, "24sk10")] = [_status("24sk10", "a")]
    monitor._last_stock[(None, "24sk10")] = {"a": True}
    alerts = monitor.get_alerts()
    ok = await monitor.remove_alert(alerts[0].id)
    assert ok
    assert (None, "24sk10") not in monitor._stock_cache
    assert (None, "24sk10") not in monitor._last_stock


@pytest.mark.asyncio
async def test_remove_alert_keeps_cache_if_other_alerts(monitor):
    await monitor.add_alert("24sk10", "*")
    await monitor.add_alert("24sk10", "*ssd*")
    monitor._stock_cache[(None, "24sk10")] = [_status("24sk10", "a")]
    alerts = monitor.get_alerts()
    await monitor.remove_alert(alerts[0].id)
    assert (None, "24sk10") in monitor._stock_cache


@pytest.mark.asyncio
async def test_set_alert_enabled(monitor):
    alert = await monitor.add_alert("24sk10", "*")
    assert alert.enabled is True
    updated = await monitor.set_alert_enabled(alert.id, False)
    assert updated.enabled is False
    assert monitor.get_alert(alert.id).enabled is False


def test_set_poll_interval_clamps(monitor):
    assert monitor.set_poll_interval(0) == 1
    assert monitor.set_poll_interval(99) == 60
    assert monitor.set_poll_interval(60) == 60
    assert monitor.set_poll_interval(5) == 5


@pytest.mark.asyncio
async def test_persistence_round_trip(monitor):
    await monitor.start()
    a = await monitor.add_alert("24sk10", "*")
    await monitor.set_alert_enabled(a.id, False)
    monitor.set_poll_interval(7)
    await monitor.stop()

    import app.services.monitor as monitor_mod
    monitor_mod._monitor_service = None
    m2 = monitor_mod.get_monitor_service()
    await m2.start()
    alerts = m2.get_alerts()
    assert len(alerts) == 1
    assert alerts[0].plan_code == "24sk10"
    assert alerts[0].enabled is False
    assert m2.get_poll_interval() == 7
    await m2.stop()


@pytest.mark.asyncio
async def test_remove_alert_disarms_sniper(monitor):
    """Deleting an alert must disarm its sniper — nothing would ever fire
    it again (poller: alert gone; sweep: skips the active account)."""
    import app.services.monitor as monitor_mod
    monitor_mod._sniper_service = None
    sniper = monitor_mod.get_sniper_service()

    alert = await monitor.add_alert("24sk10", "*")
    sniper.arm(alert.id, "prof-1", plan_code="24sk10",
               fqn_pattern="*", account_id=None)

    await monitor.remove_alert(alert.id)
    assert not sniper.is_armed(alert.id)


@pytest.mark.asyncio
async def test_disable_alert_disarms_sniper(monitor):
    """Pausing an alert disarms its sniper: a disabled alert is never
    polled, so a 'paused' alert must not silently auto-order."""
    import app.services.monitor as monitor_mod
    monitor_mod._sniper_service = None
    sniper = monitor_mod.get_sniper_service()

    alert = await monitor.add_alert("24sk10", "*")
    sniper.arm(alert.id, "prof-1", plan_code="24sk10",
               fqn_pattern="*", account_id=None)

    await monitor.set_alert_enabled(alert.id, False)
    assert not sniper.is_armed(alert.id)

    # Re-enabling does NOT re-arm — arming is an explicit user action.
    await monitor.set_alert_enabled(alert.id, True)
    assert not sniper.is_armed(alert.id)


@pytest.mark.asyncio
async def test_poll_once_persists_stock_events_in_one_batch(monitor, monkeypatch):
    """The poller must persist a cycle's stock events with a single batched
    write (off the event loop), not one INSERT+commit per event."""
    import app.services.monitor as monitor_mod

    await monitor.add_alert("24sk10", "*")

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "EUR"
    fake.get_availability.return_value = [{"fqn": "24sk10.ram-32g.a"}]
    monkeypatch.setattr(
        monitor_mod, "get_active_ovh_service", lambda: fake
    )

    storage = monitor._storage_get()
    batches = []
    real_log = storage.log_stock_events

    def _spy(events):
        batches.append(list(events))
        real_log(events)

    monkeypatch.setattr(storage, "log_stock_events", _spy)

    await monitor._poll_once()
    fake.get_availability.return_value = [
        {"fqn": "24sk10.ram-32g.a"},
        {"fqn": "24sk10.ram-64g.b"},
    ]
    await monitor._poll_once()

    # The first cycle only primes the baseline (no events); the second
    # cycle's change produces exactly one batched write.
    assert batches == [[("24sk10", "24sk10.ram-64g.b", "available",
                         batches[0][0][3], None)]]
    events = storage.load_stock_events("24sk10")
    assert any(
        e["fqn"] == "24sk10.ram-64g.b" and e["event_type"] == "available"
        for e in events
    )


@pytest.mark.asyncio
async def test_first_poll_primes_silently_but_fires_armed_sniper(monitor, monkeypatch):
    """The first poll after startup/reload records the baseline without SSE
    changes or notifications, but an armed sniper still fires on stock that
    is already available (its job is to order ASAP)."""
    import app.services.monitor as monitor_mod
    monitor_mod._sniper_service = None
    sniper = monitor_mod.get_sniper_service()

    alert = await monitor.add_alert("24sk10", "*")
    sniper.arm(alert.id, "prof-1", plan_code="24sk10",
               fqn_pattern="*", account_id=None)

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "EUR"
    fake.get_availability.return_value = [{"fqn": "24sk10.ram-32g.a"}]
    monkeypatch.setattr(monitor_mod, "get_active_ovh_service", lambda: fake)

    notified = []

    async def _notify(*args, **kwargs):
        notified.append(args)

    monkeypatch.setattr(
        "app.services.notifier.notify_stock_alert", _notify
    )

    fired = []

    async def _fire(alert_id, plan_code, matched, account_id=None):
        fired.append((alert_id, plan_code, tuple(matched)))

    monkeypatch.setattr(sniper, "maybe_fire", _fire)

    changes = await monitor._poll_once()

    assert changes == []          # no SSE broadcast on the priming cycle
    assert notified == []         # no notification either
    assert fired == [(alert.id, "24sk10", ("24sk10.ram-32g.a",))]
    stored = {a["id"]: a for a in monitor._storage_get().load_alerts()}
    assert stored[alert.id]["notified_at"] is None

    # Second cycle with unchanged stock: still nothing new.
    changes = await monitor._poll_once()
    assert changes == []
    assert notified == []


@pytest.mark.asyncio
async def test_reload_keeps_baseline_and_alerts(monitor, monkeypatch):
    """reload() preserves the primed baseline for accounts that still exist.

    It used to wipe everything (the poller only watched the active account),
    which meant an account switch re-primed and dropped a cycle of edges.
    The poller now watches every account, so a reload is a re-sync, not a
    reset — and a genuine restock right after it must still be reported.
    """
    import app.services.monitor as monitor_mod

    await monitor.add_alert("24sk10", "*")

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "EUR"
    fake.get_availability.return_value = [{"fqn": "24sk10.ram-32g.a"}]
    monkeypatch.setattr(monitor_mod, "get_active_ovh_service", lambda: fake)

    notified = []

    async def _notify(*args, **kwargs):
        notified.append(args)

    monkeypatch.setattr("app.services.notifier.notify_stock_alert", _notify)

    await monitor._poll_once()   # prime
    await monitor.reload()       # simulates an account switch
    assert monitor._primed == {(None, "24sk10")}
    assert len(monitor.get_alerts()) == 1

    changes = await monitor._poll_once()
    assert changes == []         # unchanged stock is still quiet
    assert notified == []

    # A real restock after the reload is reported — the baseline survived, so
    # this is an edge and not a re-prime.
    fake.get_availability.return_value = [
        {"fqn": "24sk10.ram-32g.a"},
        {"fqn": "24sk10.ram-64g.b"},
    ]
    changes = await monitor._poll_once()
    assert [c["newly_available"] for c in changes] == [["24sk10.ram-64g.b"]]
    assert len(notified) == 1


@pytest.mark.asyncio
async def test_poll_once_persists_notified_at(monitor, monkeypatch):
    """When an alert fires, notified_at must round-trip through storage so
    it survives a restart (regression: it was only ever set in memory)."""
    import app.services.monitor as monitor_mod

    alert = await monitor.add_alert("24sk10", "*")

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "EUR"
    fake.get_availability.return_value = [{"fqn": "24sk10.ram-32g.a"}]
    monkeypatch.setattr(
        monitor_mod, "get_active_ovh_service", lambda: fake
    )

    await monitor._poll_once()  # priming cycle — no notification yet
    fake.get_availability.return_value = [
        {"fqn": "24sk10.ram-32g.a"},
        {"fqn": "24sk10.ram-64g.b"},
    ]
    await monitor._poll_once()

    stored = {a["id"]: a for a in monitor._storage_get().load_alerts()}
    assert stored[alert.id]["notified_at"] is not None


@pytest.mark.asyncio
async def test_batch_poll_single_region_fetch_for_multiple_plans(monitor, monkeypatch):
    """With 2+ watched plans, one unfiltered get_stock(None) call replaces
    per-plan availability calls, and diffs behave identically: restocks,
    sell-outs (plan vanishing from the feed), and unwatched plans ignored."""
    import app.services.monitor as monitor_mod

    await monitor.add_alert("plan-a", "*")
    await monitor.add_alert("plan-b", "*")

    def entry(plan, fqn, availability):
        return {
            "planCode": plan,
            "fqn": fqn,
            "datacenters": [{"availability": availability, "datacenter": "gra"}],
        }

    entries = [
        entry("plan-a", "plan-a.ram-1.disk-1", "1H-low"),
        entry("plan-a", "plan-a.ram-2.disk-1", "unavailable"),  # not orderable
        entry("plan-b", "plan-b.ram-1.disk-1", "72H"),
        entry("plan-zzz", "plan-zzz.x", "1H-high"),  # unwatched plan
    ]

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "EUR"
    fake.get_stock.side_effect = lambda pc=None: list(entries)
    monkeypatch.setattr(monitor_mod, "get_active_ovh_service", lambda: fake)

    changes = await monitor._poll_once()  # priming cycle
    assert changes == []
    assert fake.get_stock.call_count == 1
    fake.get_availability.assert_not_called()
    assert monitor._last_cycle_batched

    # plan-b gains a config → one restock diff for plan-b only.
    entries.append(entry("plan-b", "plan-b.ram-2.disk-1", "1H-low"))
    changes = await monitor._poll_once()
    assert [c["plan_code"] for c in changes] == ["plan-b"]
    assert changes[0]["newly_available"] == ["plan-b.ram-2.disk-1"]

    # plan-a vanishing from the feed entirely = sold out.
    entries[:] = [e for e in entries if e["planCode"] != "plan-a"]
    changes = await monitor._poll_once()
    assert any(
        c["plan_code"] == "plan-a"
        and c["now_unavailable"] == ["plan-a.ram-1.disk-1"]
        for c in changes
    )


@pytest.mark.asyncio
async def test_single_plan_poll_stays_per_plan(monitor, monkeypatch):
    """One watched plan keeps the small filtered call (1s snipe fidelity)."""
    import app.services.monitor as monitor_mod

    await monitor.add_alert("plan-a", "*")

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "EUR"
    fake.get_availability.return_value = [{"fqn": "plan-a.ram-1.disk-1"}]
    monkeypatch.setattr(monitor_mod, "get_active_ovh_service", lambda: fake)

    await monitor._poll_once()
    fake.get_availability.assert_called_once_with("plan-a")
    fake.get_stock.assert_not_called()
    assert not monitor._last_cycle_batched


def test_effective_sleep_clamps_only_in_batch_mode(monitor):
    from app.services.monitor import BATCH_MIN_POLL_INTERVAL

    monitor.set_poll_interval(1)
    monitor._last_cycle_batched = False
    assert monitor._effective_sleep() == 1
    monitor._last_cycle_batched = True
    assert monitor._effective_sleep() == BATCH_MIN_POLL_INTERVAL
    monitor.set_poll_interval(30)
    assert monitor._effective_sleep() == 30


@pytest.mark.asyncio
async def test_batch_fetch_failure_keeps_baselines(monitor, monkeypatch):
    """If the unfiltered call fails, no plan is diffed (baselines kept) —
    a transient OVH error must not look like a region-wide sell-out."""
    import app.services.monitor as monitor_mod
    from app.services.ovh_service import OVHServiceError

    await monitor.add_alert("plan-a", "*")
    await monitor.add_alert("plan-b", "*")

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "EUR"
    fake.get_stock.side_effect = lambda pc=None: [{
        "planCode": "plan-a", "fqn": "plan-a.x",
        "datacenters": [{"availability": "1H-low", "datacenter": "gra"}],
    }]
    monkeypatch.setattr(monitor_mod, "get_active_ovh_service", lambda: fake)

    await monitor._poll_once()  # prime with plan-a in stock
    fake.get_stock.side_effect = OVHServiceError("boom", status_code=500)
    changes = await monitor._poll_once()
    assert changes == []  # not a sell-out — the fetch just failed
    assert monitor._last_stock.get((None, "plan-a")) == {"plan-a.x": True}


@pytest.mark.asyncio
async def test_region_ticker_diffs_all_plans(monitor, monkeypatch):
    """With the region ticker on, unwatched plans' transitions are logged
    and a region_restock event is queued — first cycle primes silently."""
    import app.services.monitor as monitor_mod

    await monitor.add_alert("plan-a", "*")
    await monitor.set_region_enabled(True)

    def entry(plan, fqn, availability="1H-low"):
        return {
            "planCode": plan,
            "fqn": fqn,
            "datacenters": [{"availability": availability, "datacenter": "gra"}],
        }

    entries = [
        entry("plan-a", "plan-a.x"),
        entry("plan-other", "plan-other.x"),
    ]

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "EUR"
    fake.get_stock.side_effect = lambda pc=None: list(entries)
    monkeypatch.setattr(monitor_mod, "get_active_ovh_service", lambda: fake)

    await monitor._poll_once()  # primes both watched + region baselines
    assert monitor._region_events == []
    assert None in monitor._region_primed

    # An unwatched plan gains stock → region event + logged stock event.
    entries.append(entry("plan-new", "plan-new.y"))
    await monitor._poll_once()
    assert len(monitor._region_events) == 1
    event = monitor._region_events[0]
    assert event["type"] == "region_restock"
    assert event["restocks"] == [{"plan_code": "plan-new", "fqns": ["plan-new.y"]}]

    storage = monitor._storage_get()
    logged = storage.load_stock_events("plan-new")
    assert len(logged) == 1 and logged[0]["event_type"] == "available"
    # The watched plan's events come from the per-plan loop, not the region
    # diff — no duplicate rows.
    assert len(storage.load_stock_events("plan-a")) == 0  # primed, no change


@pytest.mark.asyncio
async def test_region_ticker_polls_with_no_alerts(monitor, monkeypatch):
    """The ticker keeps polling even when there are no alerts at all."""
    import app.services.monitor as monitor_mod

    await monitor.set_region_enabled(True)

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.get_stock.side_effect = lambda pc=None: []
    monkeypatch.setattr(monitor_mod, "get_active_ovh_service", lambda: fake)

    await monitor._poll_once()
    assert fake.get_stock.call_count == 1
    assert monitor._last_cycle_batched  # ticker always uses the batch fetch


@pytest.mark.asyncio
async def test_region_ticker_disabled_no_batch_for_single_plan(monitor, monkeypatch):
    """Ticker off + one plan = the old lightweight per-plan call."""
    import app.services.monitor as monitor_mod

    await monitor.add_alert("plan-a", "*")

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "EUR"
    fake.get_availability.return_value = []
    monkeypatch.setattr(monitor_mod, "get_active_ovh_service", lambda: fake)

    await monitor._poll_once()
    fake.get_stock.assert_not_called()


def _price_catalog(price, promotions=None):
    return {
        "plans": [{
            "planCode": "24sk10",
            "pricings": [{
                "mode": "default", "interval": 1, "intervalUnit": "month",
                "price": price, "promotions": promotions or [],
            }],
        }],
    }


@pytest.mark.asyncio
async def test_price_watch_fires_below_threshold_once(monitor, monkeypatch):
    """A watch notifies at/below threshold, stays quiet while the price is
    unchanged, and re-fires on a further drop."""
    storage = monitor._storage_get()
    storage.upsert_price_watch(None, "24sk10", 5_000_000_000)

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "USD"
    fake.fetch_catalog.return_value = _price_catalog(6_000_000_000)

    drops = []

    async def _drop(plan_code, price, threshold, currency_code="EUR",
                    account_label=None):
        drops.append((plan_code, price, threshold))

    monkeypatch.setattr("app.services.notifier.notify_price_drop", _drop)

    await monitor._check_prices_and_promos(fake, storage)
    assert drops == []  # above threshold

    fake.fetch_catalog.return_value = _price_catalog(4_500_000_000)
    await monitor._check_prices_and_promos(fake, storage)
    assert drops == [("24sk10", 45.0, 50.0)]

    # Same price again: no re-notification.
    await monitor._check_prices_and_promos(fake, storage)
    assert len(drops) == 1

    # Further drop: fires again.
    fake.fetch_catalog.return_value = _price_catalog(4_000_000_000)
    await monitor._check_prices_and_promos(fake, storage)
    assert drops[-1] == ("24sk10", 40.0, 50.0)

    # Price history was logged for the watched plan.
    assert storage.latest_price("24sk10") == 4_000_000_000


@pytest.mark.asyncio
async def test_promo_scan_notifies_once_per_promo(monitor, monkeypatch):
    storage = monitor._storage_get()

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "USD"
    promo = {"description": "Flash sale -30%", "value": 30}
    fake.fetch_catalog.return_value = _price_catalog(6_000_000_000, promotions=[promo])

    promos = []

    async def _promo(description, plan_codes, account_label=None):
        promos.append((description, list(plan_codes)))

    monkeypatch.setattr("app.services.notifier.notify_promo", _promo)

    await monitor._check_prices_and_promos(fake, storage)
    await monitor._check_prices_and_promos(fake, storage)  # same promo again
    assert promos == [("Flash sale -30%", ["24sk10"])]
    assert len(storage.load_recent_promos()) == 1


# ---- multi-account polling -------------------------------------------------
#
# The poller watches EVERY stored account, not just the active one, so
# switching accounts never stops monitoring. These tests use two accounts
# with their own fake services.


def _account(storage, label, endpoint="ovh-eu"):
    return storage.save_account(
        account_id=None, label=label, endpoint=endpoint,
        application_key="ak", application_secret="as", consumer_key="ck",
    )


def _fake_service(account_id, fqns_by_plan):
    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = account_id
    fake.default_currency_code.return_value = "EUR"
    fake.get_availability.side_effect = lambda pc: [
        {"fqn": f} for f in fqns_by_plan.get(pc, [])
    ]
    return fake


def _patch_services(monkeypatch, monitor_mod, services, active_id):
    """Route get_ovh_service/get_active_ovh_service to per-account fakes."""
    monkeypatch.setattr(
        monitor_mod, "get_ovh_service",
        lambda account_id=None: services[account_id or active_id],
    )
    monkeypatch.setattr(
        monitor_mod, "get_active_ovh_service", lambda: services[active_id]
    )


@pytest.mark.asyncio
async def test_polls_every_account_not_just_the_active_one(monitor, monkeypatch):
    """Both accounts' alerts are polled in one cycle, each under its own
    service — the whole point of persistent multi-account monitoring."""
    import app.services.monitor as monitor_mod

    storage = monitor._storage_get()
    a_id = _account(storage, "Account A")
    b_id = _account(storage, "Account B", "ovh-us")
    storage.set_active_account_id(a_id)
    storage.upsert_alert("al-a", "plan-a", "*", True, None, account_id=a_id)
    storage.upsert_alert("al-b", "plan-b", "*", True, None, account_id=b_id)

    stock = {
        a_id: {"plan-a": ["plan-a.x"]},
        b_id: {"plan-b": []},
    }
    services = {aid: _fake_service(aid, stock[aid]) for aid in (a_id, b_id)}
    _patch_services(monkeypatch, monitor_mod, services, a_id)

    await monitor._load_from_storage()
    await monitor._poll_once()  # primes both accounts

    services[a_id].get_availability.assert_called_once_with("plan-a")
    services[b_id].get_availability.assert_called_once_with("plan-b")

    # B is NOT active, yet its restock still diffs and is broadcast/logged.
    stock[b_id]["plan-b"] = ["plan-b.y"]
    changes = await monitor._poll_once()
    assert [(c["plan_code"], c["account_id"], c["account_label"]) for c in changes] == [
        ("plan-b", b_id, "Account B")
    ]
    assert changes[0]["newly_available"] == ["plan-b.y"]
    events = storage.load_stock_events("plan-b", account_id=b_id)
    assert [e["event_type"] for e in events] == ["available"]


@pytest.mark.asyncio
async def test_same_plan_on_two_accounts_keeps_separate_baselines(monitor, monkeypatch):
    """Two accounts watching the same plan code (different regions) must not
    share a stock baseline, or one region's stock would mask the other's."""
    import app.services.monitor as monitor_mod

    storage = monitor._storage_get()
    a_id = _account(storage, "Account A")
    b_id = _account(storage, "Account B", "ovh-us")
    storage.set_active_account_id(a_id)
    storage.upsert_alert("al-a", "24sk10", "*", True, None, account_id=a_id)
    storage.upsert_alert("al-b", "24sk10", "*", True, None, account_id=b_id)

    stock = {a_id: {"24sk10": ["24sk10.x"]}, b_id: {"24sk10": []}}
    services = {aid: _fake_service(aid, stock[aid]) for aid in (a_id, b_id)}
    _patch_services(monkeypatch, monitor_mod, services, a_id)

    await monitor._load_from_storage()
    await monitor._poll_once()

    assert monitor._last_stock[(a_id, "24sk10")] == {"24sk10.x": True}
    assert monitor._last_stock[(b_id, "24sk10")] == {}

    # Only B restocks: A must not report a change.
    stock[b_id]["24sk10"] = ["24sk10.x"]
    changes = await monitor._poll_once()
    assert [c["account_id"] for c in changes] == [b_id]


@pytest.mark.asyncio
async def test_account_switch_preserves_baselines_and_keeps_polling(monitor, monkeypatch):
    """Switching the active account must not re-prime or drop anyone: the
    regression this whole change fixes. reload() is what the accounts API
    calls on PUT /api/accounts/active."""
    import app.services.monitor as monitor_mod

    storage = monitor._storage_get()
    a_id = _account(storage, "Account A")
    b_id = _account(storage, "Account B", "ovh-us")
    storage.set_active_account_id(a_id)
    storage.upsert_alert("al-a", "plan-a", "*", True, None, account_id=a_id)
    storage.upsert_alert("al-b", "plan-b", "*", True, None, account_id=b_id)

    stock = {a_id: {"plan-a": ["plan-a.x"]}, b_id: {"plan-b": ["plan-b.y"]}}
    services = {aid: _fake_service(aid, stock[aid]) for aid in (a_id, b_id)}
    _patch_services(monkeypatch, monitor_mod, services, a_id)

    await monitor._load_from_storage()
    await monitor._poll_once()  # prime both

    storage.set_active_account_id(b_id)
    await monitor.reload()

    # Both accounts are still watched and still primed — no re-prime burst.
    assert monitor._primed == {(a_id, "plan-a"), (b_id, "plan-b")}
    assert monitor._last_stock[(a_id, "plan-a")] == {"plan-a.x": True}
    assert {a.account_id for a in monitor.get_alerts()} == {a_id, b_id}

    # A is now the background account; its sell-out still diffs and logs.
    stock[a_id]["plan-a"] = []
    changes = await monitor._poll_once()
    assert [(c["plan_code"], c["now_unavailable"]) for c in changes] == [
        ("plan-a", ["plan-a.x"])
    ]


@pytest.mark.asyncio
async def test_reload_drops_deleted_accounts_state(monitor, monkeypatch):
    """A deleted account's alerts and baselines are pruned — nothing can poll
    them again (its history rows stay queryable in SQLite)."""
    import app.services.monitor as monitor_mod

    storage = monitor._storage_get()
    a_id = _account(storage, "Account A")
    b_id = _account(storage, "Account B", "ovh-us")
    storage.set_active_account_id(a_id)
    storage.upsert_alert("al-a", "plan-a", "*", True, None, account_id=a_id)
    storage.upsert_alert("al-b", "plan-b", "*", True, None, account_id=b_id)

    stock = {a_id: {"plan-a": ["plan-a.x"]}, b_id: {"plan-b": ["plan-b.y"]}}
    services = {aid: _fake_service(aid, stock[aid]) for aid in (a_id, b_id)}
    _patch_services(monkeypatch, monitor_mod, services, a_id)

    await monitor._load_from_storage()
    await monitor._poll_once()

    storage.delete_account(b_id)
    await monitor.reload()

    assert {a.account_id for a in monitor.get_alerts()} == {a_id}
    assert monitor._primed == {(a_id, "plan-a")}
    assert (b_id, "plan-b") not in monitor._last_stock


@pytest.mark.asyncio
async def test_sniper_fires_for_a_non_active_account(monitor, monkeypatch):
    """A sniper armed under account A keeps firing after the user switches to
    B: A is still polled, and sniper matching is level-triggered (it fires on
    stock that is currently orderable, not only on the rising edge)."""
    import app.services.monitor as monitor_mod
    monitor_mod._sniper_service = None
    sniper = monitor_mod.get_sniper_service()

    storage = monitor._storage_get()
    a_id = _account(storage, "Account A")
    b_id = _account(storage, "Account B", "ovh-us")
    storage.set_active_account_id(b_id)  # A is the background account
    storage.upsert_alert(
        "al-a", "24sk10", "24sk10*ssd*", True, None, account_id=a_id
    )

    stock = {
        a_id: {"24sk10": [
            "24sk10.ram-32g.softraid-2x480ssd",
            "24sk10.ram-64g.softraid-2x4tb",  # doesn't match *ssd*
        ]},
        b_id: {},
    }
    services = {aid: _fake_service(aid, stock[aid]) for aid in (a_id, b_id)}
    _patch_services(monkeypatch, monitor_mod, services, b_id)

    await monitor._load_from_storage()
    sniper.arm("al-a", "prof-1", plan_code="24sk10",
               fqn_pattern="24sk10*ssd*", account_id=a_id)

    fired = []

    async def _record(alert_id, plan_code, matched, account_id=None):
        fired.append((alert_id, plan_code, tuple(matched), account_id))

    monkeypatch.setattr(sniper, "maybe_fire", _record)

    await monitor._poll_once()

    assert fired == [
        ("al-a", "24sk10", ("24sk10.ram-32g.softraid-2x480ssd",), a_id)
    ]

    # Still fires on a later cycle with unchanged stock (level-triggered);
    # SniperService.fqns_seen is what stops a duplicate ORDER.
    await monitor._poll_once()
    assert len(fired) == 2


@pytest.mark.asyncio
async def test_region_ticker_is_per_account(monitor, monkeypatch):
    """Enabling the ticker on one account must not enable it on another, and
    it persists on the account row."""
    storage = monitor._storage_get()
    a_id = _account(storage, "Account A")
    b_id = _account(storage, "Account B", "ovh-us")
    storage.set_active_account_id(a_id)
    await monitor._load_from_storage()

    await monitor.set_region_enabled(True)  # defaults to the active account
    assert monitor.is_region_enabled() is True
    assert monitor.is_region_enabled(b_id) is False
    assert storage.get_account(a_id)["region_ticker_enabled"] is True
    assert storage.get_account(b_id)["region_ticker_enabled"] is False

    storage.set_active_account_id(b_id)
    assert monitor.is_region_enabled() is False
    # Survives a reload (read back from the account rows).
    await monitor.reload()
    assert monitor.is_region_enabled(a_id) is True
    assert monitor.is_region_enabled(b_id) is False


def test_legacy_global_region_ticker_migrates_to_active_account(tmp_path):
    """The old app-wide region_ticker_enabled setting moves onto the account
    it was actually watching, and the settings row is dropped."""
    from app.services.storage import Storage

    db = str(tmp_path / "legacy.db")
    s = Storage(db)
    s.init()
    acct = _account(s, "Account A")
    _account(s, "Account B", "ovh-us")
    s.set_active_account_id(acct)
    s.set_setting("region_ticker_enabled", "1")

    s2 = Storage(db)
    s2.init()  # migration runs here
    assert s2.get_setting("region_ticker_enabled") is None
    accounts = {a["label"]: a["region_ticker_enabled"] for a in s2.list_accounts()}
    assert accounts == {"Account A": True, "Account B": False}


@pytest.mark.asyncio
async def test_price_check_covers_every_account(monitor, monkeypatch):
    """The price/promo scan runs once per configured account, not just the
    active one, so price history keeps building for all of them."""
    import app.services.monitor as monitor_mod

    storage = monitor._storage_get()
    a_id = _account(storage, "Account A")
    b_id = _account(storage, "Account B", "ovh-us")
    storage.set_active_account_id(a_id)

    services = {}
    for aid in (a_id, b_id):
        fake = MagicMock()
        fake.is_configured.return_value = True
        fake.account_id = aid
        fake.default_currency_code.return_value = "EUR"
        services[aid] = fake
    monkeypatch.setattr(
        monitor_mod, "get_ovh_service", lambda account_id=None: services[account_id]
    )

    scanned = []

    async def _scan(service, storage_arg):
        scanned.append(service.account_id)

    monkeypatch.setattr(monitor, "_check_prices_and_promos", _scan)

    await monitor._maybe_check_prices_and_promos()
    assert sorted(scanned) == sorted([a_id, b_id])
