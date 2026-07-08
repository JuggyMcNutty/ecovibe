"""FastAPI app factory and lifespan management."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse
from starlette.types import Scope

from app.utils.cache_buster import get_file_hash

logger = logging.getLogger(__name__)

# Project root (one level up from app/). Used to find static/ and templates/.
BASE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

templates = Jinja2Templates(directory=os.path.join(BASE_PATH, "templates"))


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
    from app.services.monitor import get_monitor_service

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
        alert,
        cart,
        catalog,
        checkout,
        insights,
        monitor,
        profiles,
        setup,
        sniper,
    )
    from app.api import (
        settings as settings_api,
    )
    from app.config import get_settings

    app = FastAPI(
        title="OVH Flash Sale Monitor",
        description="Real-time OVH server stock monitoring and fast checkout",
        version="0.2.0",
        lifespan=lifespan,
    )

    settings = get_settings()

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
    app.include_router(cart.router)
    app.include_router(checkout.router)
    app.include_router(monitor.router)
    app.include_router(alert.router)
    app.include_router(insights.router)
    app.include_router(profiles.router)
    app.include_router(sniper.router)
    app.include_router(setup.router)
    app.include_router(settings_api.router)
    app.include_router(account.router)

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
        """Liveness check. Returns whether OVH credentials are configured."""
        from app.services.ovh_service import get_ovh_service
        service = get_ovh_service()
        return {
            "status": "ok",
            "endpoint": service.endpoint,
            "configured": service.is_configured(),
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
