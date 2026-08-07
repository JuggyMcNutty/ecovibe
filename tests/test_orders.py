"""Order name/price extraction tests."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api.orders import (
    _extract_price,
    _extract_server_name,
    _group_line_items,
    _name_from_details,
    _normalize_followup,
    _pick_label,
)
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_extract_server_name_from_order_object():
    assert _extract_server_name({"domain": "ns12345.ip-1-2-3.eu"}) == "ns12345.ip-1-2-3.eu"
    assert _extract_server_name({"description": "My server"}) == "My server"
    # Top-level OVH dedicated-server orders carry none of these → None.
    assert _extract_server_name({"orderId": 22135744, "date": "2026-01-01"}) is None


def test_name_from_details_prefers_description():
    """Regression: dedicated-server orders show their name on the line items,
    not the order object — so the list must fall back to details (was showing
    '(unknown)')."""
    details = [
        {"detailType": "INSTALLATION", "description": "", "domain": "*"},
        {"detailType": "DURATION", "description": "Dedicated Server KS-A | Intel i5",
         "domain": "ns3164033.ip-51-91-33.eu"},
    ]
    assert _name_from_details(details) == "Dedicated Server KS-A | Intel i5"


def test_name_from_details_falls_back_to_domain():
    details = [{"description": "", "domain": "ns999.ip-1-2-3.eu"}]
    assert _name_from_details(details) == "ns999.ip-1-2-3.eu"


def test_name_from_details_skips_placeholder_and_empty():
    assert _name_from_details([{"description": "", "domain": "*"}]) is None
    assert _name_from_details([]) is None


def test_extract_price():
    order = {"priceWithTax": {"priceInUcents": 5990000000, "currencyCode": "EUR"}}
    assert _extract_price(order) == (5990000000, "EUR")
    assert _extract_price({}) == (None, None)
    # Missing currency → treated as no price.
    assert _extract_price({"priceWithTax": {"priceInUcents": 100}}) == (None, None)


class _FakeSvc:
    """Minimal OVH service stub for the orders-list endpoint."""
    account_id = "acct-x"
    endpoint = "ovh-ca"

    def is_configured(self):
        return True

    def list_orders(self, date_from, date_to):
        return [101, 102, 103]

    def get_order(self, oid):
        return {"date": "2026-06-01T00:00:00+00:00", "priceWithTax": {}}

    def get_order_status(self, oid):
        return "notPaid"

    def get(self, path, **kwargs):  # detail lookups — shouldn't matter here
        return []

    def get_order_details(self, oid):
        return _ORDER_22135744_DETAILS


def test_orders_never_dropped_on_enrichment_timeout(client, monkeypatch):
    """A slow/timed-out enrichment must never make an OVH order disappear
    (regression: an unpaid order stopped showing when enrichment timed out)."""
    monkeypatch.setattr("app.api.orders.get_active_ovh_service", lambda: _FakeSvc())
    # Force the enrichment loop to time out immediately so no order gets
    # enriched inside the timeout block. Capture the real timeout first —
    # asyncio is a shared module, so patching it in place would otherwise
    # recurse into this lambda.
    real_timeout = asyncio.timeout
    monkeypatch.setattr("app.api.orders.asyncio.timeout", lambda _t: real_timeout(0))

    r = client.get("/api/orders")
    assert r.status_code == 200
    body = r.json()
    assert body["timed_out"] is True
    # All three OVH orders must still be present.
    assert {o["order_id"] for o in body["orders"]} == {101, 102, 103}


def _price(value):
    return {"value": value, "text": f"${value:.2f} USD", "currencyCode": "USD"}


# The 8 raw detail rows OVH returned for the unpaid ovh-ca order 22135744
# (KS-C server, 32GB RAM, 500Mbps bandwidth, 120GB SSD). Trimmed to the fields
# the grouping logic reads.
_ORDER_22135744_DETAILS = [
    {"domain": "*001.002", "detailType": "INSTALLATION", "description": "32GB DDR3 ECC 1333MHz", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
    {"domain": "*001", "detailType": "INSTALLATION", "description": "KS-C | Intel Xeon E5-1650v2 rental - datacenter gra - ", "quantity": "1", "cancelled": False, "totalPrice": _price(13.30)},
    {"domain": "*001.001", "detailType": "DURATION", "description": "500Mbps unmetered public bandwidth rental - 1 month", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
    {"domain": "*001", "detailType": "DURATION", "description": "KS-C | Intel Xeon E5-1650v2 rental - datacenter gra - 1 month", "quantity": "1", "cancelled": False, "totalPrice": _price(13.30)},
    {"domain": "*001", "detailType": "INSTALLATION", "description": "KS-C - Intel Xeon E5-1650v2", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
    {"domain": "*001.002", "detailType": "DURATION", "description": "32GB DDR3 ECC 1333MHz rental - 1 month", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
    {"domain": "*001.003", "detailType": "INSTALLATION", "description": "1x 120GB SSD", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
    {"domain": "*001.003", "detailType": "DURATION", "description": "1x 120GB SSD rental - 1 month", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
]


def test_group_line_items_collapses_duplicates():
    """8 raw OVH rows collapse to 4 items, one per physical component, in
    domain order (server first, then its options)."""
    items = _group_line_items(_ORDER_22135744_DETAILS)
    assert [i["domain"] for i in items] == ["*001", "*001.001", "*001.002", "*001.003"]

    server = items[0]
    assert server["label"] == "KS-C - Intel Xeon E5-1650v2"
    # Setup sums both INSTALLATION rows ($0 + $13.30); monthly is the DURATION row.
    assert server["setup_price"]["value"] == 13.30
    assert server["setup_price"]["text"] == "$13.30 USD"
    assert server["recurring_price"]["value"] == 13.30

    # Included options: zero setup + zero monthly.
    ram = items[2]
    assert ram["label"] == "32GB DDR3 ECC 1333MHz"
    assert ram["setup_price"]["value"] == 0
    assert ram["recurring_price"]["value"] == 0

    # Bandwidth has only a DURATION row → no setup price at all.
    bandwidth = items[1]
    assert bandwidth["label"] == "500Mbps unmetered public bandwidth"
    assert bandwidth["setup_price"] is None


def test_refresh_rederives_stale_server_name(client, monkeypatch):
    """`?refresh=true` must re-derive a title that was cached wrong (as the RAM
    option) instead of trusting the persisted value; a plain load keeps it."""
    from app.services.storage import get_storage
    monkeypatch.setattr("app.api.orders.get_active_ovh_service", lambda: _FakeSvc())
    get_storage().upsert_order_enriched(
        101, status="notPaid", server_name="32GB DDR3 ECC 1333MHz", account_id="acct-x"
    )

    # Plain load trusts the (wrong) cached name.
    o = next(o for o in client.get("/api/orders").json()["orders"] if o["order_id"] == 101)
    assert o["server_name"] == "32GB DDR3 ECC 1333MHz"

    # Refresh re-derives the server from the line items and persists it.
    o = next(o for o in client.get("/api/orders?refresh=true").json()["orders"] if o["order_id"] == 101)
    assert o["server_name"] == "KS-C - Intel Xeon E5-1650v2"


def test_name_from_details_picks_server_not_option():
    """Regression: an order's list title showed the RAM option instead of the
    server. The server is the priciest line item, not the first detail row."""
    assert _name_from_details(_ORDER_22135744_DETAILS) == "KS-C - Intel Xeon E5-1650v2"


# ovh-us reports order details differently from ovh-ca/eu: every row's `domain`
# is "*" (no hierarchy) and there is NO `detailType` — the setup vs monthly
# split is encoded only in the description ("X" vs "X - 1 month"). Real rows
# from the unpaid US order 8336243 (KS-1 server, 2x2TB HDD, 32GB RAM, 500Mbps).
_ORDER_8336243_DETAILS_US = [
    {"domain": "*", "description": "KS-1 | Intel Xeon-D 1520", "quantity": "1", "cancelled": False, "totalPrice": _price(20)},
    {"domain": "*", "description": "KS-1 | Intel Xeon-D 1520 - 1 month", "quantity": "1", "cancelled": False, "totalPrice": _price(20)},
    {"domain": "*", "description": "2x 2TB HDD Soft RAID", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
    {"domain": "*", "description": "2x 2TB HDD Soft RAID - 1 month", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
    {"domain": "*", "description": "32GB DDR4 ECC 2133MHz", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
    {"domain": "*", "description": "32GB DDR4 ECC 2133MHz - 1 month", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
    {"domain": "*", "description": "500Mbps unmetered public bandwidth", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
    {"domain": "*", "description": "500Mbps unmetered public bandwidth - 1 month", "quantity": "1", "cancelled": False, "totalPrice": _price(0)},
]


def test_group_line_items_us_format_by_description():
    """ovh-us has no detailType and domain '*' for every row, so grouping falls
    back to the product label and the setup/monthly split is read from the
    '- 1 month' suffix. 8 rows collapse to 4 items, server first."""
    items = _group_line_items(_ORDER_8336243_DETAILS_US)
    assert [i["label"] for i in items] == [
        "KS-1 | Intel Xeon-D 1520",
        "2x 2TB HDD Soft RAID",
        "32GB DDR4 ECC 2133MHz",
        "500Mbps unmetered public bandwidth",
    ]
    server = items[0]
    assert server["setup_price"]["value"] == 20
    assert server["recurring_price"]["value"] == 20  # $20 setup + $20/mo = $40 total
    # Options are included ($0 setup + $0 monthly), not missing.
    assert items[1]["setup_price"]["value"] == 0
    assert items[1]["recurring_price"]["value"] == 0


def test_name_from_details_us_picks_server():
    """ovh-us title must be the server, not an option (regression: showed the
    HDD because all rows share domain '*' and carry no price differentiation)."""
    assert _name_from_details(_ORDER_8336243_DETAILS_US) == "KS-1 | Intel Xeon-D 1520"


def test_pick_label_prefers_clean_non_rental_description():
    assert _pick_label([
        "KS-C | Intel Xeon E5-1650v2 rental - datacenter gra - 1 month",
        "KS-C - Intel Xeon E5-1650v2",
    ]) == "KS-C - Intel Xeon E5-1650v2"
    # Rental-only descriptions get the boilerplate stripped.
    assert _pick_label(["32GB DDR3 ECC 1333MHz rental - 1 month"]) == "32GB DDR3 ECC 1333MHz"
    assert _pick_label([]) == "(line item)"


# ----- delivery follow-up normalisation -----
#
# OVH sends follow-up history dates NAIVE and space-separated
# ("2026-08-04 20:05:02") while the order's own `date` carries a real offset.
# Rendered as-is the browser read them as its own local time (an hour off on a
# -05:00 host for a -04:00 order) and Safari rejected them outright. OVH also
# returns each step's history newest-first, which read backwards next to the
# steps, which run forwards.

# The live shape of order 8510474's VALIDATING step, newest-first as OVH sends it.
_FOLLOWUP_8510474 = [
    {
        "step": "VALIDATING",
        "status": "DONE",
        "history": [
            {"date": "2026-08-04 20:05:02", "label": "ORDER_ACCEPTED",
             "description": "Order accepted"},
            {"date": "2026-08-04 20:05:00", "label": "FRAUD_CHECK",
             "description": "Check fraud started"},
            {"date": "2026-08-04 20:04:55", "label": "ORDER_STARTED",
             "description": "Order workflow started"},
        ],
    },
    {"step": "DELIVERING", "status": "DOING", "history": [
        {"date": "2026-08-06 20:55:30", "label": "INVOICE_IN_PROGRESS",
         "description": "Invoice creation started"},
        {"date": "2026-08-04 20:05:15", "label": "DELIVERY",
         "description": "Service delivery started"},
    ]},
    {"step": "AVAILABLE", "status": "TODO", "history": []},
]

_ORDER_8510474 = {"date": "2026-08-04T20:01:19.979215-04:00"}


def test_followup_dates_get_the_orders_offset():
    """The one-hour bug: a naive -04:00 timestamp rendered as browser-local."""
    steps = _normalize_followup(_FOLLOWUP_8510474, _ORDER_8510474)
    assert [h["date"] for h in steps[0]["history"]] == [
        "2026-08-04T20:04:55-04:00",
        "2026-08-04T20:05:00-04:00",
        "2026-08-04T20:05:02-04:00",
    ]


def test_followup_history_is_returned_oldest_first():
    """OVH sends newest-first; the steps run forwards, so the events must too."""
    steps = _normalize_followup(_FOLLOWUP_8510474, _ORDER_8510474)
    assert [h["label"] for h in steps[0]["history"]] == [
        "ORDER_STARTED", "FRAUD_CHECK", "ORDER_ACCEPTED",
    ]
    assert [h["label"] for h in steps[1]["history"]] == ["DELIVERY", "INVOICE_IN_PROGRESS"]


def test_followup_preserves_step_order_and_states():
    steps = _normalize_followup(_FOLLOWUP_8510474, _ORDER_8510474)
    assert [(s["step"], s["status"]) for s in steps] == [
        ("VALIDATING", "DONE"), ("DELIVERING", "DOING"), ("AVAILABLE", "TODO"),
    ]


def test_followup_keeps_both_label_and_description():
    """`label` stays OVH's enum and `description` its human string — the
    frontend picks. Repurposing either would mislead against OVH's API docs."""
    h = _normalize_followup(_FOLLOWUP_8510474, _ORDER_8510474)[0]["history"][0]
    assert h["label"] == "ORDER_STARTED"
    assert h["description"] == "Order workflow started"


def test_followup_leaves_an_already_aware_date_alone():
    """Defensive: if OVH starts sending offsets, don't overwrite them."""
    fu = [{"step": "VALIDATING", "status": "DONE", "history": [
        {"date": "2026-08-04T20:05:02+02:00", "label": "ORDER_ACCEPTED"},
    ]}]
    steps = _normalize_followup(fu, _ORDER_8510474)
    assert steps[0]["history"][0]["date"] == "2026-08-04T20:05:02+02:00"


def test_followup_without_an_order_date_invents_no_offset():
    """No offset to attach → still normalise the separator to ISO (so Safari
    can parse it) but never fabricate a timezone."""
    steps = _normalize_followup(_FOLLOWUP_8510474, {})
    assert steps[0]["history"][0]["date"] == "2026-08-04T20:04:55"
    steps = _normalize_followup(_FOLLOWUP_8510474, {"date": "not-a-date"})
    assert steps[0]["history"][0]["date"] == "2026-08-04T20:04:55"


def test_followup_passes_through_an_unparseable_date():
    """A weird date is still a better row than a dropped one; it sorts last."""
    fu = [{"step": "VALIDATING", "status": "DONE", "history": [
        {"date": "garbage", "label": "WEIRD"},
        {"date": "2026-08-04 20:05:02", "label": "ORDER_ACCEPTED"},
    ]}]
    steps = _normalize_followup(fu, _ORDER_8510474)
    assert [h["label"] for h in steps[0]["history"]] == ["ORDER_ACCEPTED", "WEIRD"]
    assert steps[0]["history"][1]["date"] == "garbage"


def test_followup_handles_the_unpaid_order_shape():
    """An unpaid order is four TODO steps with no history at all."""
    fu = [{"step": s, "status": "TODO", "history": []}
          for s in ("VALIDATING", "VALIDATED", "DELIVERING", "AVAILABLE")]
    assert _normalize_followup(fu, {"date": "2026-07-09T23:42:54-04:00"}) == fu
    assert _normalize_followup([], _ORDER_8510474) == []


def test_followup_mixed_naive_and_aware_without_offset_does_not_raise():
    """Sorting must not compare naive against aware datetimes (TypeError)."""
    fu = [{"step": "VALIDATING", "status": "DONE", "history": [
        {"date": "2026-08-04T20:05:02+02:00", "label": "AWARE"},
        {"date": "2026-08-04 20:04:55", "label": "NAIVE"},
    ]}]
    steps = _normalize_followup(fu, {})
    assert {h["label"] for h in steps[0]["history"]} == {"AWARE", "NAIVE"}
