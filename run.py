#!/usr/bin/env python3
"""OVH Flash Sale Monitor - entry point.

Run: python run.py
Then open http://localhost:8000 and configure credentials in the browser.
"""
def main() -> None:
    import uvicorn

    from app.main import app

    print("OVH Flash Sale Monitor")
    print("=" * 40)
    print("Starting server on http://0.0.0.0:8000")
    print("Open the URL in your browser to configure OVH credentials.")
    print()

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
