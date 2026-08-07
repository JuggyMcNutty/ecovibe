"""Runtime settings APIs: notification channels + app-wide options."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.app_settings import (
    APP_SETTINGS,
    get_effective,
    validate_value,
)
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


@router.post("/notifications/test-email")
async def test_email() -> dict:
    """Send a test email using the saved SMTP settings.

    Tests what is *stored*, not what is in the form - the UI saves before
    calling this. That keeps the tested config identical to the one real
    alerts will use, and means a masked password round-tripped from GET
    resolves to the real stored secret instead of being sent as literal
    asterisks.
    """
    from app.services import notifier
    try:
        recipient = await asyncio.to_thread(notifier.send_test_email)
    except notifier.EmailNotConfiguredError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Email is not fully configured. Missing: {e}",
        ) from e
    except notifier.EmailSendError as e:
        # 502: the app is fine, the upstream mail server rejected us.
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"status": "sent", "recipient": recipient}


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


# ---------------------------------------------------------------------------
# App-wide options (Settings → App). Backed by the app_settings registry:
# stored under app_* keys, env fallback, validated ranges, rebuild hooks.
# ---------------------------------------------------------------------------


class AppSettingsUpdate(BaseModel):
    """Body for PUT /api/settings/app — full replace of every option."""
    monitor_enabled: bool
    price_check_interval: int
    stock_event_retention_days: int
    stock_event_max_rows: int
    catalog_watch_enabled: bool
    catalog_watch_notify: bool
    order_check_interval: int
    use_cache: bool
    cache_ttl: int
    log_level: str
    log_file_max_bytes: int
    log_backup_count: int
    log_buffer_size: int
    ui_alert_autohide_ms: int
    ui_orders_days: int
    ui_orders_limit: int
    ui_logs_limit: int
    ui_region_feed_cap: int
    ui_recent_alerts_shown: int


@router.get("/app")
async def get_app_settings() -> dict:
    """Effective app options (DB-first, env fallback) + the read-only
    env-only values (shown greyed out; changing them needs a restart)."""
    from app.config import get_settings
    env = get_settings()
    return {
        "settings": {key: get_effective(key) for key in APP_SETTINGS},
        "env": {
            "host": env.host,
            "port": env.port,
            "db_path": env.db_path,
            "log_file": env.log_file,
            "cors_origins": env.cors_origins,
        },
    }


@router.put("/app")
async def update_app_settings(request: AppSettingsUpdate) -> dict:
    """Validate and persist all app options, then apply rebuild hooks.

    All-or-nothing: every value is validated before anything is written,
    so a single bad field can't leave the settings half-saved. Hooks run
    only for groups whose effective value actually changed; the response
    lists them so the UI can say what was rebuilt.
    """
    # 1. Validate everything up front.
    validated: dict[str, object] = {}
    for key in APP_SETTINGS:
        try:
            validated[key] = validate_value(key, getattr(request, key))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    # 2. Snapshot old effective values to detect which hooks must run.
    old = {key: get_effective(key) for key in APP_SETTINGS}

    # 3. Persist. Bools stored as "true"/"false" (parsed by app_setting_bool).
    storage = get_storage()
    for key, val in validated.items():
        raw = str(val).lower() if isinstance(val, bool) else str(val)
        storage.set_setting(f"app_{key}", raw)

    # 4. Refresh the env-settings cache (parity with the notifications PUT).
    from app.config import get_settings
    get_settings.cache_clear()

    # 5. Rebuild hooks for values frozen into long-lived objects.
    applied: list[str] = []
    # Start/stop the poller immediately, so turning monitoring off is not a
    # dead end that needs a restart to undo.
    if old["monitor_enabled"] != validated["monitor_enabled"]:
        from app.services.monitor import get_monitor_service
        monitor = get_monitor_service()
        if validated["monitor_enabled"]:
            await monitor.start()
            applied.append("stock monitor started")
        else:
            await monitor.stop()
            applied.append("stock monitor stopped")
    if (old["use_cache"] != validated["use_cache"]
            or old["cache_ttl"] != validated["cache_ttl"]):
        from app.services.cache import get_cache
        from app.services.ovh_service import reset_ovh_service
        reset_ovh_service(None)
        cache = get_cache(ttl=int(validated["cache_ttl"]))
        cache.clear()
        applied.append("catalog cache rebuilt")
    if any(old[k] != validated[k] for k in
           ("log_level", "log_file_max_bytes", "log_backup_count")):
        from app.logging_config import setup_logging
        setup_logging()
        applied.append(f"log config applied (level {validated['log_level']})")
    if old["log_buffer_size"] != validated["log_buffer_size"]:
        from app.services.logbus import get_log_bus
        get_log_bus().resize(int(validated["log_buffer_size"]))
        applied.append("log buffer resized")

    logger.info("App settings updated (%s)", "; ".join(applied) or "no hooks")
    return {"status": "saved", "applied": applied}
