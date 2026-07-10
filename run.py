#!/usr/bin/env python3
"""ECOVibe - entry point.

Run: python run.py
Then open http://localhost:8000 and configure credentials in the browser.

The server binds to 127.0.0.1 by default. Set OVH_HOST=0.0.0.0 to listen
on all interfaces (e.g. behind a reverse proxy). See README.md > Deployment.
"""
def main() -> None:
    import uvicorn

    from app.config import get_settings
    from app.main import app

    settings = get_settings()
    print("ECOVibe")
    print("=" * 40)
    print(f"Starting server on http://{settings.host}:{settings.port}")
    print("Open the URL in your browser to configure OVH credentials.")
    if settings.host == "127.0.0.1":
        print("Binding to localhost only. Set OVH_HOST=0.0.0.0 to expose.")
    print()

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
