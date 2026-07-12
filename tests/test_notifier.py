"""Tests for notifier message formatting."""
from app.services.notifier import _format_message


def test_format_message_uses_whole_units_and_currency_code():
    """Regression test: price must be whole currency units (already divided
    out of microcents by the caller), not raw microcents, and the currency
    must reflect the account's own currency, not a hardcoded euro sign."""
    plain, html = _format_message(
        "24sk10", ["24sk10.ram-32g"], price=59.90, currency_code="USD",
    )
    assert "59.90 USD" in plain
    assert "59.90 USD" in html
    assert "5990000000" not in plain  # raw-microcents regression guard
    assert "€" not in plain  # no hardcoded euro sign


def test_format_message_omits_price_when_unavailable():
    plain, _ = _format_message("24sk10", ["24sk10.ram-32g"], price=None)
    assert " at " not in plain


def test_format_message_escapes_html_special_chars():
    """HTML output must escape dynamic content so it can't break email markup
    or be rejected by Telegram's HTML parser (regression: was unescaped)."""
    _, html = _format_message("a&b<c>", ["fqn&<>"], price=None)
    assert "&amp;" in html and "&lt;" in html and "&gt;" in html
    # The raw unescaped sequence must not survive into the HTML.
    assert "a&b<c>" not in html
