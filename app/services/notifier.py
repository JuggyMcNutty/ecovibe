"""Multi-channel notifier: Telegram, Discord, Slack, email.

Settings are read from the DB settings table (set via the Settings UI)
with env var fallback for initial setup. The helper ``_get_notifier_setting``
checks DB first, then env.
"""
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from html import escape
from typing import Any, NamedTuple

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def _get_notifier_setting(key: str) -> str | None:
    """Read a notifier setting from the DB, falling back to env vars.

    DB keys are prefixed with ``notifier_`` to namespace them. Returns
    None if not configured in either DB or env.
    """
    try:
        from app.services.storage import get_storage
        storage = get_storage()
        val = storage.get_setting(f"notifier_{key}")
        if val is not None and val != "":
            return val
    except Exception:
        pass
    return getattr(get_settings(), key, None)


def _account_suffix(account_label: str | None) -> str:
    """`" [label]"` for a named account, else empty.

    Every account is polled at once, so an alert has to say which one it
    came from — otherwise a restock on a background account is
    indistinguishable from one on the account you're looking at.
    """
    return f" [{account_label}]" if account_label else ""


def _format_message(
    plan_code: str, fqns: list[str], price: float | None = None,
    currency_code: str = "EUR", account_label: str | None = None,
) -> tuple[str, str]:
    """Build the alert text in both plain-text and HTML forms.

    `price` is the latest known price in the account's own currency units
    (already divided out of microcents by the caller), or None if
    unavailable. Truncates the FQN list to the first 5 entries with a
    "+N more" suffix to avoid overflowing chat clients on plans with many
    configs.
    """
    fqn_list = ", ".join(fqns[:5])
    extra = f" (+{len(fqns) - 5} more)" if len(fqns) > 5 else ""
    price_str = f" at {price:.2f} {currency_code}" if price is not None else ""
    acct = _account_suffix(account_label)
    plain = (
        f"OVH stock alert{acct}: {plan_code} now available{price_str}\n"
        f"Configs: {fqn_list}{extra}"
    )
    # Escape dynamic content so a config code or currency string containing
    # HTML-special chars (&, <, >) can't break the HTML markup (email) or be
    # rejected by Telegram's HTML parser with a 400.
    html = (
        f"<b>OVH stock alert</b>{escape(acct)}: <code>{escape(plan_code)}</code>"
        f" now available{escape(price_str)}<br>"
        f"<b>Configs</b>: <code>{escape(fqn_list)}</code>{escape(extra)}"
    )
    return plain, html


async def _send_telegram(html: str) -> None:
    """Send a Telegram message via the Bot API. No-op if not configured.

    ``html`` must already have its dynamic parts escaped (see the
    ``notify_*`` formatters)."""
    token = _get_notifier_setting("telegram_bot_token")
    chat_id = _get_notifier_setting("telegram_chat_id")
    if not (token and chat_id):
        return
    # Telegram HTML doesn't support <br>; it uses literal newlines.
    text = html.replace("<br>", "\n")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
            if r.status_code != 200:
                logger.warning("telegram responded %s: %s", r.status_code, r.text[:200])
    except Exception:
        logger.warning("telegram notification failed", exc_info=True)


async def _send_discord(
    subject: str, plain: str, fields: list[dict[str, Any]] | None = None,
) -> None:
    """Post a Discord webhook with a rich embed. No-op if not configured."""
    webhook_url = _get_notifier_setting("discord_webhook_url")
    if not webhook_url:
        return
    embed: dict[str, Any] = {
        "title": f"\U0001F514 {subject}",
        "description": plain,
        "color": 0x60A5FA,  # tailwind blue-400
    }
    if fields:
        embed["fields"] = fields
    payload = {"embeds": [embed]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook_url, json=payload)
            # Discord returns 204 on success.
            if r.status_code not in (200, 204):
                logger.warning("discord responded %s: %s", r.status_code, r.text[:200])
    except Exception:
        logger.warning("discord notification failed", exc_info=True)


async def _send_slack(subject: str, plain: str) -> None:
    """Post to a Slack incoming webhook. No-op if not configured."""
    webhook_url = _get_notifier_setting("slack_webhook_url")
    if not webhook_url:
        return
    payload = {
        "text": f"\U0001F514 {plain}",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{subject}*\n{plain}"},
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook_url, json=payload)
            if r.status_code != 200:
                logger.warning("slack responded %s: %s", r.status_code, r.text[:200])
    except Exception:
        logger.warning("slack notification failed", exc_info=True)


class EmailNotConfiguredError(RuntimeError):
    """Raised when an email send is attempted with incomplete SMTP settings.

    The message is a comma-separated list of the missing field labels, so
    the Settings UI can tell the user exactly what to fill in.
    """


class EmailSendError(RuntimeError):
    """Raised when an email fails to send, carrying a human-readable reason.

    Only the test path raises this; `_send_email` still swallows failures
    so a broken mail server can never take down a stock notification.
    """


class _EmailConfig(NamedTuple):
    """Resolved SMTP settings for one send."""
    host: str
    port: int
    sender: str
    recipient: str
    username: str | None
    password: str | None


def _email_config() -> _EmailConfig:
    """Resolve SMTP settings (DB first, env fallback).

    Raises EmailNotConfiguredError naming the missing required fields. An
    unparseable port falls back to 587, matching GET /api/settings/notifications.
    """
    host = _get_notifier_setting("smtp_host")
    sender = _get_notifier_setting("smtp_from")
    recipient = _get_notifier_setting("notify_email_to")
    missing = [
        label for label, val in (
            ("SMTP host", host),
            ("From address", sender),
            ("To address", recipient),
        ) if not val
    ]
    if missing:
        raise EmailNotConfiguredError(", ".join(missing))
    raw_port = _get_notifier_setting("smtp_port")
    try:
        port = int(raw_port) if raw_port else 587
    except (TypeError, ValueError):
        port = 587
    return _EmailConfig(
        host=host, port=port, sender=sender, recipient=recipient,
        username=_get_notifier_setting("smtp_username"),
        password=_get_notifier_setting("smtp_password"),
    )


def _deliver_email(cfg: _EmailConfig, subject: str, html: str) -> None:
    """Send one HTML email over SMTP. Raises on any failure.

    Uses STARTTLS when credentials are provided. Shared by the background
    notification path (which swallows errors) and the Settings test button
    (which surfaces them).
    """
    msg = MIMEText(html, "html")
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = cfg.recipient
    with smtplib.SMTP(cfg.host, cfg.port, timeout=10) as server:
        if cfg.username and cfg.password:
            server.starttls()
            server.login(cfg.username, cfg.password)
        server.send_message(msg)


def _send_email(subject: str, html: str) -> None:
    """Send an HTML email via SMTP. No-op if not configured.

    Synchronous (smtplib is blocking) - callers should run via
    `asyncio.to_thread(_send_email, ...)` to avoid blocking the event loop.
    Never raises: a mail failure must not abort the other channels.
    """
    try:
        cfg = _email_config()
    except EmailNotConfiguredError:
        return
    try:
        _deliver_email(cfg, subject, html)
    except Exception:
        logger.warning("email notification failed", exc_info=True)


def send_test_email() -> str:
    """Send a test email to verify the stored SMTP settings. Returns the
    recipient on success.

    Blocking (smtplib) - call via `asyncio.to_thread`. Unlike `_send_email`,
    every failure raises with a reason the user can act on; a Test button
    that silently logged its error would be worse than no button at all.
    """
    cfg = _email_config()
    html = (
        "<b>ECOVibe test email</b><br>"
        "Your SMTP settings are working - stock alerts will arrive here."
    )
    try:
        _deliver_email(cfg, "ECOVibe test email", html)
    except smtplib.SMTPAuthenticationError as e:
        raise EmailSendError(
            f"Authentication failed ({e.smtp_code}). Check the SMTP username "
            f"and password. Gmail and Outlook require an app password, not "
            f"your normal account password."
        ) from e
    except smtplib.SMTPRecipientsRefused as e:
        raise EmailSendError(f"The server refused the To address {cfg.recipient}.") from e
    except smtplib.SMTPSenderRefused as e:
        raise EmailSendError(f"The server refused the From address {cfg.sender}.") from e
    except smtplib.SMTPNotSupportedError as e:
        raise EmailSendError(f"The server does not support a required feature: {e}") from e
    except smtplib.SMTPException as e:
        raise EmailSendError(f"SMTP error from {cfg.host}:{cfg.port} - {e}") from e
    except (TimeoutError, OSError) as e:
        # Wrong host/port, firewall, or TLS-only port used without STARTTLS.
        raise EmailSendError(f"Could not connect to {cfg.host}:{cfg.port} - {e}") from e
    logger.info("test email sent to %s", cfg.recipient)
    return cfg.recipient


async def broadcast(
    subject: str, plain: str, html: str,
    fields: list[dict[str, Any]] | None = None,
) -> None:
    """Fan out one pre-formatted message to every configured channel.

    ``plain`` feeds Discord/Slack, ``html`` feeds Telegram/email (dynamic
    parts must already be escaped), ``fields`` optionally enriches the
    Discord embed. `return_exceptions=True` ensures one channel's failure
    cannot cancel the others; errors are logged inside each sender and
    this coroutine never raises.
    """
    await asyncio.gather(
        _send_telegram(html),
        _send_discord(subject, plain, fields),
        _send_slack(subject, plain),
        asyncio.to_thread(_send_email, subject, html),
        return_exceptions=True,
    )


async def notify_stock_alert(
    plan_code: str, fqns: list[str], price: float | None = None,
    currency_code: str = "EUR", account_label: str | None = None,
) -> None:
    """Fan out a stock alert to every configured channel concurrently.

    `price` must already be in whole currency units (not microcents) and
    `currency_code` should reflect the account the alert came from - callers
    must convert before calling this. `account_label` names the account the
    alert fired under (the poller watches them all).
    """
    channels = configured_channels()
    acct = _account_suffix(account_label)
    logger.info(
        "notifying stock alert for %s%s (%d config%s) via %s",
        plan_code, acct, len(fqns), "" if len(fqns) == 1 else "s",
        ", ".join(channels) if channels else "no channels",
    )
    plain, html = _format_message(
        plan_code, fqns, price, currency_code, account_label
    )
    fields = [
        {"name": "Plan", "value": f"`{plan_code}`", "inline": True},
        {"name": "Configs", "value": f"`{', '.join(fqns[:3])}`", "inline": True},
    ]
    if account_label:
        fields.append({"name": "Account", "value": account_label, "inline": True})
    await broadcast(
        f"OVH stock alert{acct}: {plan_code} available", plain, html, fields
    )


async def notify_price_drop(
    plan_code: str, price: float, threshold: float, currency_code: str = "EUR",
    account_label: str | None = None,
) -> None:
    """Notify that a plan's price dropped to/below the user's watch threshold.

    Both prices are in whole currency units (already divided out of
    microcents by the caller). `account_label` names the account whose
    catalog the price came from.
    """
    acct = _account_suffix(account_label)
    logger.info(
        "notifying price drop for %s%s: %.2f %s (threshold %.2f)",
        plan_code, acct, price, currency_code, threshold,
    )
    plain = (
        f"OVH price drop{acct}: {plan_code} is now {price:.2f} {currency_code} "
        f"(at or below your {threshold:.2f} {currency_code} watch)"
    )
    html = (
        f"<b>OVH price drop</b>{escape(acct)}: <code>{escape(plan_code)}</code>"
        f" is now <b>{price:.2f} {escape(currency_code)}</b> "
        f"(at or below your {threshold:.2f} {escape(currency_code)} watch)"
    )
    await broadcast(f"OVH price drop{acct}: {plan_code}", plain, html)


async def notify_promo(
    description: str, plan_codes: list[str], account_label: str | None = None,
) -> None:
    """Notify that OVH published a promotion, once per campaign.

    OVH attaches a campaign to every plan it covers, so one sale spans many
    plan codes (a recent flash sale hit 17). Callers group by campaign and
    pass the whole plan list here; notifying per plan sent 17 identical
    messages for a single offer.
    """
    codes = list(plan_codes)
    count = f"{len(codes)} plan{'' if len(codes) == 1 else 's'}"
    shown = ", ".join(codes[:5])
    extra = f" (+{len(codes) - 5} more)" if len(codes) > 5 else ""
    acct = _account_suffix(account_label)
    logger.info("notifying promo%s on %s: %s", acct, count, description[:120])
    plain = f"OVH promotion{acct} on {count}: {description}\nPlans: {shown}{extra}"
    html = (
        f"<b>OVH promotion</b>{escape(acct)} on {escape(count)}: "
        f"{escape(description)}<br>"
        f"<b>Plans</b>: <code>{escape(shown)}</code>{escape(extra)}"
    )
    await broadcast(f"OVH promotion{acct} on {count}", plain, html)


def _plan_list(rows: list[dict[str, Any]], limit: int = 5) -> str:
    """`"a, b, c (+2 more)"` from catalog-change rows, truncated like the
    stock/promo formatters so a big catalog shift can't overflow a chat
    client."""
    codes = [str(r.get("plan_code") or "") for r in rows]
    shown = ", ".join(codes[:limit])
    return shown + (f" (+{len(codes) - limit} more)" if len(codes) > limit else "")


async def notify_catalog_change(
    added: list[dict[str, Any]], removed: list[dict[str, Any]],
    account_label: str | None = None,
) -> None:
    """Notify that OVH added plans to, or removed plans from, the catalog.

    Called once per account per scan cycle with the whole diff — never once
    per plan. A range launch touches many plan codes at once, and per-plan
    messages would repeat the same news a dozen times (the bug that made
    promo notifications group by campaign).
    """
    acct = _account_suffix(account_label)
    counts = f"+{len(added)} / -{len(removed)} plan{'' if len(added) + len(removed) == 1 else 's'}"
    logger.info(
        "notifying catalog change%s: %d added, %d removed",
        acct, len(added), len(removed),
    )
    lines = []
    if added:
        lines.append(f"Added: {_plan_list(added)}")
    if removed:
        lines.append(f"Removed: {_plan_list(removed)}")
    plain = f"OVH catalog change{acct}: {counts}\n" + "\n".join(lines)
    html_lines = "<br>".join(
        f"<b>{label}</b>: <code>{escape(_plan_list(rows))}</code>"
        for label, rows in (("Added", added), ("Removed", removed)) if rows
    )
    html = (
        f"<b>OVH catalog change</b>{escape(acct)}: {escape(counts)}<br>{html_lines}"
    )
    fields = [
        {"name": label, "value": f"`{_plan_list(rows)}`", "inline": False}
        for label, rows in (("Added", added), ("Removed", removed)) if rows
    ]
    if account_label:
        fields.append({"name": "Account", "value": account_label, "inline": True})
    await broadcast(f"OVH catalog change{acct}: {counts}", plain, html, fields)


async def notify_order_status(
    order_id: int, name: str | None, status: str, previous: str | None,
    account_label: str | None = None,
) -> None:
    """Notify that an order reached a terminal state (delivered / cancelled).

    Called only for transitions the delivery watch considers newsworthy —
    intermediate churn (``checking`` → ``delivering``) is persisted and
    streamed to the browser but never fanned out, so a single order produces
    at most one message per outcome.
    """
    acct = _account_suffix(account_label)
    label = name or f"Order #{order_id}"
    logger.info(
        "notifying order %s%s: %s -> %s", order_id, acct, previous or "?", status,
    )
    headline = "Server delivered" if status == "delivered" else f"Order {status}"
    plain = (
        f"{headline}{acct}: {label}\n"
        f"Order #{order_id} — {previous or '?'} → {status}"
    )
    html = (
        f"<b>{escape(headline)}</b>{escape(acct)}: {escape(label)}<br>"
        f"Order <code>#{order_id}</code> — "
        f"{escape(previous or '?')} → <b>{escape(status)}</b>"
    )
    fields = [
        {"name": "Order", "value": f"`#{order_id}`", "inline": True},
        {"name": "Status", "value": status, "inline": True},
    ]
    if account_label:
        fields.append({"name": "Account", "value": account_label, "inline": True})
    await broadcast(f"{headline}{acct}: {label}", plain, html, fields)


def _server_list(names: list[str], limit: int = 5) -> str:
    """`"a, b, c (+2 more)"` from service names — same truncation rule as
    :func:`_plan_list`, so a bulk change can't overflow a chat client."""
    shown = ", ".join(names[:limit])
    return shown + (f" (+{len(names) - limit} more)" if len(names) > limit else "")


async def notify_server_change(
    added: list[str], removed: list[str], account_label: str | None = None,
) -> None:
    """Notify that dedicated servers appeared on, or vanished from, an account.

    Called once per account per scan cycle with the whole diff, never once per
    server — the same rule as :func:`notify_catalog_change`.
    """
    acct = _account_suffix(account_label)
    counts = f"+{len(added)} / -{len(removed)}"
    logger.info(
        "notifying server change%s: %d added, %d removed",
        acct, len(added), len(removed),
    )
    headline = "New OVH server" if added and not removed else "OVH servers changed"
    lines = []
    if added:
        lines.append(f"Added: {_server_list(added)}")
    if removed:
        lines.append(f"Removed: {_server_list(removed)}")
    plain = f"{headline}{acct}: {counts}\n" + "\n".join(lines)
    html_lines = "<br>".join(
        f"<b>{label}</b>: <code>{escape(_server_list(names))}</code>"
        for label, names in (("Added", added), ("Removed", removed)) if names
    )
    html = f"<b>{escape(headline)}</b>{escape(acct)}: {escape(counts)}<br>{html_lines}"
    fields = [
        {"name": label, "value": f"`{_server_list(names)}`", "inline": False}
        for label, names in (("Added", added), ("Removed", removed)) if names
    ]
    if account_label:
        fields.append({"name": "Account", "value": account_label, "inline": True})
    await broadcast(f"{headline}{acct}: {counts}", plain, html, fields)


def configured_channels() -> list[str]:
    """Return the names of channels that have all their required settings.

    Useful for the /health endpoint or a status panel to show which
    notification channels will actually fire. Reads from DB first, then env.
    """
    out = []
    if _get_notifier_setting("telegram_bot_token") and _get_notifier_setting("telegram_chat_id"):
        out.append("telegram")
    if _get_notifier_setting("discord_webhook_url"):
        out.append("discord")
    if _get_notifier_setting("slack_webhook_url"):
        out.append("slack")
    if (_get_notifier_setting("smtp_host") and _get_notifier_setting("notify_email_to")
            and _get_notifier_setting("smtp_from")):
        out.append("email")
    return out

