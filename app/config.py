"""Application configuration loaded from environment variables.

All variables are prefixed with `OVH_` (e.g. `OVH_APPLICATION_KEY`).
The settings instance is cached via `lru_cache` — call `get_settings.cache_clear()`
in tests after monkeypatching env vars.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Maps each supported OVH region to a human-readable label. Used by the
# credentials setup view in the frontend.
SUPPORTED_ENDPOINTS: dict[str, str] = {
    "ovh-eu": "OVHcloud Europe (IE, FR, DE, GB, ES, PL...)",
    "ovh-us": "OVHcloud US (US based services)",
    "ovh-ca": "OVHcloud Canada (CA based services)",
}


class Settings(BaseSettings):
    """Runtime configuration. All fields are optional with sensible defaults.

    OVH API:
        endpoint:            Region (ovh-eu / ovh-us / ovh-ca).
        application_key:     OVH application key. Required to call the API.
        application_secret:  OVH application secret.
        consumer_key:        OVH consumer key (per-token authorisation).

    Caching:
        use_cache:   Toggle in-memory catalog caching (default off — flash-sale
                     use case wants fresh data).
        cache_ttl:   Seconds before a cached catalog entry is considered stale.

    Persistence:
        db_path:     SQLite database path for alerts, profiles, orders, etc.

    CORS:
        cors_origins:  List of allowed origins for cross-origin requests.
                        Empty (default) = same-origin only.

    Notifier (all optional — leave blank to disable a channel):
        telegram_bot_token / telegram_chat_id
        discord_webhook_url
        slack_webhook_url
        smtp_host / smtp_port / smtp_username / smtp_password / smtp_from
        notify_email_to
    """

    model_config = SettingsConfigDict(env_prefix="OVH_", case_sensitive=False)

    # OVH API credentials
    endpoint: str = "ovh-eu"
    application_key: str | None = None
    application_secret: str | None = None
    consumer_key: str | None = None

    # Catalog cache
    use_cache: bool = False
    cache_ttl: int = 300

    # Persistence
    db_path: str = "ovh-flash-monitor.db"

    # CORS
    cors_origins: list[str] = []

    # Multi-channel notifier config (all optional)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None
    slack_webhook_url: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    notify_email_to: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance.

    The cache means environment variables are read once per process. Tests
    that change env vars must call `get_settings.cache_clear()` afterwards.
    """
    return Settings()
