# OVH Flash Sale Monitor

Real-time stock monitoring and fast checkout for OVH ECO servers. Never miss a flash sale!

## Supported Regions

| Region | Endpoint | API Console |
|--------|----------|-------------|
| Europe | `ovh-eu` | https://eu.api.ovh.com |
| United States | `ovh-us` | https://api.us.ovhcloud.com |
| Canada | `ovh-ca` | https://ca.api.ovh.com |

## Quick Start

```bash
# 1. Install dependencies (runtime only)
pip install -r requirements.txt

# 2. Run
python run.py
```

Open http://localhost:8000 in your browser. On first startup, the setup
wizard will appear - enter your OVH API credentials (application key,
secret, and consumer key) directly in the browser. Credentials are stored
in the local SQLite database; no environment variables are needed for
secrets.

For development (includes tests + linting):

```bash
pip install -r requirements-dev.txt
```

## Deployment

By default the server binds to `127.0.0.1` (localhost only) so it is
never publicly reachable by accident. To expose it — for example behind
a reverse proxy with HTTP Basic Auth — set `OVH_HOST=0.0.0.0`:

```bash
OVH_HOST=0.0.0.0 python run.py
```

The app has built-in CSRF protection: state-changing requests
(`POST`/`PUT`/`DELETE`/`PATCH`) to `/api/*` must carry the
`X-Requested-With: XMLHttpRequest` header or have a same-origin
`Origin`/`Referer`. The SPA sends this header automatically; a malicious
third-party page cannot forge it without triggering a CORS preflight
(which the default empty CORS policy blocks). Authentication itself is
handled by the reverse proxy layer below.

### Caddy

```Caddyfile
flash.example.com {
    basicauth {
        # Generate with: caddy hash-password
        admin $2a$14$...
    }
    reverse_proxy 127.0.0.1:8000
}
```

### nginx

```nginx
server {
    listen 80;
    server_name flash.example.com;

    auth_basic "OVH Flash Sale Monitor";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # SSE: disable buffering, raise timeout for /api/monitor/stream
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

Generate the `.htpasswd` file with `htpasswd -c /etc/nginx/.htpasswd admin`.

> **Note**: Basic Auth credentials are cached per-origin by the browser
> and sent automatically on cross-origin requests, which is why the
> in-app CSRF middleware is required even behind a proxy.

## Requirements

- **Python 3.10+**
- OVH API credentials for your region

## Features

### Flash Sale Monitor
- **Real-time stock tracking** via SSE (Server-Sent Events) with a single shared background poller
- **Browser notifications** when desired configs become available
- **Multi-channel notifications** (Telegram, Discord, Slack, email) - never miss a flash sale when away from the browser
- **Sound alerts** - audio notification (requires a user gesture first, e.g. clicking "Start Monitor")
- **1-10 second polling** configurable interval (persisted across restarts)
- **One-click Rush Order** when stock is detected
- **Alert pause/resume** - disable alerts without deleting them

### Sniper Mode (auto-order)
- Arm an alert with a saved checkout profile
- When stock appears that matches the alert, the backend automatically fires the rush order
- One-shot per arm (no duplicate orders); re-arm after each result
- Status endpoint shows armed alerts and last results

### Server Catalog
- Browse ECO server catalog with full CPU and hardware specifications
- **Live stock levels** - real-time availability per RAM+storage combo, fetched from OVH's datacenter availabilities API
- **Out-of-stock badges** - plans whose included (free) config is out of stock are dimmed with a red OOS badge
- **In stock first** sort filter - pushes orderable plans to the top of the list
- **Selected plan highlight** - clicking a plan highlights it in the list for easy tracking
- **Loading overlay** - semi-transparent overlay with spinner during catalog/stock loads
- **CPU details** extracted from OVH product blobs: model, cores/threads, frequency, boost, benchmark score
- **Hardware badges**: chassis size, SLA, anti-DDoS, server range (Kimsufi/Rise/SYS/LE)
- **Setup/installation fees** surfaced alongside monthly pricing
- **Search & filter** plans by name/code, sort by price/name/CPU score
- **Region filter** - only show plans orderable on your configured endpoint
- Filter by country/subsidiary (IE, FR, DE, GB, ES, PL, IT, PT, CZ, FI)
- View configurations, pricing, and availability
- **Addon labels** use OVH's official invoiceName (e.g. "2x SSD NVMe 512GB Datacenter Class Soft RAID")
- Quick-add servers to watchlist (click or keyboard)

### Checkout
- **Saved checkout profiles** - pre-configure cart templates (RAM, storage, DCs, OS, duration, etc.)
- **Multi-datacenter fallback** - try DCs in order (GRA→SBG→RBX→...) during rush order
- Configure RAM, storage, bandwidth options
- Set region (Europe/Canada/US), operating system, and billing duration
- Auto-pay and waive retraction options
- **Max price cap** - refuse checkout if price exceeds threshold (enforced in both catalog and rush order flows)
- **Region mismatch guard** - prevents ordering cross-region plans (e.g. EU plan on US endpoint)

### Historical Insights
- **Restock patterns** - stock events are logged to SQLite; view hourly bar chart aggregation to find the best times to monitor
- **Price history** - track price changes per plan over time with manual refresh
- **Order tracking** - recently placed orders with live status refresh from OVH
- **Stock events** - recent availability/unavailability events per plan

### Notification Channels
- **Telegram** - stock alerts via Telegram bot
- **Discord** - rich embed alerts via webhook
- **Slack** - alerts via incoming webhook
- **Email/SMTP** - HTML email alerts with STARTTLS
- Configured via the Settings button (top-right header) - stored in the DB, secrets masked
- Environment variables serve as fallback for initial setup

### Persistence
- Alerts, poll-interval setting, checkout profiles, stock events, price history, and orders are persisted to SQLite (`ovh-flash-monitor.db`)
- Alert and profiles survive process restarts
- Notification settings persisted to DB `settings` table with `notifier_` prefix
- DB path resolves to an absolute path anchored to the project root (CWD-independent)

## Configuration

OVH API credentials are configured via the browser setup wizard on first
startup and stored in the SQLite database. Notification channel settings
(Telegram, Discord, Slack, SMTP) are also configured via the browser
(Settings button in the header) and stored in the DB; env vars serve as
fallback for initial setup. Non-secret configuration uses environment
variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OVH_HOST` | `127.0.0.1` | Server bind address (use `0.0.0.0` behind a reverse proxy) |
| `OVH_PORT` | `8000` | Server bind port |
| `OVH_USE_CACHE` | `false` | Enable in-memory catalog caching |
| `OVH_CACHE_TTL` | `300` | Cache TTL in seconds |
| `OVH_DB_PATH` | `<project>/ovh-flash-monitor.db` | SQLite database path (defaults to project root) |
| `OVH_CORS_ORIGINS` | `[]` | Comma-separated allowed CORS origins |
| `OVH_TELEGRAM_BOT_TOKEN` | - | Telegram bot token (fallback for DB settings) |
| `OVH_TELEGRAM_CHAT_ID` | - | Telegram chat ID (fallback for DB settings) |
| `OVH_DISCORD_WEBHOOK_URL` | - | Discord webhook URL (fallback for DB settings) |
| `OVH_SLACK_WEBHOOK_URL` | - | Slack webhook URL (fallback for DB settings) |
| `OVH_SMTP_HOST` | - | SMTP host (fallback for DB settings) |
| `OVH_SMTP_PORT` | `587` | SMTP port (fallback for DB settings) |
| `OVH_SMTP_USERNAME` | - | SMTP username (fallback for DB settings) |
| `OVH_SMTP_PASSWORD` | - | SMTP password (fallback for DB settings) |
| `OVH_SMTP_FROM` | - | From address (fallback for DB settings) |
| `OVH_NOTIFY_EMAIL_TO` | - | Recipient (fallback for DB settings) |

See `.env.example` for a template.

## Region-Specific Setup

OVH credentials are region-specific. Select your region in the setup wizard
and use the corresponding API console to create your application and token:

### United States (ovh-us)
- API Base: `https://api.us.ovhcloud.com/v1`
- Create App: `https://api.us.ovhcloud.com/createApp/`
- Create Token: `https://api.us.ovhcloud.com/createToken/`

### Europe (ovh-eu)
- API Base: `https://eu.api.ovh.com/v1`
- Create App: `https://eu.api.ovh.com/createApp/`
- Create Token: `https://eu.api.ovh.com/createToken/`

### Canada (ovh-ca)
- API Base: `https://ca.api.ovh.com/v1`
- Create App: `https://ca.api.ovh.com/createApp/`
- Create Token: `https://ca.api.ovh.com/createToken/`

## Getting OVH Credentials

1. **Create Application** - Visit your region's API console (links above).

2. **Create Token** - Visit the token creation page for your region.

3. **Required Permissions**:
   - GET/PUT/POST/DELETE on `/order/*`
   - GET on `/me`

4. **Enter in Setup Wizard** - Open http://localhost:8000, select your
   region, and paste the three keys into the setup form. Click "Save & Test"
   to verify the connection.

**Note:** Credentials are region-specific. US credentials only work with `ovh-us`, EU credentials only work with `ovh-eu`.

## API Endpoints

```
# Catalog
GET  /api/catalog?country=IE                - Fetch full server catalog
GET  /api/catalog/plans?country=IE         - List plans + addon prices + product specs
GET  /api/catalog/availability?plan_code=XX - Check plan availability
GET  /api/catalog/stock?plan_code=XX       - Live stock levels per RAM+storage combo

# Monitor
GET  /api/monitor/stream                    - SSE real-time stock updates
GET  /api/monitor/availability?plans=XX,YY  - Current stock for plans
GET  /api/monitor/status                    - Monitor status (interval, alert count)
PUT  /api/monitor/poll-interval             - Set poll interval (body: {poll_interval: 1-10})
POST /api/monitor/poll-interval             - Alias for PUT

# Alerts
POST   /api/alerts                          - Create stock alert
GET    /api/alerts                          - List alerts
GET    /api/alerts/{id}                     - Get a single alert
DELETE /api/alerts/{id}                     - Remove alert
PUT    /api/alerts/{id}/enable              - Enable alert
PUT    /api/alerts/{id}/disable             - Disable alert
PUT    /api/alerts/{id}/profile             - Assign checkout profile (for sniper mode)

# Cart (legacy granular API; prefer /api/checkout/rush for one-shot)
POST /api/cart                              - Create cart (body: {description})
GET  /api/cart/{id}                         - Get cart details
POST /api/cart/{id}/server                  - Add server item
POST /api/cart/{id}/options                  - Add option to item
POST /api/cart/{id}/config                   - Set item configuration
GET  /api/cart/{id}/summary                  - Order summary

# Checkout
POST /api/checkout/rush                      - One-shot rush order (builds cart, tries DCs in order, checks out)
POST /api/checkout/{cart_id}                 - Place order from existing cart (body: {auto_pay, waive_retractation})

# Checkout Profiles
GET    /api/profiles                         - List saved profiles
POST   /api/profiles                         - Create profile
GET    /api/profiles/{id}                    - Get a profile
PUT    /api/profiles/{id}                    - Update profile
DELETE /api/profiles/{id}                    - Delete profile

# Sniper Mode
GET  /api/sniper/status                      - Show armed alerts + last results
POST /api/sniper/arm                         - Arm alert with profile (body: {alert_id, profile_id})
POST /api/sniper/disarm/{alert_id}           - Disarm an alert

# Insights (historical data)
GET  /api/insights/history/{plan_code}?days=N    - Recent stock events
GET  /api/insights/patterns/{plan_code}          - Hourly restock count aggregation
GET  /api/insights/price/{plan_code}             - Price history
POST /api/insights/price/{plan_code}/refresh     - Fetch + log current price
GET  /api/insights/orders                         - Recently placed orders
GET  /api/insights/orders/{order_id}              - Fetch order status from OVH

# Setup Wizard
GET    /api/setup/credentials                    - Check if credentials are configured (masked)
POST   /api/setup/credentials                    - Save OVH credentials to database
POST   /api/setup/test                           - Test credentials via GET /me on OVH
DELETE /api/setup/credentials                    - Delete stored credentials

# Notification Settings
GET    /api/settings/notifications               - Get notifier config (secrets masked) + active channels
PUT    /api/settings/notifications               - Save notifier config (masked values preserved)

# Account & Billing
GET  /api/account/me                             - OVH account info (name, nichandle, email)
GET  /api/account/payment-methods                - Available payment methods on the account
GET  /api/account/checkout-defaults              - Default checkout preferences (auto-pay, duration, etc.)
PUT  /api/account/checkout-defaults              - Save default checkout preferences

# Health
GET  /api/health                                - Service health + config status
```

## Development

```bash
# Run tests (must use PYTHONPATH=. since app/ is not installed)
PYTHONPATH=. .venv/bin/pytest

# Lint
.venv/bin/ruff check app/ tests/ run.py

# Rebuild + minify CSS (uses standalone Tailwind binary)
/tmp/tailwindcss --input static/css/input.css --output static/css/app.css --minify

# Run in dev mode
.venv/bin/uvicorn app.main:app --reload
```

## Project Structure

```
ovh-gui/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + lifespan
│   ├── config.py            # Environment/config (pydantic-settings)
│   ├── api/                 # Route handlers
│   │   ├── catalog.py       # Catalog endpoints + product spec extraction
│   │   ├── monitor.py       # SSE stock streaming + poll-interval
│   │   ├── alert.py         # Alert CRUD + enable/disable + profile assignment
│   │   ├── cart.py          # Cart lifecycle (legacy)
│   │   ├── checkout.py      # Rush order (one-shot) + legacy cart checkout
│   │   ├── profiles.py      # Saved checkout profile CRUD
│   │   ├── sniper.py        # Sniper arm/disarm/status
│   │   ├── insights.py      # History, patterns, price, orders
│   │   ├── setup.py         # Setup wizard (save/test OVH credentials)
│   │   ├── settings.py      # Notification channel settings (Telegram/Discord/Slack/SMTP)
│   │   ├── account.py       # OVH account + payment methods + defaults
│   │   └── errors.py        # OVH->HTTP error mapping
│   ├── models/schemas.py    # Pydantic request/response models
│   └── services/
│       ├── ovh_service.py   # OVH API wrapper (singleton)
│       ├── monitor.py       # Stock monitoring + background poller + SniperService
│       ├── notifier.py      # Telegram/Discord/Slack/email fan-out
│       ├── storage.py       # SQLite persistence (alerts, profiles, events, prices, orders)
│       └── cache.py         # In-memory TTL cache
├── static/js/app.js         # Frontend SPA (vanilla JS, ~2600 lines)
├── static/css/input.css     # Tailwind v4 source
├── static/css/app.css       # Built/minified (do not edit)
├── templates/index.html    # SPA shell with cache-busted asset refs
├── tests/                   # pytest suite (69 tests, uses TestClient)
├── requirements.txt         # Runtime dependencies
├── requirements-dev.txt     # Dev dependencies (ruff, pytest, httpx)
├── pyproject.toml           # Project metadata + tool config
├── run.py                   # Entry point
├── Makefile                 # install/dev/test/lint/run/clean targets
├── AGENTS.md                # AI session reference
└── README.md
```

## Tips for Flash Sales

1. **Pre-configure everything** before the sale starts:
   - Credentials already set
   - Servers added to watchlist
   - Rush Order form pre-filled
   - Auto-pay enabled if you want instant checkout

2. **Enable notifications** when prompted (click "Start Monitor" to grant permission)

3. **Keep the tab open** and monitoring active

4. **Use 1-second polling** for fastest detection

5. **Have payment ready** on your OVH account
