"""Install the file + in-app log handlers.

`setup_logging()` attaches a rotating file handler (durable) and a
`LogBusHandler` (feeds the webui Logs tab) to the `app` and `uvicorn.error`
loggers. It is called from both `create_app()` (so capture works in tests and
before the server starts) and the lifespan startup (so it re-attaches after
uvicorn applies its own dict-config, which otherwise strips handlers off the
loggers it configures). It is idempotent: our handlers are tagged and refreshed
rather than duplicated.

`uvicorn.access` is deliberately left alone — per-request access logs would
flood the viewer.
"""
import logging
from logging.handlers import RotatingFileHandler

from app.config import get_settings
from app.services.app_settings import app_setting_int, app_setting_str
from app.services.logbus import LogBusHandler, get_log_bus

# Loggers we mirror into the file + ring buffer.
_TARGET_LOGGERS = ("app", "uvicorn.error")

# Marks handlers this module owns so we can refresh them idempotently.
_TAG = "_ecovibe_log_handler"

_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging() -> None:
    """Attach (or refresh) the file + LogBus handlers on the target loggers.

    Level and rotation settings are read DB-first (Settings → App) with
    env fallback; the Settings PUT hook re-runs this function so changes
    apply at runtime. ``log_file`` (the path) stays env-only — moving the
    log file requires a restart.
    """
    settings = get_settings()
    level = app_setting_str("log_level").upper()

    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=app_setting_int("log_file_max_bytes"),
        backupCount=app_setting_int("log_backup_count"),
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    file_handler.setLevel(level)
    setattr(file_handler, _TAG, True)

    logbus_handler = LogBusHandler(get_log_bus())
    logbus_handler.setLevel(level)
    setattr(logbus_handler, _TAG, True)

    for name in _TARGET_LOGGERS:
        lg = logging.getLogger(name)
        # Drop any handlers we attached previously (avoids duplicates and
        # stale open file handles if setup runs more than once).
        for existing in [h for h in lg.handlers if getattr(h, _TAG, False)]:
            lg.removeHandler(existing)
            existing.close()
        lg.addHandler(file_handler)
        lg.addHandler(logbus_handler)

    # Raise the app logger to the configured level so DEBUG can be enabled
    # (its default effective level would otherwise suppress INFO/DEBUG).
    logging.getLogger("app").setLevel(level)
