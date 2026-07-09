"""App configuration from env vars.

OVH credentials are stored in the DB (via the setup wizard), not env vars.
Non-secret settings like cache TTL, DB path, notifier config, etc. come
from env vars prefixed with OVH_.
"""
import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root (two levels up from this file: app/config.py -> app/ -> root).
# Used to resolve the default DB path so it doesn't depend on the CWD.
BASE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SUPPORTED_ENDPOINTS: dict[str, str] = {
    "ovh-eu": "OVHcloud Europe (IE, FR, DE, GB, ES, PL...)",
    "ovh-us": "OVHcloud US (US based services)",
    "ovh-ca": "OVHcloud Canada (CA based services)",
}


def _default_db_path() -> str:
    """Return an absolute path for the DB, anchored to the project root.

    Without this, the relative path 'ovh-flash-monitor.db' resolves
    relative to the CWD, so running from a different directory creates
    a new empty DB and the user must re-enter credentials.
    """
    return os.path.join(BASE_PATH, "ovh-flash-monitor.db")


class Settings(BaseSettings):
    """Non-secret runtime config from env vars."""

    model_config = SettingsConfigDict(env_prefix="OVH_", case_sensitive=False)

    # Default endpoint before credentials are saved to DB
    endpoint: str = "ovh-eu"

    # Server bind address. Defaults to localhost so `python run.py` is
    # never publicly reachable by accident. Set OVH_HOST=0.0.0.0 to expose
    # behind a reverse proxy (see README.md > Deployment).
    host: str = "127.0.0.1"
    port: int = 8000

    # Catalog cache
    use_cache: bool = False
    cache_ttl: int = 300

    # Persistence — absolute path so CWD doesn't matter
    db_path: str = _default_db_path()

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
