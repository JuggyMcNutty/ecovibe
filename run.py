#!/usr/bin/env python3
"""
OVH Flash Sale Monitor - Entry Point
Run directly with: python3 run.py
Or build to binary with PyInstaller on Python 3.10-3.13

NOTE: Building to a standalone binary requires Python 3.10, 3.11, 3.12, or 3.13.
Python 3.14 is NOT yet supported by PyInstaller.
"""


def main():
    import uvicorn

    from app.main import app

    print("OVH Flash Sale Monitor")
    print("=" * 40)
    print("Starting server on http://0.0.0.0:8000")
    print()
    print("Set these environment variables before running:")
    print("  export OVH_APPLICATION_KEY=your_key")
    print("  export OVH_APPLICATION_SECRET=your_secret")
    print("  export OVH_CONSUMER_KEY=your_consumer_key")
    print()

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
