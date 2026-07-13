"""App configuration from env vars.

OVH credentials are stored in the DB (via the setup wizard), not env vars.
Non-secret settings like cache TTL, DB path, notifier config, etc. come
from env vars prefixed with OVH_.
"""
import json
import os
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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


def _default_log_path() -> str:
    """Return an absolute path for the log file, anchored to the project root.

    Mirrors `_default_db_path()` so the rotating log file lands next to the
    DB regardless of the CWD the server is started from.
    """
    return os.path.join(BASE_PATH, "ecovibe.log")


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

    # CORS. `NoDecode` disables pydantic-settings' default JSON parsing so the
    # validator below can accept the README's comma-separated form (a bare
    # "a,b" is not valid JSON and would otherwise crash startup).
    cors_origins: Annotated[list[str], NoDecode] = []

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v: object) -> object:
        """Accept a comma-separated string OR a JSON array for CORS origins."""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

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

    # Logging. `app.*` and `uvicorn.error` records flow to a rotating file
    # (durable) and an in-memory ring buffer (fed to the webui Logs tab).
    # See app/logging_config.py and app/services/logbus.py.
    log_level: str = "INFO"
    log_file: str = _default_log_path()
    log_file_max_bytes: int = 5_000_000
    log_backup_count: int = 3
    log_buffer_size: int = 5000


@lru_cache
def get_settings() -> Settings:
    """Cached settings. Call get_settings.cache_clear() in tests."""
    return Settings()
