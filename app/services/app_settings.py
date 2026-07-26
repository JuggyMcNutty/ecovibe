"""App-wide runtime settings: DB-first with env-var fallback.

The single source of truth for options exposed on the Settings → App
page. Each option lives in the ``APP_SETTINGS`` registry and is stored
in the DB ``settings`` table under an ``app_`` prefix (mirroring the
notifier's ``notifier_`` convention). Reads go DB-first, then fall back
to the env-backed ``Settings`` field of the same name (or a plain
default for UI-only preferences).

IMPORTANT: code must read these keys through the ``app_setting_*``
helpers here — never via ``get_settings()`` directly. The lru-cached
Settings object only sees environment variables; ``cache_clear()``
does not (and cannot) pick up DB overrides.
"""
import logging
from dataclasses import dataclass
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AppSetting:
    """One configurable option: where it comes from and what's valid."""

    key: str                              # helper key; DB row is f"app_{key}"
    type: str                             # "int" | "bool" | "str"
    group: str                            # "monitoring" | "cache" | "logging" | "ui"
    default: Any = None                   # used when env_backed is False
    env_backed: bool = True               # fallback = getattr(Settings, key)
    min: int | None = None
    max: int | None = None
    choices: tuple[str, ...] | None = None
    allow_zero: bool = False              # value may be 0 even if min > 0


APP_SETTINGS: dict[str, AppSetting] = {s.key: s for s in (
    # Monitoring & data — read live each monitor cycle.
    # monitor_enabled is read once at startup (app/main.py lifespan) and
    # applied immediately via the PUT hook when toggled, so the stored value
    # is both the boot state and the live state.
    AppSetting("monitor_enabled", "bool", "monitoring"),
    AppSetting("price_check_interval", "int", "monitoring",
               min=60, max=86_400, allow_zero=True),
    AppSetting("stock_event_retention_days", "int", "monitoring",
               min=1, max=3650),
    AppSetting("stock_event_max_rows", "int", "monitoring",
               min=1000, max=10_000_000),
    # Catalog cache — applied via the PUT hook (service registry reset).
    AppSetting("use_cache", "bool", "cache"),
    AppSetting("cache_ttl", "int", "cache", min=10, max=86_400),
    # Logging — applied via setup_logging() / LogBus.resize().
    AppSetting("log_level", "str", "logging",
               choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    AppSetting("log_file_max_bytes", "int", "logging",
               min=100_000, max=1_000_000_000),
    AppSetting("log_backup_count", "int", "logging", min=0, max=50),
    AppSetting("log_buffer_size", "int", "logging", min=100, max=100_000),
    # UI preferences — consumed by the frontend only (env_backed=False).
    AppSetting("ui_alert_autohide_ms", "int", "ui", default=30_000,
               env_backed=False, min=1000, max=600_000, allow_zero=True),
    AppSetting("ui_orders_days", "int", "ui", default=90,
               env_backed=False, min=1, max=365),
    AppSetting("ui_orders_limit", "int", "ui", default=50,
               env_backed=False, min=1, max=200),
    AppSetting("ui_logs_limit", "int", "ui", default=1000,
               env_backed=False, min=50, max=5000),  # logs.py caps at 5000
    AppSetting("ui_region_feed_cap", "int", "ui", default=100,
               env_backed=False, min=10, max=1000),
    AppSetting("ui_recent_alerts_shown", "int", "ui", default=5,
               env_backed=False, min=1, max=50),
)}


def _raw(key: str) -> str | None:
    """Read the DB override for a key, or None when unset/empty.

    Best-effort like the notifier's ``_get_notifier_setting``: a broken
    storage layer must never take down a settings read.
    """
    try:
        from app.services.storage import get_storage
        val = get_storage().get_setting(f"app_{key}")
        if val is not None and val != "":
            return val
    except Exception:
        pass
    return None


def _fallback(spec: AppSetting) -> Any:
    if spec.env_backed:
        return getattr(get_settings(), spec.key)
    return spec.default


def app_setting_int(key: str) -> int:
    spec = APP_SETTINGS[key]
    raw = _raw(key)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            logger.warning("app setting %s has non-int value %r; using fallback", key, raw)
    return int(_fallback(spec))


def app_setting_bool(key: str) -> bool:
    spec = APP_SETTINGS[key]
    raw = _raw(key)
    if raw is not None:
        return raw.strip().lower() in _TRUE_VALUES
    return bool(_fallback(spec))


def app_setting_str(key: str) -> str:
    spec = APP_SETTINGS[key]
    raw = _raw(key)
    if raw is not None:
        return raw
    return str(_fallback(spec))


def get_effective(key: str) -> Any:
    """The value currently in force for a key (typed), DB-first."""
    spec = APP_SETTINGS[key]
    if spec.type == "int":
        return app_setting_int(key)
    if spec.type == "bool":
        return app_setting_bool(key)
    return app_setting_str(key)


def validate_value(key: str, value: Any) -> Any:
    """Validate a proposed value against the registry. Returns the value
    (normalised for str choices) or raises ValueError with a message
    suitable for a 422 response."""
    spec = APP_SETTINGS[key]
    if spec.type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key}: expected a boolean")
        return value
    if spec.type == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{key}: expected an integer")
        if value == 0 and spec.allow_zero:
            return value
        if spec.min is not None and value < spec.min:
            raise ValueError(
                f"{key}: must be at least {spec.min}"
                + (" (or 0 to disable)" if spec.allow_zero else "")
            )
        if spec.max is not None and value > spec.max:
            raise ValueError(f"{key}: must be at most {spec.max}")
        return value
    # str
    normalised = str(value).strip().upper() if spec.choices else str(value)
    if spec.choices and normalised not in spec.choices:
        raise ValueError(f"{key}: must be one of {', '.join(spec.choices)}")
    return normalised
