"""FastAPI app factory and lifespan management."""
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse
from starlette.types import Scope

from app.utils.cache_buster import get_file_hash

logger = logging.getLogger(__name__)

# Project root (one level up from app/). Used to find static/ and templates/.
BASE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

templates = Jinja2Templates(directory=os.path.join(BASE_PATH, "templates"))

# Methods that can change state and must be protected from CSRF.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _same_origin(request: HTTPConnection) -> bool:
    """True if the request's Origin or Referer matches the request Host
    and scheme. A scheme mismatch (e.g. http origin on an HTTPS request)
    is treated as unsafe."""
    host = request.headers.get("host", "")
    if not host:
        return False
    scheme = request.url.scheme  # "http" or "https"
    for header in ("origin", "referer"):
        val = request.headers.get(header)
        if not val:
            continue
        parsed = urlparse(val)
        if parsed.netloc == host and parsed.scheme == scheme:
            return True
    return False


class CsrfMiddleware(BaseHTTPMiddleware):
    """Reject cross-origin state-changing requests to /api/*.

    A request to an /api/* path using POST/PUT/PATCH/DELETE is blocked
    unless one of the following holds:
      1. It carries the ``X-Requested-With: XMLHttpRequest`` header
         (custom headers force a CORS preflight, which the default
         empty CORS policy blocks for cross-origin callers).
      2. Its ``Origin`` or ``Referer`` host matches the request Host
         (same-site form/fetch submissions).
      3. Both ``Origin`` and ``Referer`` are absent (non-browser clients
         such as curl, the Starlette TestClient, or scripts).

    Requests that carry a cross-origin ``Origin``/``Referer`` but no
    ``X-Requested-With`` header are blocked. GET/HEAD/OPTIONS and
    non-/api/ paths (the SPA shell, /static/*) are always allowed.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        if path.startswith("/api/") and method in _UNSAFE_METHODS:
            xrw = request.headers.get("x-requested-with", "")
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            has_origin_or_referer = bool(origin or referer)
            if xrw != "XMLHttpRequest" and has_origin_or_referer and not _same_origin(request):
                logger.warning(
                    "CSRF block: %s %s (xrw=%r origin=%r referer=%r)",
                    method, path, xrw, origin, referer,
                )
                return JSONResponse(
                    {"detail": "Cross-origin requests not allowed"},
                    status_code=403,
                )
        return await call_next(request)


class CachedStaticFiles(StaticFiles):
    """StaticFiles that adds long-cache headers for cache-busted assets.

    Requests carrying a ``?v=<hash>`` query string (produced by the
    content-hash cache buster) are served with
    ``Cache-Control: public, max-age=31536000, immutable`` so browsers
    cache them for a year. Requests without ``v=`` fall back to the
    default (no long-cache) behaviour.
    """

    _IMMUTABLE_CC = "public, max-age=31536000, immutable"

    def file_response(
        self,
        full_path: str,
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = FileResponse(
            full_path, status_code=status_code, stat_result=stat_result
        )
        request_headers = Headers(scope=scope)
        if self.is_not_modified(response.headers, request_headers):
            return NotModifiedResponse(response.headers)
        qs = (scope.get("query_string") or b"").decode("latin-1")
        if "v=" in qs:
            response.headers["Cache-Control"] = self._IMMUTABLE_CC
        return response


@asynccontextmanager
async def lifespan(app):
    """Start/stop the background stock poller."""
    import asyncio

    from app.logging_config import setup_logging
    from app.services.logbus import get_log_bus
    from app.services.monitor import get_monitor_service

    # Re-attach the file + LogBus handlers after uvicorn has applied its own
    # logging dict-config (which strips handlers off the loggers it configures),
    # and record the running loop so off-loop log emits can fan out to SSE.
    setup_logging()
    get_log_bus().set_loop(asyncio.get_running_loop())

    monitor = get_monitor_service()
    await monitor.start()
    try:
        yield
    finally:
        await monitor.stop()


def create_app():
    """Build the FastAPI app with all routers and middleware."""
    from app.api import (
        account,
        accounts,
        alert,
        catalog,
        checkout,
        currency,
        insights,
        logs,
        monitor,
        orders,
        profiles,
        sniper,
    )
    from app.api import (
        settings as settings_api,
    )
    from app.config import get_settings
    from app.logging_config import setup_logging

    # Install the file + LogBus handlers early so logs are captured even
    # before the server starts (and in tests, which may not run the lifespan).
    setup_logging()

    app = FastAPI(
        title="ECOVibe",
        description="Real-time OVH server stock monitoring and fast checkout",
        version="0.2.0",
        lifespan=lifespan,
    )

    settings = get_settings()

    # CSRF protection runs before route handlers. Registered before CORS
    # so cross-origin preflight/result checks are evaluated first.
    app.add_middleware(CsrfMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    static_path = os.path.join(BASE_PATH, "static")
    if os.path.exists(static_path):
        app.mount("/static", CachedStaticFiles(directory=static_path), name="static")

    app.include_router(catalog.router)
    app.include_router(checkout.router)
    app.include_router(monitor.router)
    app.include_router(logs.router)
    app.include_router(alert.router)
    app.include_router(insights.router)
    app.include_router(orders.router)
    app.include_router(profiles.router)
    app.include_router(sniper.router)
    app.include_router(settings_api.router)
    app.include_router(account.router)
    app.include_router(accounts.router)
    app.include_router(currency.router)

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request) -> Response:
        """Serve the SPA frontend with content-hash cache busters."""
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "css_hash": get_file_hash("static/css/app.css"),
                "js_hash": get_file_hash("static/js/app.js"),
            },
        )

    @app.get("/api/health")
    async def health() -> dict:
        """Liveness check. Returns active-account status."""
        from app.services.ovh_service import get_active_ovh_service
        from app.services.storage import get_storage
        storage = get_storage()
        active_id = storage.get_active_account_id()
        service = get_active_ovh_service()
        return {
            "status": "ok",
            "configured": service.is_configured(),
            "endpoint": service.endpoint,
            "active_account_id": active_id,
            "account_count": len(storage.list_accounts()),
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    from app.config import get_settings

    _settings = get_settings()
    uvicorn.run(app, host=_settings.host, port=_settings.port)
