"""Delivery watch: order status transitions and owned-server changes.

Before this watch existed, the Orders and Servers tabs only talked to OVH when
they were opened, so a delivered server was invisible until the user happened to
click the tab — OVH's own email was the only notice. These tests pin the two
rules that keep it quiet enough to live in the poll loop: notify only on a
transition from a status we already knew, and prime the server snapshot behind
an explicit marker (because "no snapshot" means both "never scanned" and "owns
no servers").
"""
from unittest.mock import MagicMock

import pytest

from app.services.monitor import MonitorService
from app.services.ovh_service import OVHServiceError


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    """A MonitorService with an isolated SQLite DB."""
    monkeypatch.setenv("OVH_DB_PATH", str(tmp_path / "test.db"))
    import app.services.storage as storage_mod
    storage_mod._storage = None
    import app.services.monitor as monitor_mod
    monitor_mod._monitor_service = None
    return MonitorService()


def _account(storage, label, endpoint="ovh-us"):
    return storage.save_account(
        account_id=None, label=label, endpoint=endpoint,
        application_key="ak", application_secret="as", consumer_key="ck",
    )


def _service(account_id=None, orders=None, statuses=None, servers=None):
    """A fake OVHService serving a fixed order list / status map / server list."""
    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = account_id
    fake.default_currency_code.return_value = "USD"
    fake.list_orders.return_value = list(orders or [])
    fake.get_order_status.side_effect = lambda oid: (statuses or {})[oid]
    fake.list_dedicated_servers.return_value = list(servers or [])
    return fake


@pytest.fixture
def order_notes(monkeypatch):
    """Capture order-status notifications instead of sending them."""
    calls = []

    async def _notify(order_id, name, status, previous, account_label=None):
        calls.append({
            "order_id": order_id, "name": name, "status": status,
            "previous": previous, "account_label": account_label,
        })

    monkeypatch.setattr("app.services.notifier.notify_order_status", _notify)
    return calls


@pytest.fixture
def server_notes(monkeypatch):
    """Capture server-change notifications instead of sending them."""
    calls = []

    async def _notify(added, removed, account_label=None):
        calls.append({
            "added": list(added), "removed": list(removed),
            "account_label": account_label,
        })

    monkeypatch.setattr("app.services.notifier.notify_server_change", _notify)
    return calls


def _published(monitor):
    """Drain the SSE events the watch broadcast."""
    events = []
    for q in monitor._subscribers:
        while not q.empty():
            events.append(q.get_nowait())
    return events


async def _subscribe(monitor):
    return await monitor.subscribe()


# ----- orders: priming -----


@pytest.mark.asyncio
async def test_unknown_orders_are_recorded_without_notifying(monitor, order_notes):
    """A fresh install sees a pile of historical orders it has never stored.
    Reporting them would be an artefact of having no baseline, so the first
    sighting of an order id is silent no matter what state it is in."""
    storage = monitor._storage_get()
    await _subscribe(monitor)
    service = _service(
        "acct", orders=[1, 2, 3],
        statuses={1: "delivered", 2: "delivered", 3: "delivering"},
    )

    await monitor._check_orders(service, storage)

    # Recorded, so the next transition has something to compare against.
    stored = {r["order_id"]: r["status"] for r in storage.load_orders(account_id="acct")}
    assert stored == {1: "delivered", 2: "delivered", 3: "delivering"}
    assert order_notes == []
    assert _published(monitor) == []


# ----- orders: detection -----


@pytest.mark.asyncio
async def test_delivered_transition_notifies_and_publishes(monitor, order_notes):
    """The case that started this: an order known as 'delivering' turns
    'delivered' and the user should hear about it from ECOVibe, not OVH."""
    storage = monitor._storage_get()
    q = await _subscribe(monitor)
    storage.upsert_order_enriched(
        8510474, status="delivering", account_id="acct",
        server_name="KS-C | Intel Xeon E5-1650v2",
    )
    service = _service("acct", orders=[8510474], statuses={8510474: "delivered"})

    await monitor._check_orders(service, storage)

    assert storage.get_order_by_id(8510474)["status"] == "delivered"
    assert order_notes == [{
        "order_id": 8510474, "name": "KS-C | Intel Xeon E5-1650v2",
        "status": "delivered", "previous": "delivering", "account_label": None,
    }]
    event = q.get_nowait()
    assert event["type"] == "order_update"
    assert event["changes"] == [{
        "order_id": 8510474, "name": "KS-C | Intel Xeon E5-1650v2",
        "status": "delivered", "previous": "delivering",
    }]


@pytest.mark.asyncio
async def test_intermediate_transition_publishes_but_does_not_notify(monitor, order_notes):
    """OVH churns through checking/delivering; only terminal outcomes are worth
    a Telegram message. The browser still gets the update."""
    storage = monitor._storage_get()
    q = await _subscribe(monitor)
    storage.upsert_order_enriched(77, status="checking", account_id="acct")
    service = _service("acct", orders=[77], statuses={77: "delivering"})

    await monitor._check_orders(service, storage)

    assert storage.get_order_by_id(77)["status"] == "delivering"
    assert order_notes == []
    assert q.get_nowait()["changes"][0]["status"] == "delivering"


@pytest.mark.asyncio
async def test_cancelled_transition_notifies(monitor, order_notes):
    storage = monitor._storage_get()
    await _subscribe(monitor)
    storage.upsert_order_enriched(9, status="notPaid", account_id="acct")
    service = _service("acct", orders=[9], statuses={9: "cancelled"})

    await monitor._check_orders(service, storage)

    assert [c["status"] for c in order_notes] == ["cancelled"]


@pytest.mark.asyncio
async def test_unchanged_status_is_not_rewritten_or_reported(monitor, order_notes):
    storage = monitor._storage_get()
    await _subscribe(monitor)
    storage.upsert_order_enriched(5, status="delivering", account_id="acct")
    service = _service("acct", orders=[5], statuses={5: "delivering"})

    await monitor._check_orders(service, storage)

    assert order_notes == []
    assert _published(monitor) == []


# ----- orders: cost control -----


@pytest.mark.asyncio
async def test_terminal_orders_are_never_requeried(monitor, order_notes):
    """Once settled, an order costs zero OVH calls forever — otherwise a long
    history would re-check every id on every cycle."""
    storage = monitor._storage_get()
    storage.upsert_order_enriched(1, status="delivered", account_id="acct")
    storage.upsert_order_enriched(2, status="cancelled", account_id="acct")
    storage.upsert_order_enriched(3, status="delivering", account_id="acct")
    service = _service("acct", orders=[1, 2, 3], statuses={3: "delivered"})

    await monitor._check_orders(service, storage)

    assert service.get_order_status.call_count == 1
    service.get_order_status.assert_called_once_with(3)


@pytest.mark.asyncio
async def test_status_calls_are_capped_per_cycle(monitor, order_notes):
    """Every OVH call serialises on the account's client lock, so a pile of
    pending orders must not monopolise a cycle."""
    from app.services.monitor import ORDER_STATUS_BUDGET

    storage = monitor._storage_get()
    ids = list(range(1, 26))
    for oid in ids:
        storage.upsert_order_enriched(oid, status="delivering", account_id="acct")
    service = _service("acct", orders=ids, statuses={oid: "delivered" for oid in ids})

    await monitor._check_orders(service, storage)

    assert service.get_order_status.call_count == ORDER_STATUS_BUDGET
    # Newest first — the order the user is actually waiting on.
    assert service.get_order_status.call_args_list[0].args[0] == 25


@pytest.mark.asyncio
async def test_missing_order_list_is_not_an_error(monitor, order_notes):
    """OVH 404s /me/order on accounts that have never ordered."""
    storage = monitor._storage_get()
    service = _service("acct")
    service.list_orders.side_effect = OVHServiceError("nope", status_code=404)

    await monitor._check_orders(service, storage)

    assert order_notes == []


@pytest.mark.asyncio
async def test_order_list_failure_propagates(monitor, order_notes):
    """A non-404 failure must not be swallowed here — the per-account handler
    logs it, and silently reporting "no orders" would hide a broken account."""
    storage = monitor._storage_get()
    service = _service("acct")
    service.list_orders.side_effect = OVHServiceError("boom", status_code=500)

    with pytest.raises(OVHServiceError):
        await monitor._check_orders(service, storage)


# ----- servers -----


@pytest.mark.asyncio
async def test_first_server_scan_primes_silently(monitor, server_notes):
    storage = monitor._storage_get()
    await _subscribe(monitor)
    service = _service("acct", servers=["ns1.example", "ns2.example"])

    await monitor._check_servers(service, storage)

    assert set(storage.load_owned_servers("acct")) == {"ns1.example", "ns2.example"}
    assert storage.get_setting("server_watch_primed_acct") == "true"
    assert server_notes == []
    assert _published(monitor) == []


@pytest.mark.asyncio
async def test_new_server_is_recorded_and_notified(monitor, server_notes):
    storage = monitor._storage_get()
    q = await _subscribe(monitor)
    service = _service("acct", servers=["ns1.example"])
    await monitor._check_servers(service, storage)
    service.list_dedicated_servers.return_value = ["ns1.example", "ns2.example"]

    await monitor._check_servers(service, storage)

    assert set(storage.load_owned_servers("acct")) == {"ns1.example", "ns2.example"}
    assert server_notes == [
        {"added": ["ns2.example"], "removed": [], "account_label": None}
    ]
    event = q.get_nowait()
    assert event["type"] == "server_change"
    assert event["added"] == ["ns2.example"]
    assert event["added_count"] == 1


@pytest.mark.asyncio
async def test_account_starting_with_no_servers_still_announces_the_first(
    monitor, server_notes
):
    """The reason priming needs an explicit marker: an empty snapshot means
    both "never scanned" and "owns nothing". Buying your first server through
    this app is exactly the case that must NOT be swallowed as a baseline."""
    storage = monitor._storage_get()
    await _subscribe(monitor)
    service = _service("acct", servers=[])
    await monitor._check_servers(service, storage)
    assert server_notes == []

    service.list_dedicated_servers.return_value = ["ns3147088.ip-51-83-10.eu"]
    await monitor._check_servers(service, storage)

    assert server_notes == [{
        "added": ["ns3147088.ip-51-83-10.eu"], "removed": [],
        "account_label": None,
    }]


@pytest.mark.asyncio
async def test_removed_server_is_reported_and_dropped(monitor, server_notes):
    storage = monitor._storage_get()
    await _subscribe(monitor)
    service = _service("acct", servers=["ns1.example", "gone.example"])
    await monitor._check_servers(service, storage)
    service.list_dedicated_servers.return_value = ["ns1.example"]

    await monitor._check_servers(service, storage)

    assert set(storage.load_owned_servers("acct")) == {"ns1.example"}
    assert server_notes == [
        {"added": [], "removed": ["gone.example"], "account_label": None}
    ]


@pytest.mark.asyncio
async def test_failed_server_fetch_leaves_the_snapshot_alone(monitor, server_notes):
    """A failed fetch must never read as "every server vanished"."""
    storage = monitor._storage_get()
    service = _service("acct", servers=["ns1.example"])
    await monitor._check_servers(service, storage)
    service.list_dedicated_servers.side_effect = OVHServiceError("boom", status_code=500)

    with pytest.raises(OVHServiceError):
        await monitor._check_servers(service, storage)

    assert set(storage.load_owned_servers("acct")) == {"ns1.example"}
    assert server_notes == []


@pytest.mark.asyncio
async def test_unchanged_server_list_reports_nothing(monitor, server_notes):
    storage = monitor._storage_get()
    await _subscribe(monitor)
    service = _service("acct", servers=["ns1.example"])
    await monitor._check_servers(service, storage)

    await monitor._check_servers(service, storage)

    assert server_notes == []
    assert _published(monitor) == []


# ----- the cycle gate -----


@pytest.mark.asyncio
async def test_unmonitored_account_is_skipped(monitor, monkeypatch, order_notes):
    """'Monitoring: Off' means no OVH work of any kind for that account — the
    delivery watch is gated exactly like the poller and the price scan."""
    import app.services.monitor as monitor_mod

    storage = monitor._storage_get()
    a_id = _account(storage, "Account A")
    b_id = _account(storage, "Account B", "ovh-ca")
    storage.set_account_monitoring(b_id, False)
    services = {
        a_id: _service(a_id, orders=[], servers=[]),
        b_id: _service(b_id, orders=[], servers=[]),
    }
    monkeypatch.setattr(
        monitor_mod, "get_ovh_service", lambda account_id=None: services[account_id]
    )
    await monitor._load_from_storage()

    await monitor._maybe_check_orders_and_servers()

    services[a_id].list_orders.assert_called_once()
    services[b_id].list_orders.assert_not_called()
    services[b_id].list_dedicated_servers.assert_not_called()


@pytest.mark.asyncio
async def test_zero_interval_disables_the_watch(monitor, monkeypatch):
    import app.services.monitor as monitor_mod

    storage = monitor._storage_get()
    a_id = _account(storage, "Account A")
    storage.set_setting("app_order_check_interval", "0")
    service = _service(a_id, orders=[], servers=[])
    monkeypatch.setattr(monitor_mod, "get_ovh_service", lambda account_id=None: service)
    await monitor._load_from_storage()

    await monitor._maybe_check_orders_and_servers()

    service.list_orders.assert_not_called()


@pytest.mark.asyncio
async def test_one_account_failing_does_not_skip_the_others(monitor, monkeypatch):
    """A broken account must not cost the healthy one its delivery news."""
    import app.services.monitor as monitor_mod

    storage = monitor._storage_get()
    a_id = _account(storage, "Broken")
    b_id = _account(storage, "Healthy", "ovh-ca")
    services = {
        a_id: _service(a_id, orders=[], servers=[]),
        b_id: _service(b_id, orders=[], servers=[]),
    }
    services[a_id].list_orders.side_effect = OVHServiceError("boom", status_code=500)
    monkeypatch.setattr(
        monitor_mod, "get_ovh_service", lambda account_id=None: services[account_id]
    )
    await monitor._load_from_storage()

    await monitor._maybe_check_orders_and_servers()

    # The broken account's server half still ran, and so did the healthy one.
    services[a_id].list_dedicated_servers.assert_called_once()
    services[b_id].list_orders.assert_called_once()
    services[b_id].list_dedicated_servers.assert_called_once()
