from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_ENDPOINTS: dict[str, str] = {
    "ovh-eu": "OVHcloud Europe (IE, FR, DE, GB, ES, PL...)",
    "ovh-us": "OVHcloud US (US based services)",
    "ovh-ca": "OVHcloud Canada (CA based services)",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OVH_", case_sensitive=False)

    endpoint: str = "ovh-eu"
    application_key: str | None = None
    application_secret: str | None = None
    consumer_key: str | None = None
    use_cache: bool = False
    cache_ttl: int = 300
    db_path: str = "ovh-flash-monitor.db"
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
    return Settings()
