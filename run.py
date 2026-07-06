#!/usr/bin/env python3
"""OVH Flash Sale Monitor — entry point.

Run from the project root:

    python run.py

Then open http://localhost:8000 in your browser.

Required environment variables (see `.env.example`):

    OVH_APPLICATION_KEY
    OVH_APPLICATION_SECRET
    OVH_CONSUMER_KEY
    OVH_ENDPOINT     (ovh-eu | ovh-us | ovh-ca)
"""


def main() -> None:
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
    print("  export OVH_ENDPOINT=ovh-eu   # or ovh-us / ovh-ca")
    print()

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
