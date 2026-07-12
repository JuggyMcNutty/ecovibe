"""Tests for MonitorService: stock diffing, alert matching, lifecycle."""

from datetime import datetime, timezone

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
