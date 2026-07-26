"""Tests for notifier message formatting and SMTP delivery."""
import pytest

from app.services import notifier
from app.services.notifier import _format_message
from app.services.storage import get_storage


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


# ----- SMTP test email (Settings → Notifications → Send test email) -----


class _FakeSMTP:
    """Records what a send did without touching the network.

    Doubles as a context manager so it can stand in for `smtplib.SMTP`.
    """
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def smtp_settings():
    """Store a complete set of SMTP settings in the DB."""
    storage = get_storage()
    for key, val in {
        "smtp_host": "smtp.example.com",
        "smtp_port": "2525",
        "smtp_username": "user@example.com",
        "smtp_password": "app-password",
        "smtp_from": "alerts@example.com",
        "notify_email_to": "me@example.com",
    }.items():
        storage.set_setting(f"notifier_{key}", val)
    return storage


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    _FakeSMTP.instances = []
    yield


def test_send_test_email_delivers_with_stored_settings(smtp_settings, monkeypatch):
    """Happy path: connects to the configured host/port, authenticates over
    STARTTLS, and sends to the configured recipient."""
    monkeypatch.setattr(notifier.smtplib, "SMTP", _FakeSMTP)

    recipient = notifier.send_test_email()

    assert recipient == "me@example.com"
    assert len(_FakeSMTP.instances) == 1
    conn = _FakeSMTP.instances[0]
    assert (conn.host, conn.port) == ("smtp.example.com", 2525)
    assert conn.started_tls is True
    assert conn.login_args == ("user@example.com", "app-password")
    assert len(conn.sent) == 1
    assert conn.sent[0]["To"] == "me@example.com"
    assert conn.sent[0]["From"] == "alerts@example.com"


def test_send_test_email_skips_login_without_credentials(smtp_settings, monkeypatch):
    """A relay that needs no auth must not get STARTTLS/login forced on it."""
    smtp_settings.set_setting("notifier_smtp_username", "")
    smtp_settings.set_setting("notifier_smtp_password", "")
    monkeypatch.setattr(notifier.smtplib, "SMTP", _FakeSMTP)

    notifier.send_test_email()

    conn = _FakeSMTP.instances[0]
    assert conn.started_tls is False
    assert conn.login_args is None


def test_send_test_email_reports_missing_fields():
    """Unconfigured email must name every missing field, not fail opaquely."""
    with pytest.raises(notifier.EmailNotConfiguredError) as exc:
        notifier.send_test_email()
    detail = str(exc.value)
    assert "SMTP host" in detail
    assert "From address" in detail
    assert "To address" in detail


def test_send_test_email_wraps_auth_failure(smtp_settings, monkeypatch):
    """A bad password is the single most likely failure - it must come back
    as an actionable message rather than a stack trace."""
    class _AuthFailSMTP(_FakeSMTP):
        def login(self, username, password):
            raise notifier.smtplib.SMTPAuthenticationError(535, b"Bad credentials")

    monkeypatch.setattr(notifier.smtplib, "SMTP", _AuthFailSMTP)

    with pytest.raises(notifier.EmailSendError) as exc:
        notifier.send_test_email()
    assert "535" in str(exc.value)
    assert "app password" in str(exc.value).lower()


def test_send_test_email_wraps_connection_failure(smtp_settings, monkeypatch):
    """Wrong host/port or a firewall must surface as a connection error."""
    def _boom(host, port, timeout=None):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(notifier.smtplib, "SMTP", _boom)

    with pytest.raises(notifier.EmailSendError) as exc:
        notifier.send_test_email()
    assert "smtp.example.com:2525" in str(exc.value)


def test_background_send_email_still_swallows_failures(smtp_settings, monkeypatch):
    """Regression guard: a broken mail server must never propagate out of the
    notification fan-out and take down the other channels."""
    def _boom(host, port, timeout=None):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(notifier.smtplib, "SMTP", _boom)

    notifier._send_email("subject", "<b>body</b>")  # must not raise


def test_background_send_email_noop_when_unconfigured(monkeypatch):
    """No SMTP settings means no connection attempt at all."""
    monkeypatch.setattr(notifier.smtplib, "SMTP", _FakeSMTP)

    notifier._send_email("subject", "<b>body</b>")

    assert _FakeSMTP.instances == []
