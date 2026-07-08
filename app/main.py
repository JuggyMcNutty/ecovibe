"""FastAPI app factory and lifespan management."""
import logging
import os
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Project root (one level up from app/). Used to find static/ and templates/.
BASE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

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
        app.mount("/static", StaticFiles(directory=static_path), name="static")

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
    async def root() -> str:
        """Serve the SPA frontend."""
        template_path = os.path.join(BASE_PATH, "templates", "index.html")
        if os.path.exists(template_path):
            with open(template_path) as f:
                return f.read()
        return """
        <html><body style="background:#1a1a2e;color:#eee;font-family:sans-serif;padding:2rem;">
            <h1>OVH Flash Sale Monitor</h1>
            <p>Application files not found. Please run from the project root directory.</p>
        </body></html>
        """

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
