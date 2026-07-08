"""Multi-channel notifier: Telegram, Discord, Slack, email.

Settings are read from the DB settings table (set via the Settings UI)
with env var fallback for initial setup. The helper ``_get_notifier_setting``
checks DB first, then env.
"""
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from typing import Any

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


def _format_message(plan_code: str, fqns: list[str], price: float | None = None) -> tuple[str, str]:
    """Build the alert text in both plain-text and HTML forms.

    `price` is the latest known price in euros (or None if unavailable).
    Truncates the FQN list to the first 5 entries with a "+N more" suffix
    to avoid overflowing chat clients on plans with many configs.
    """
    fqn_list = ", ".join(fqns[:5])
    extra = f" (+{len(fqns) - 5} more)" if len(fqns) > 5 else ""
    price_str = f" at \u20AC{price:.2f}" if price is not None else ""
    plain = f"OVH stock alert: {plan_code} now available{price_str}\nConfigs: {fqn_list}{extra}"
    html = (
        f"<b>OVH stock alert</b>: <code>{plan_code}</code> now available{price_str}<br>"
        f"<b>Configs</b>: <code>{fqn_list}</code>{extra}"
    )
    return plain, html


async def _send_telegram(plan_code: str, fqns: list[str], price: float | None) -> None:
    """Send a Telegram message via the Bot API. No-op if not configured."""
    token = _get_notifier_setting("telegram_bot_token")
    chat_id = _get_notifier_setting("telegram_chat_id")
    if not (token and chat_id):
        return
    plain, _ = _format_message(plan_code, fqns, price)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": plain,
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


async def _send_discord(plan_code: str, fqns: list[str], price: float | None) -> None:
    """Post a Discord webhook with a rich embed. No-op if not configured."""
    webhook_url = _get_notifier_setting("discord_webhook_url")
    if not webhook_url:
        return
    plain, _ = _format_message(plan_code, fqns, price)
    embed: dict[str, Any] = {
        "title": "\U0001F514 OVH Stock Alert",
        "description": plain,
        "color": 0x60A5FA,  # tailwind blue-400
        "fields": [
            {"name": "Plan", "value": f"`{plan_code}`", "inline": True},
            {"name": "Configs", "value": f"`{', '.join(fqns[:3])}`", "inline": True},
        ],
    }
    payload = {"embeds": [embed]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook_url, json=payload)
            # Discord returns 204 on success.
            if r.status_code not in (200, 204):
                logger.warning("discord responded %s: %s", r.status_code, r.text[:200])
    except Exception:
        logger.warning("discord notification failed", exc_info=True)


async def _send_slack(plan_code: str, fqns: list[str], price: float | None) -> None:
    """Post to a Slack incoming webhook. No-op if not configured."""
    webhook_url = _get_notifier_setting("slack_webhook_url")
    if not webhook_url:
        return
    plain, _ = _format_message(plan_code, fqns, price)
    payload = {
        "text": f"\U0001F514 {plain}",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*OVH Stock Alert*\n{plain}"},
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


def _send_email(plan_code: str, fqns: list[str], price: float | None) -> None:
    """Send an HTML email via SMTP. No-op if not configured.

    Synchronous (smtplib is blocking) - callers should run via
    `asyncio.to_thread(_send_email, ...)` to avoid blocking the event loop.
    Uses STARTTLS when SMTP credentials are provided.
    """
    smtp_host = _get_notifier_setting("smtp_host")
    smtp_port = _get_notifier_setting("smtp_port")
    smtp_from = _get_notifier_setting("smtp_from")
    notify_to = _get_notifier_setting("notify_email_to")
    if not (smtp_host and notify_to and smtp_from):
        return
    _, html = _format_message(plan_code, fqns, price)
    msg = MIMEText(html, "html")
    msg["Subject"] = f"OVH stock alert: {plan_code} available"
    msg["From"] = smtp_from
    msg["To"] = notify_to
    try:
        port = int(smtp_port) if smtp_port else 587
        with smtplib.SMTP(smtp_host, port, timeout=10) as server:
            smtp_username = _get_notifier_setting("smtp_username")
            smtp_password = _get_notifier_setting("smtp_password")
            if smtp_username and smtp_password:
                server.starttls()
                server.login(smtp_username, smtp_password)
            server.send_message(msg)
    except Exception:
        logger.warning("email notification failed", exc_info=True)


async def notify_stock_alert(
    plan_code: str, fqns: list[str], price: float | None = None
) -> None:
    """Fan out a stock alert to every configured channel concurrently.

    `return_exceptions=True` ensures one channel's failure cannot cancel the
    others. Errors are logged inside each sender; this coroutine never raises.
    """
    await asyncio.gather(
        _send_telegram(plan_code, fqns, price),
        _send_discord(plan_code, fqns, price),
        _send_slack(plan_code, fqns, price),
        asyncio.to_thread(_send_email, plan_code, fqns, price),
        return_exceptions=True,
    )


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

