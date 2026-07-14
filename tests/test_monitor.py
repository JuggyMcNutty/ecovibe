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
    monitor._stock_cache["24sk10"] = [_status("24sk10", "a")]
    monitor._last_stock["24sk10"] = {"a": True}
    alerts = monitor.get_alerts()
    ok = await monitor.remove_alert(alerts[0].id)
    assert ok
    assert "24sk10" not in monitor._stock_cache
    assert "24sk10" not in monitor._last_stock


@pytest.mark.asyncio
async def test_remove_alert_keeps_cache_if_other_alerts(monitor):
    await monitor.add_alert("24sk10", "*")
    await monitor.add_alert("24sk10", "*ssd*")
    monitor._stock_cache["24sk10"] = [_status("24sk10", "a")]
    alerts = monitor.get_alerts()
    await monitor.remove_alert(alerts[0].id)
    assert "24sk10" in monitor._stock_cache


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
async def test_reload_reprimes_baseline(monitor, monkeypatch):
    """reload() clears the primed set, so the next poll after an account
    switch is silent again instead of re-notifying everything in stock."""
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
    await monitor.reload()       # simulates account switch
    changes = await monitor._poll_once()

    assert changes == []
    assert notified == []


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
    assert monitor._last_stock.get("plan-a") == {"plan-a.x": True}


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
    assert monitor._region_event is None
    assert monitor._region_primed

    # An unwatched plan gains stock → region event + logged stock event.
    entries.append(entry("plan-new", "plan-new.y"))
    await monitor._poll_once()
    event = monitor._region_event
    assert event is not None and event["type"] == "region_restock"
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


@pytest.mark.asyncio
async def test_sweep_fires_snipers_for_non_active_account(monitor, monkeypatch):
    """A sniper armed under account A must keep firing after the user switches
    away: _sweep_snipers polls A's plan under A's own credentials and fires,
    even though A's alert is no longer in the active poller's alert set."""
    import app.services.monitor as monitor_mod
    monitor_mod._sniper_service = None
    sniper = monitor_mod.get_sniper_service()
    # No active account in the isolated DB (get_active_account_id() -> None),
    # so "acct-A" is non-active and the sweep should pick it up.
    sniper.arm("alert-1", "prof-1", plan_code="24sk10",
               fqn_pattern="24sk10*ssd*", account_id="acct-A")

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.get_availability.return_value = [
        {"fqn": "24sk10.ram-32g.softraid-2x480ssd"},
        {"fqn": "24sk10.ram-64g.softraid-2x4tb"},  # doesn't match *ssd*
    ]
    monkeypatch.setattr(monitor_mod, "get_ovh_service", lambda account_id=None: fake)

    fired = []

    async def _record(alert_id, plan_code, matched, account_id=None):
        fired.append((alert_id, plan_code, tuple(matched), account_id))

    monkeypatch.setattr(sniper, "maybe_fire", _record)

    await monitor._sweep_snipers()

    fake.get_availability.assert_called_once_with("24sk10")
    assert fired == [
        ("alert-1", "24sk10", ("24sk10.ram-32g.softraid-2x480ssd",), "acct-A")
    ]


@pytest.mark.asyncio
async def test_sweep_skips_active_account_sniper(monitor, monkeypatch):
    """The sweep must NOT double-fire snipers whose account is active — those
    are handled edge-triggered by _poll_once. It skips them here."""
    import app.services.monitor as monitor_mod
    monitor_mod._sniper_service = None
    sniper = monitor_mod.get_sniper_service()
    monitor._storage_get().set_active_account_id("acct-A")
    sniper.arm("alert-1", "prof-1", plan_code="24sk10",
               fqn_pattern="*", account_id="acct-A")

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.get_availability.return_value = [{"fqn": "24sk10.ram-32g"}]
    monkeypatch.setattr(monitor_mod, "get_ovh_service", lambda account_id=None: fake)

    fired = []

    async def _record(*args, **kwargs):
        fired.append(args)

    monkeypatch.setattr(sniper, "maybe_fire", _record)

    await monitor._sweep_snipers()

    fake.get_availability.assert_not_called()
    assert fired == []
