import logging
import os
import sys
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


def get_base_path():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

BASE_PATH = get_base_path()


@asynccontextmanager
async def lifespan(app):
    from app.services.monitor import get_monitor_service
    monitor = get_monitor_service()
    await monitor.start()
    try:
        yield
    finally:
        await monitor.stop()


def create_app():
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    from app.api import alert, cart, catalog, checkout, monitor
    from app.config import get_settings

    app = FastAPI(
        title="OVH Flash Sale Monitor",
        description="Real-time OVH server stock monitoring and fast checkout",
        version="0.1.0",
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

    @app.get("/", response_class=HTMLResponse)
    async def root():
        template_path = os.path.join(BASE_PATH, "templates", "index.html")
        if os.path.exists(template_path):
            with open(template_path) as f:
                return f.read()
        return """
        <html><body style="background:#1a1a2e;color:#eee;font-family:sans-serif;padding:2rem;">
            <h1>OVH Flash Sale Monitor</h1>
            <p>Application files not found. Please run from the correct directory or reinstall.</p>
        </body></html>
        """

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "endpoint": settings.endpoint,
            "configured": all([
                settings.application_key,
                settings.application_secret,
                settings.consumer_key
            ])
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
