"""App configuration from env vars.

OVH credentials are stored in the DB (via the setup wizard), not env vars.
Non-secret settings like cache TTL, DB path, notifier config, etc. come
from env vars prefixed with OVH_.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_ENDPOINTS: dict[str, str] = {
    "ovh-eu": "OVHcloud Europe (IE, FR, DE, GB, ES, PL...)",
    "ovh-us": "OVHcloud US (US based services)",
    "ovh-ca": "OVHcloud Canada (CA based services)",
}


class Settings(BaseSettings):
    """Non-secret runtime config from env vars."""

    model_config = SettingsConfigDict(env_prefix="OVH_", case_sensitive=False)

    # Default endpoint before credentials are saved to DB
    endpoint: str = "ovh-eu"

    # Catalog cache
    use_cache: bool = False
    cache_ttl: int = 300

    # Persistence
    db_path: str = "ovh-flash-monitor.db"

    # CORS
    cors_origins: list[str] = []

    # Notifier (all optional)
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
    """Cached settings. Call get_settings.cache_clear() in tests."""
    return Settings()
