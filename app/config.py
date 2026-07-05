from functools import lru_cache
from typing import Dict, List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


SUPPORTED_ENDPOINTS: Dict[str, str] = {
    "ovh-eu": "OVHcloud Europe (IE, FR, DE, GB, ES, PL...)",
    "ovh-us": "OVHcloud US (US based services)",
    "ovh-ca": "OVHcloud Canada (CA based services)",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OVH_", case_sensitive=False)

    endpoint: str = "ovh-eu"
    application_key: Optional[str] = None
    application_secret: Optional[str] = None
    consumer_key: Optional[str] = None
    use_cache: bool = False
    cache_ttl: int = 300
    db_path: str = "ovh-flash-monitor.db"
    cors_origins: List[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()
