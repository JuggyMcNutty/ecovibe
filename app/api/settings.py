"""Notification settings - configure Telegram, Discord, Slack, SMTP from the UI."""
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Keys stored in the DB settings table, mapped to their pydantic field names.
NOTIFIER_KEYS = [
    "telegram_bot_token",
    "telegram_chat_id",
    "discord_webhook_url",
    "slack_webhook_url",
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_from",
    "notify_email_to",
]


class NotifierSettings(BaseModel):
    """Notifier configuration for all channels.

    Empty strings mean 'not configured'. Secrets (tokens, passwords) are
    returned masked on GET so the browser never receives the full value.
    """
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    slack_webhook_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notify_email_to: str = ""


def _mask(value: str) -> str:
    """Mask a secret, showing only first 4 and last 4 characters.

    Always includes "..." in the output, even for short secrets, so
    `update_notifications`'s `"..." in val` check can reliably recognize
    a round-tripped masked value and skip overwriting the real stored
    secret with the mask itself.
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "****...****"
    return f"{value[:4]}...{value[-4:]}"


# Which fields are secrets (masked on GET)
_SECRET_FIELDS = {"telegram_bot_token", "smtp_password", "discord_webhook_url", "slack_webhook_url"}


@router.get("/notifications")
async def get_notifications() -> dict:
    """Return current notifier settings, with secrets masked."""
    storage = get_storage()
    settings = {}
    for key in NOTIFIER_KEYS:
        raw = storage.get_setting(f"notifier_{key}")
        if raw is not None:
            if key == "smtp_port":
                try:
                    settings[key] = int(raw)
                except ValueError:
                    settings[key] = 587
            else:
                settings[key] = raw
    # Apply env var defaults for fields not in DB
    from app.config import get_settings
    env = get_settings()
    for key in NOTIFIER_KEYS:
        if key not in settings:
            val = getattr(env, key, None)
            if val is not None:
                settings[key] = val
    # Mask secrets
    for key in _SECRET_FIELDS:
        if key in settings and settings[key]:
            settings[key] = _mask(settings[key])
    return {"settings": settings, "configured": _configured_channels(settings)}


@router.put("/notifications")
async def update_notifications(request: NotifierSettings) -> dict:
    """Save notifier settings to the database.

    Empty strings clear a field. Masked values (containing '...') are
    preserved as-is (not overwritten) so the user can update other
    fields without re-entering secrets each time.
    """
    storage = get_storage()
    for key in NOTIFIER_KEYS:
        val = getattr(request, key)
        if key in _SECRET_FIELDS and isinstance(val, str) and "..." in val:
            # Skip masked values - don't overwrite the stored secret
            continue
        if key == "smtp_port":
            storage.set_setting(f"notifier_{key}", str(val))
        elif val:
            storage.set_setting(f"notifier_{key}", val)
        else:
            # Clear empty fields
            storage.set_setting(f"notifier_{key}", "")
    # Reload settings cache so the notifier picks up new values
    from app.config import get_settings
    get_settings.cache_clear()
    logger.info("Notifier settings updated")
    return {"status": "saved"}


def _configured_channels(settings: dict) -> list[str]:
    """Return names of channels that have all required settings."""
    out = []
    if settings.get("telegram_bot_token") and settings.get("telegram_chat_id"):
        out.append("telegram")
    if settings.get("discord_webhook_url"):
        out.append("discord")
    if settings.get("slack_webhook_url"):
        out.append("slack")
    if settings.get("smtp_host") and settings.get("notify_email_to") and settings.get("smtp_from"):
        out.append("email")
    return out
