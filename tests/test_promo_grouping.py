"""Promotions are grouped by campaign, in the API and in notifications.

OVH attaches a campaign to every plan it covers, so one flash sale writes one
promo_events row per plan code. A real sale produced 17 rows of identical text
across 9 distinct promo_keys - the keys differ because the payload hash
includes each plan's own discount amount. Grouping therefore keys on the
promo's `name`, which is constant across the campaign.
"""
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.insights import _group_promos
from app.main import app
from app.services.monitor import MonitorService
from app.services.storage import get_storage

CAMPAIGN = "FLASHSALE_WW_RISE_1_3_AND_GAME_T1"
DESC = "Special offer : Free setup fees on Rise-1, Rise-3 and KS-GAME"


def _payload(discount_value, name=CAMPAIGN, desc=DESC, **extra):
    """A promo payload shaped like OVH's, with a per-plan discount amount."""
    body = {
        "name": name,
        "description": desc,
        "discount": {"value": discount_value},
        "startDate": "2026-07-21T08:00:00+02:00",
        "endDate": "2026-08-31T14:00:00+02:00",
    }
    body.update(extra)
    return json.dumps(body, sort_keys=True)


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


# ----- grouping -----


def test_one_campaign_across_plans_collapses_to_one_entry():
    """The reported bug: 17 near-identical rows rendered as 17 panel entries.
    Differing discount amounts must not split the campaign."""
    rows = [
        {"plan_code": f"24rise0{i}", "promo_key": f"k{i}",
         "payload": _payload(100 + i), "first_seen": "2026-07-26T10:00:00+00:00"}
        for i in range(5)
    ]

    groups = _group_promos(rows)

    assert len(groups) == 1
    assert groups[0]["plan_count"] == 5
    assert groups[0]["plan_codes"] == sorted(r["plan_code"] for r in rows)
    assert groups[0]["description"] == DESC
    assert groups[0]["end_date"] == "2026-08-31T14:00:00+02:00"


def test_distinct_campaigns_stay_separate():
    rows = [
        {"plan_code": "a", "promo_key": "k1", "payload": _payload(1),
         "first_seen": "2026-07-26T10:00:00+00:00"},
        {"plan_code": "b", "promo_key": "k2",
         "payload": _payload(1, name="OTHER_SALE", desc="Half price"),
         "first_seen": "2026-07-26T11:00:00+00:00"},
    ]

    groups = _group_promos(rows)

    assert len(groups) == 2
    # Newest campaign first.
    assert groups[0]["name"] == "OTHER_SALE"
    assert groups[1]["name"] == CAMPAIGN


def test_groups_by_description_when_name_absent():
    """Older or odd payloads may carry no `name`; description is the fallback
    key so those still collapse instead of listing one row per plan."""
    payload = json.dumps({"description": "Free setup"}, sort_keys=True)
    rows = [
        {"plan_code": "a", "promo_key": "k1", "payload": payload,
         "first_seen": "2026-07-26T10:00:00+00:00"},
        {"plan_code": "b", "promo_key": "k2", "payload": payload,
         "first_seen": "2026-07-26T10:00:00+00:00"},
    ]

    groups = _group_promos(rows)

    assert len(groups) == 1
    assert groups[0]["plan_codes"] == ["a", "b"]


def test_reports_earliest_sighting_of_a_campaign():
    """The panel shows "N ago" - it should mean when the campaign appeared,
    not when its most recent plan row was written."""
    rows = [
        {"plan_code": "b", "promo_key": "k2", "payload": _payload(2),
         "first_seen": "2026-07-26T12:00:00+00:00"},
        {"plan_code": "a", "promo_key": "k1", "payload": _payload(1),
         "first_seen": "2026-07-26T09:00:00+00:00"},
    ]

    groups = _group_promos(rows)

    assert groups[0]["first_seen"] == "2026-07-26T09:00:00+00:00"


def test_malformed_payload_does_not_break_grouping():
    """record_promo falls back to repr() for unserialisable promos, so the
    payload is not guaranteed to be JSON."""
    rows = [
        {"plan_code": "a", "promo_key": "k1", "payload": "not json at all",
         "first_seen": "2026-07-26T10:00:00+00:00"},
        {"plan_code": "b", "promo_key": "k2", "payload": "[1, 2, 3]",
         "first_seen": "2026-07-26T10:00:00+00:00"},
    ]

    groups = _group_promos(rows)

    assert len(groups) == 2
    assert all(g["plan_count"] == 1 for g in groups)


def test_promos_endpoint_returns_grouped_campaigns(client):
    storage = get_storage()
    for i in range(4):
        storage.record_promo(f"24rise0{i}", f"key{i}", _payload(100 + i))

    promos = client.get("/api/insights/promos").json()["promos"]

    assert len(promos) == 1
    assert promos[0]["plan_count"] == 4
    assert promos[0]["name"] == CAMPAIGN


def test_promos_endpoint_limit_caps_campaigns_not_rows(client):
    """`limit` counts campaigns; one campaign spanning many plans must not
    consume the caller's budget."""
    storage = get_storage()
    for i in range(6):
        storage.record_promo(f"plan{i}", f"key{i}", _payload(i))
    storage.record_promo("other", "otherkey", _payload(1, name="SECOND"))

    # 7 stored rows, 2 campaigns: limit=1 yields one campaign, not one row.
    assert len(client.get("/api/insights/promos?limit=1").json()["promos"]) == 1

    promos = client.get("/api/insights/promos?limit=2").json()["promos"]

    assert len(promos) == 2
    assert {p["name"]: p["plan_count"] for p in promos} == {CAMPAIGN: 6, "SECOND": 1}


# ----- notification fan-out -----


@pytest.mark.asyncio
async def test_campaign_notifies_once_not_once_per_plan(monitor, monkeypatch):
    """The spam regression: a 17-plan campaign sent 17 identical messages.
    One campaign must produce exactly one notification carrying every plan."""
    storage = monitor._storage_get()
    promo = {
        "name": CAMPAIGN,
        "description": DESC,
        "discount": {"value": 1},
    }
    plans = [
        {
            "planCode": f"24rise{i:02d}",
            "pricings": [{
                "mode": "default", "interval": 1, "intervalUnit": "month",
                "price": 6_000_000_000,
                # Per-plan discount amount => a different payload hash per
                # plan, exactly as OVH sends it.
                "promotions": [dict(promo, discount={"value": 100 + i})],
            }],
        }
        for i in range(17)
    ]

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "USD"
    fake.fetch_catalog.return_value = {"plans": plans}

    sent = []

    async def _promo(description, plan_codes):
        sent.append((description, list(plan_codes)))

    monkeypatch.setattr("app.services.notifier.notify_promo", _promo)

    await monitor._check_prices_and_promos(fake, storage)

    assert len(sent) == 1
    assert sent[0][0] == DESC
    assert len(sent[0][1]) == 17
    # Every plan row still stored individually for per-plan dedup.
    assert len(storage.load_recent_promos(limit=100)) == 17


@pytest.mark.asyncio
async def test_two_campaigns_notify_once_each(monitor, monkeypatch):
    storage = monitor._storage_get()
    plans = [
        {
            "planCode": f"plan{i}",
            "pricings": [{
                "mode": "default", "interval": 1, "intervalUnit": "month",
                "price": 6_000_000_000,
                "promotions": [{
                    "name": "A" if i < 2 else "B",
                    "description": "first" if i < 2 else "second",
                    "discount": {"value": i},
                }],
            }],
        }
        for i in range(4)
    ]

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.account_id = None
    fake.default_currency_code.return_value = "USD"
    fake.fetch_catalog.return_value = {"plans": plans}

    sent = []

    async def _promo(description, plan_codes):
        sent.append((description, sorted(plan_codes)))

    monkeypatch.setattr("app.services.notifier.notify_promo", _promo)

    await monitor._check_prices_and_promos(fake, storage)

    assert sorted(sent) == [
        ("first", ["plan0", "plan1"]),
        ("second", ["plan2", "plan3"]),
    ]
