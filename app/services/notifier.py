"""Multi-channel notifier: Telegram, Discord, Slack, email.

All channels are optional and configured via environment variables.
Failures in any channel are logged but do not affect others.
"""
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def _format_message(plan_code: str, fqns: list[str], price: float | None = None) -> tuple[str, str]:
    """Return (plain, html) message forms."""
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
    settings = get_settings()
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    plain, html = _format_message(plan_code, fqns, price)
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
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
    settings = get_settings()
    if not settings.discord_webhook_url:
        return
    plain, _ = _format_message(plan_code, fqns, price)
    embed: dict[str, Any] = {
        "title": "\U0001F514 OVH Stock Alert",
        "description": plain,
        "color": 0x60A5FA,
        "fields": [
            {"name": "Plan", "value": f"`{plan_code}`", "inline": True},
            {"name": "Configs", "value": f"`{', '.join(fqns[:3])}`", "inline": True},
        ],
    }
    payload = {"embeds": [embed]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(settings.discord_webhook_url, json=payload)
            if r.status_code not in (200, 204):
                logger.warning("discord responded %s: %s", r.status_code, r.text[:200])
    except Exception:
        logger.warning("discord notification failed", exc_info=True)


async def _send_slack(plan_code: str, fqns: list[str], price: float | None) -> None:
    settings = get_settings()
    if not settings.slack_webhook_url:
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
            r = await client.post(settings.slack_webhook_url, json=payload)
            if r.status_code != 200:
                logger.warning("slack responded %s: %s", r.status_code, r.text[:200])
    except Exception:
        logger.warning("slack notification failed", exc_info=True)


def _send_email(plan_code: str, fqns: list[str], price: float | None) -> None:
    settings = get_settings()
    if not (settings.smtp_host and settings.notify_email_to and settings.smtp_from):
        return
    plain, html = _format_message(plan_code, fqns, price)
    msg = MIMEText(html, "html")
    msg["Subject"] = f"OVH stock alert: {plan_code} available"
    msg["From"] = settings.smtp_from
    msg["To"] = settings.notify_email_to
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            if settings.smtp_username and settings.smtp_password:
                server.starttls()
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
    except Exception:
        logger.warning("email notification failed", exc_info=True)


async def notify_stock_alert(
    plan_code: str, fqns: list[str], price: float | None = None
) -> None:
    """Fan out a stock alert to all configured channels. Non-blocking, fault-tolerant."""
    await asyncio.gather(
        _send_telegram(plan_code, fqns, price),
        _send_discord(plan_code, fqns, price),
        _send_slack(plan_code, fqns, price),
        asyncio.to_thread(_send_email, plan_code, fqns, price),
        return_exceptions=True,
    )


def configured_channels() -> list[str]:
    """Return list of configured channel names (for /health or status display)."""
    s = get_settings()
    out = []
    if s.telegram_bot_token and s.telegram_chat_id:
        out.append("telegram")
    if s.discord_webhook_url:
        out.append("discord")
    if s.slack_webhook_url:
        out.append("slack")
    if s.smtp_host and s.notify_email_to and s.smtp_from:
        out.append("email")
    return out
