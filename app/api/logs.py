"""Runtime log viewer: recent-log snapshot + SSE live tail.

Reads from the in-memory `LogBus` ring buffer (see app/services/logbus.py),
which is fed by the file/ring-buffer handlers installed in
app/logging_config.py. The durable record is the rotating log file.
"""
import asyncio
import json
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.services.logbus import get_log_bus

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def list_logs(
    limit: int = Query(default=200, ge=1, le=5000),
    level: str = Query(default="all"),
    source: str = Query(default="all"),
    search: str = Query(default=""),
) -> dict[str, Any]:
    """Return recent log entries (oldest-first) plus the known source labels.

    `level` filters to that level and above; `source` is an exact match;
    `search` is a case-insensitive substring of the message.
    """
    bus = get_log_bus()
    return {
        "logs": bus.recent(limit=limit, level=level, source=source, search=search),
        "sources": bus.sources(),
    }


@router.get("/stream")
async def stream_logs() -> StreamingResponse:
    """Server-Sent Events stream of new log entries.

    Mirrors the monitor stream: each client gets its own bounded queue that the
    log handler pushes to; the generator runs forever and the `finally` block
    deregisters the queue on disconnect.
    """
    bus = get_log_bus()
    queue = await bus.subscribe()

    async def event_generator():
        try:
            while True:
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # SSE keep-alive comment (no client-visible event).
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(entry)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            await bus.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
