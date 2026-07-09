"""Tests for the FX rate service + /api/currency/rates endpoint."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import currency


@pytest.fixture
def client():
    return TestClient(app)


def _fake_rates():
    return {
        "base": "EUR",
        "date": "2026-07-09",
        "rates": {"USD": 1.14, "GBP": 0.85, "CAD": 1.62},
    }


def test_get_rates_caches_upstream_call():
    """A second get_rates() does not re-fetch within the TTL."""
    currency.reset_cache()
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return _fake_rates()

    with patch.object(currency, "_fetch_rates", side_effect=fake_fetch):
        first = currency.get_rates()
        second = currency.get_rates()
    assert first == second == _fake_rates()
    assert calls["n"] == 1


def test_get_rates_serves_stale_when_upstream_fails():
    """If the upstream goes down after a successful fetch, the cached rates
    are served rather than None."""
    currency.reset_cache()
    with patch.object(currency, "_fetch_rates", return_value=_fake_rates()):
        currency.get_rates()
    # Now upstream fails: should return the cached (stale) payload.
    with patch.object(currency, "_fetch_rates", return_value=None):
        result = currency.get_rates()
    assert result == _fake_rates()


def test_get_rates_none_on_cold_failure():
    """First-ever fetch that fails returns None (no stale cache to serve)."""
    currency.reset_cache()
    with patch.object(currency, "_fetch_rates", return_value=None):
        assert currency.get_rates() is None


def test_convert_same_currency_is_noop():
    assert currency.convert(100.0, "EUR", "EUR", _fake_rates()) == 100.0


def test_convert_eur_to_usd():
    # 100 EUR * 1.14 = 114 USD
    assert round(currency.convert(100.0, "EUR", "USD", _fake_rates()), 2) == 114.0


def test_convert_usd_to_eur():
    # 114 USD / 1.14 = 100 EUR
    assert round(currency.convert(114.0, "USD", "EUR", _fake_rates()), 2) == 100.0


def test_convert_usd_to_gbp():
    # 100 USD -> EUR (100/1.14) -> GBP (*0.85) = 74.56
    assert round(currency.convert(100.0, "USD", "GBP", _fake_rates()), 2) == 74.56


def test_convert_unknown_currency_falls_back_to_amount():
    # Unknown 'to' code: rate_map.get returns None -> fallback to original amount
    assert currency.convert(50.0, "USD", "XYZ", _fake_rates()) == 50.0


def test_rates_endpoint_returns_payload(client):
    currency.reset_cache()
    with patch.object(currency, "_fetch_rates", return_value=_fake_rates()):
        r = client.get("/api/currency/rates")
    assert r.status_code == 200
    body = r.json()
    assert body["base"] == "EUR"
    assert "USD" in body["rates"]


def test_rates_endpoint_503_when_unavailable(client):
    currency.reset_cache()
    with patch.object(currency, "_fetch_rates", return_value=None):
        r = client.get("/api/currency/rates")
    assert r.status_code == 503
