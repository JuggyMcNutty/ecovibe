#!/usr/bin/env python3
"""OVH Flash Sale Monitor — entry point.

Run from the project root:

    python run.py

Then open http://localhost:8000 in your browser.

On first startup, the setup wizard will appear in your browser. Enter your
OVH API credentials there — they are stored in the local SQLite database.
No environment variables are needed for OVH secrets.

Non-secret configuration (caching, notifications, etc.) can be set via
environment variables — see `.env.example`.
"""


def main() -> None:
    import uvicorn

    from app.main import app

    print("OVH Flash Sale Monitor")
    print("=" * 40)
    print("Starting server on http://0.0.0.0:8000")
    print()
    print("Open the URL in your browser to configure OVH credentials.")
    print("Credentials are stored in the database — no env vars needed.")
    print()

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
