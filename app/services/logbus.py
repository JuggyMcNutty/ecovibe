"""In-memory log bus for the webui Logs tab.

A bounded ring buffer of recent log records plus an SSE pub/sub, fed by a
`LogBusHandler` attached to the `app.*` and `uvicorn.error` loggers (see
`app/logging_config.py`). The webui reads a snapshot via `GET /api/logs` and
live-tails via `GET /api/logs/stream`.

Records are emitted from any thread (e.g. OVH calls run under
`asyncio.to_thread`), so the buffer is guarded by a `threading.Lock` and
fan-out to the loop-bound asyncio queues goes through
`loop.call_soon_threadsafe`. The durable record is the rotating log file, not
this buffer — restarting the server clears it.
"""
import asyncio
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

# Map level names to numeric severity so the `level` filter means
# "this level and above" (e.g. WARNING shows WARNING + ERROR).
_LEVELS = logging.getLevelNamesMapping()


def _short_source(name: str) -> str:
    """Derive a compact source label from a logger name.

    `app.services.monitor` -> `monitor`, `app.api.checkout` -> `checkout`,
    `uvicorn.error` -> `uvicorn`. Anything else is returned unchanged.
    """
    if name.startswith("uvicorn"):
        return "uvicorn"
    if name.startswith("app."):
        return name.rsplit(".", 1)[-1]
    return name


class LogBus:
    """Ring buffer of recent log entries + SSE fan-out to live tailers."""

    def __init__(self, maxlen: int | None = None) -> None:
        if maxlen is None:
            maxlen = get_settings().log_buffer_size
        self._buffer: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._subscribers: list[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the running event loop so off-loop emits can fan out.

        Called from the FastAPI lifespan startup. Until it is set, the buffer
        still fills but live SSE delivery is a no-op.
        """
        self._loop = loop

    # -- producer side (called from the logging handler, any thread) --------

    def publish(self, entry: dict[str, Any]) -> None:
        """Append an entry to the buffer and fan it out to live tailers."""
        with self._lock:
            self._buffer.append(entry)
            subscribers = list(self._subscribers)
        loop = self._loop
        if loop is not None and subscribers:
            loop.call_soon_threadsafe(self._deliver, subscribers, entry)

    def _deliver(self, subscribers: list[asyncio.Queue], entry: dict[str, Any]) -> None:
        """Push an entry to each subscriber queue (runs on the loop thread)."""
        for q in subscribers:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                # Drop for a slow tailer rather than blocking the loop
                # (mirrors the monitor's slow-subscriber handling).
                pass

    # -- consumer side (SSE, called from the event loop) --------------------

    async def subscribe(self) -> asyncio.Queue:
        """Register a live tailer. Returns the queue it should await."""
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        """Deregister a queue when its SSE client disconnects."""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # -- snapshot / query ---------------------------------------------------

    def recent(
        self,
        limit: int = 200,
        level: str | None = None,
        source: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return up to `limit` most-recent entries (oldest-first) matching filters.

        `level` filters to that level and above; `source` is an exact match;
        `search` is a case-insensitive substring of the message.
        """
        with self._lock:
            entries = list(self._buffer)

        min_level = None
        if level and level.upper() != "ALL":
            min_level = _LEVELS.get(level.upper())
        needle = search.lower() if search else None

        matched = []
        for e in entries:
            if min_level is not None and _LEVELS.get(e["level"], 0) < min_level:
                continue
            if source and source != "all" and e["source"] != source:
                continue
            if needle and needle not in e["message"].lower():
                continue
            matched.append(e)

        return matched[-limit:]

    def sources(self) -> list[str]:
        """Return the distinct source labels currently in the buffer, sorted."""
        with self._lock:
            return sorted({e["source"] for e in self._buffer})

    def clear(self) -> None:
        """Drop all buffered entries (used by tests)."""
        with self._lock:
            self._buffer.clear()


class LogBusHandler(logging.Handler):
    """Logging handler that feeds records into a `LogBus`."""

    def __init__(self, bus: "LogBus") -> None:
        super().__init__()
        self._bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{logging.Formatter().formatException(record.exc_info)}"
            entry = {
                "ts": datetime.fromtimestamp(
                    record.created, tz=timezone.utc
                ).isoformat(),
                "level": record.levelname,
                "source": _short_source(record.name),
                "message": message,
            }
            self._bus.publish(entry)
        except Exception:  # never let logging raise
            self.handleError(record)


_log_bus: LogBus | None = None


def get_log_bus() -> LogBus:
    global _log_bus
    if _log_bus is None:
        _log_bus = LogBus()
    return _log_bus
