"""FastAPI application entry point for the OVH Flash Sale Monitor.

This module constructs the FastAPI app, wires up all routers and middleware,
and manages the background monitor service lifecycle via a lifespan context.
"""
import logging
import os
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Project root (one level above the `app/` package). Used to locate the
# `static/` and `templates/` directories when running from source.
BASE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@asynccontextmanager
async def lifespan(app):
    """Start the background stock poller on startup, stop it on shutdown.

    The MonitorService runs a single long-lived asyncio task that polls OVH
    for stock changes and broadcasts updates to SSE subscribers. The poller
    only runs while the app is up — shutting down cancels it cleanly.
    """
    from app.services.monitor import get_monitor_service

    monitor = get_monitor_service()
    await monitor.start()
    try:
        yield
    finally:
        await monitor.stop()


def create_app():
    """Build and return the configured FastAPI application.

    Imported routers are mounted under their respective `/api/...` prefixes.
    Imports are kept inside the factory so that test configuration (env vars,
    monkeypatched settings) is applied before module-level side effects run.
    """
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    from app.api import alert, cart, catalog, checkout, insights, monitor, profiles, sniper
    from app.config import get_settings

    app = FastAPI(
        title="OVH Flash Sale Monitor",
        description="Real-time OVH server stock monitoring and fast checkout",
        version="0.2.0",
        lifespan=lifespan,
    )

    settings = get_settings()

    # CORS is opt-in via OVH_CORS_ORIGINS. Defaults to an empty allow-list
    # (same-origin only), which is correct when serving the SPA from `/`.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount the static asset directory (frontend JS) if present.
    static_path = os.path.join(BASE_PATH, "static")
    if os.path.exists(static_path):
        app.mount("/static", StaticFiles(directory=static_path), name="static")

    # Register every API router. Each router declares its own prefix/tag.
    app.include_router(catalog.router)
    app.include_router(cart.router)
    app.include_router(checkout.router)
    app.include_router(monitor.router)
    app.include_router(alert.router)
    app.include_router(insights.router)
    app.include_router(profiles.router)
    app.include_router(sniper.router)

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        """Serve the single-page frontend from `templates/index.html`."""
        template_path = os.path.join(BASE_PATH, "templates", "index.html")
        if os.path.exists(template_path):
            with open(template_path) as f:
                return f.read()
        # Fallback shown if the templates directory is missing (e.g. wrong CWD).
        return """
        <html><body style="background:#1a1a2e;color:#eee;font-family:sans-serif;padding:2rem;">
            <h1>OVH Flash Sale Monitor</h1>
            <p>Application files not found. Please run from the project root directory.</p>
        </body></html>
        """

    @app.get("/health")
    async def health() -> dict:
        """Lightweight liveness check.

        Returns the configured OVH endpoint region and whether credentials
        are present. No secrets are exposed — only a boolean `configured` flag.
        """
        return {
            "status": "ok",
            "endpoint": settings.endpoint,
            "configured": all([
                settings.application_key,
                settings.application_secret,
                settings.consumer_key,
            ]),
        }

    return app


# Module-level app instance. Imported by `run.py` and uvicorn via `app.main:app`.
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
