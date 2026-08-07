# ECOVibe

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

    auth_basic "ECOVibe";
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
- **All accounts monitored at once** - the poller watches every stored account's alerts, not just the active one, so switching accounts never pauses monitoring: history keeps building and restock alerts keep firing for the accounts you aren't looking at (each alert is tagged with its account)
- **Per-account monitoring switch** - "Monitoring: On/Off" on the Monitor tab is a server-side, persisted switch for the *active account only*. Off means the server does nothing at all for that account: no stock polling, no region ticker, no price/promo scan, no sniper fire. Other accounts are unaffected
- **Batched polling** - watching 2+ plans uses ONE region-wide availabilities call per cycle instead of one per plan (poll interval clamps to ≥3s in batch mode; single-plan polling keeps 1s fidelity). Accounts are polled concurrently, so a cycle costs the slowest account rather than their sum
- **Region restock ticker** (optional, per account) - live feed of restocks across an account's ENTIRE region, streamed over SSE and logged for insights
- **Silent start** - the first poll after a restart primes the stock baseline without re-notifying everything already in stock (armed snipers still fire)
- **Browser notifications** when desired configs become available
- **Multi-channel notifications** (Telegram, Discord, Slack, email) - never miss a flash sale when away from the browser
- **Sound alerts** - audio notification (requires a user gesture first, e.g. clicking the Monitoring toggle)
- **Live view is always on** - the browser connects to the event stream on page load and stays connected across account switches; the header dot (Live / Reconnecting… / Offline) reports *that connection only*. Whether monitoring is happening is server-side and shown on the Monitor tab, so closing the tab never stops anything
- **1-60 second polling** configurable interval (persisted across restarts)
- **One-click Rush Order** when stock is detected (incoming alerts never overwrite the form while you're editing it - a "Use this config" button applies them explicitly)
- **Alert pause/resume** - disable alerts without deleting them (pausing disarms any sniper on the alert)

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
- **Currency selector** - view all prices in EUR/USD/GBP/CAD (display-only; converts via daily ECB rates, defaults to the account's billing currency)
- **Search & filter** plans by name/code, sort by price/name/CPU score
- **Location badges & filter** - each plan is badged with the location groups it can actually deploy to (EU/CA/US/APAC, from its real datacenter list — hover for the DCs), with a dropdown to filter by location and search matching DC codes. Note: OVH-CA's catalog has no separate "-eu" plan codes, but most of its home plans deploy to European datacenters — the badges reflect that
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

### Order Management
- **Full orders tab** - list all orders from OVH (not just locally-placed), merged with local order data
- **Status badges** - color-coded by OVH OrderStatusEnum (delivered, delivering, notPaid, cancelled, etc.)
- **Order detail panel** - price breakdown (with/without tax), grouped line items (one row per component — server + options — with setup fee and monthly price merged), delivery follow-up timeline
- **Invoice PDF links** - direct link to OVH's invoice PDF
- **Waive retraction** - one-click waive the legal retraction period to speed up delivery
- **Cancel order** - exercise the right of retraction (withdrawal) during the retraction period
- **Refresh all** - re-fetch all order statuses from OVH
- **Filter** by status (all / pending / delivered / cancelled)
- **Delivery watch** - the background monitor re-checks your pending orders every 5 minutes (for every monitored account) and **notifies you the moment one is delivered or cancelled**, so you hear it from ECOVibe rather than from OVH's email. Intermediate churn (checking → delivering) updates quietly without pinging your channels, settled orders are never re-queried, and an open browser refreshes the Orders tab in place. Cadence lives in **Settings → App** → *Delivery check (s)* (0 disables)

### Price Watches, Promotions & Catalog Changes
- **Price-drop alerts** - set a per-plan price cap; the monitor re-checks the catalog every 15 minutes (for every account, not just the active one) and notifies (all channels) when the price falls to/below it. Re-fires only when the price moves again
- **Promo detector** - OVH's catalog `promotions` field is scanned on the same cadence; newly published promotions notify and appear in the Insights "Recent promotions" panel
- **Catalog watch** - the same catalog fetch is diffed against a stored snapshot of the account's plan codes, so **plans OVH adds or retires** are recorded and notified (no extra API calls). They appear in the Insights "Catalog changes" panel, newest first, with a one-click "Watch" button on new plans. The first scan for an account only records a baseline (it would otherwise report the whole catalog as new), the snapshot lives in SQLite so a restart still catches what changed while the app was down, and a truncated catalog response is ignored rather than reported as a mass retirement. Both switches live in **Settings → App**: *Track catalog changes* and *Notify on catalog changes*

### Owned Servers & Invoices
- **Servers tab** - full control panel for each dedicated server, not just a list:
  - **Power & boot** - pick a netboot (hard disk / rescue / power-off), set it for the next boot or apply it with a reboot, or hard-reboot now
  - **Flags & rescue** - ICMP monitoring, block-datacenter-intervention, rescue email and rescue SSH key
  - **Tasks** - live view of OVH's task queue with cancel, auto-refreshing while anything is running
  - **Reinstall** - pick from the OS templates compatible with your hardware, with hostname, SSH key, post-install script and custom-image (BYOI) options, and live installation progress
  - **IPMI / KVM** - request a console session; only the console types your machine actually reports are offered
  - **Console in your browser** - "Open console in browser" gives you the server's screen, keyboard and mouse in a normal Chrome tab, **even on hardware where OVH only offers a Java `.jnlp`**. OVH's entry-level servers report `kvmipHtml5URL: false`, and Java Web Start no longer runs anywhere — but the `.jnlp` is only a connection descriptor. Behind it these BMCs are ATEN iKVM speaking plain VNC, so ECOVibe brokers the session, relays it over a WebSocket, and renders it with a vendored noVNC build that understands ATEN's video encoding. No Java, no containers, no browser plugins
  - **Info panels** - hardware, network, IPs, interfaces, options, licences, intervention history, planned changes, virtual MACs, secondary DNS, SPLA, vRack, and traffic graphs
  - **Service** - renewal settings, and a two-step termination flow (OVH emails a token; nothing is cancelled without it)
- **Only what your server supports is shown.** OVH advertises 98 API paths for dedicated servers but any given machine implements a subset — an entry-level box has no firewall, KVM, cloud backup, BIOS settings, burst or hardware RAID. ECOVibe probes each server once, caches the answer, and renders only the sections that exist, so there are no dead buttons
- **Destructive actions are gated** - reinstall, OLA reset and termination require typing the server's name to confirm
- **Server watch** - on the same cadence as the delivery watch, your dedicated-server list is diffed against a stored snapshot, so a **newly delivered server announces itself** on your notification channels and appears in the Servers tab without a manual refresh (servers that vanish are reported too). The first scan for an account only records a baseline
- **Recent invoices** - last 6 months of invoices with totals and PDF links on the Billing tab

### Historical Insights
- **Catalog changes** - plans OVH added to or retired from this account's catalog (see Catalog watch above), with price and a "Watch" shortcut on new arrivals
- **Restock patterns** - stock events are logged to SQLite; view hourly bar chart aggregation to find the best times to monitor
- **Region activity** - with the ticker on, every plan's transitions are recorded (retention-pruned: 90 days / 500k rows by default)
- **Price history** - track price changes per plan over time with manual refresh
- **Order tracking** - see the Orders tab above for full order management
- **Stock events** - recent availability/unavailability events per plan

### Logs
- **In-app Logs tab** - view runtime logs live in the browser without SSH'ing
  into the server. Streamed via SSE as they happen (with a pause/resume toggle)
- **Verbosity levels & filters** - filter by level (DEBUG/INFO/WARNING/ERROR and
  above), by source module, and by free-text search
- **Event logging** - stock changes, orders placed, sniper auto-orders,
  notifications sent, and OVH API failures are all logged explicitly
- **Rotating log file** - all logs are also written to `ecovibe.log` (durable,
  size-rotated) regardless of the browser; the in-app view is a live tail of the
  last few thousand lines
- Log level and file location are configurable via `OVH_LOG_LEVEL` / `OVH_LOG_FILE`

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

Options marked ⚙ can also be changed live in **Settings → App** — the
DB value wins over the env var and survives restarts. The rest are
env-only and require a restart to change.

| Variable | Default | Description |
|----------|---------|-------------|
| `OVH_HOST` | `127.0.0.1` | Server bind address (use `0.0.0.0` behind a reverse proxy) |
| `OVH_PORT` | `8000` | Server bind port |
| `OVH_USE_CACHE` ⚙ | `false` | Enable in-memory catalog caching |
| `OVH_CACHE_TTL` ⚙ | `300` | Cache TTL in seconds |
| `OVH_DB_PATH` | `<project>/ovh-flash-monitor.db` | SQLite database path (defaults to project root) |
| `OVH_CORS_ORIGINS` | `[]` | Comma-separated allowed CORS origins |
| `OVH_PRICE_CHECK_INTERVAL` ⚙ | `900` | Price-watch/promo/catalog scan cadence in seconds (0 disables) |
| `OVH_ORDER_CHECK_INTERVAL` ⚙ | `300` | Delivery watch cadence in seconds — pending order statuses + owned-server diff (0 disables) |
| `OVH_CATALOG_WATCH_ENABLED` ⚙ | `true` | Track plans added to/removed from the catalog |
| `OVH_CATALOG_WATCH_NOTIFY` ⚙ | `true` | Send catalog changes to the notification channels |
| `OVH_STOCK_EVENT_RETENTION_DAYS` ⚙ | `90` | Stock events older than this are pruned hourly |
| `OVH_STOCK_EVENT_MAX_ROWS` ⚙ | `500000` | Hard cap on the stock_events table (oldest dropped) |
| `OVH_LOG_LEVEL` ⚙ | `INFO` | Log verbosity (`DEBUG`/`INFO`/`WARNING`/`ERROR`) |
| `OVH_LOG_FILE` | `<project>/ecovibe.log` | Rotating log file path |
| `OVH_LOG_FILE_MAX_BYTES` ⚙ | `5000000` | Rotate the log file at this size |
| `OVH_LOG_BACKUP_COUNT` ⚙ | `3` | Number of rotated log backups to keep |
| `OVH_LOG_BUFFER_SIZE` ⚙ | `5000` | In-memory log entries retained for the Logs tab |
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

**Settings → App** additionally holds UI preferences with no env
counterpart: stock-alert auto-hide time (0 = keep open), Orders tab
window/limit, Logs snapshot size, region feed cap, and how many recent
alerts are shown. Log level, cache options, and rotation settings apply
immediately on save — no restart needed.

## Region-Specific Setup

OVH credentials are region-specific. Select your region in the setup wizard
and use the corresponding OVHcloud Manager to create an API key:

### United States (ovh-us)
- API Base: `https://api.us.ovhcloud.com/v1`
- Manager: `https://us.ovhcloud.com/manager/` (Account settings → Security → API keys)

### Europe (ovh-eu)
- API Base: `https://eu.api.ovh.com/v1`
- Manager: `https://www.ovh.com/manager/` (Account settings → Security → API keys)

### Canada (ovh-ca)
- API Base: `https://ca.api.ovh.com/v1`
- Manager: `https://ca.ovh.com/manager/` (Account settings → Security → API keys)

## Getting OVH Credentials

1. **Open the OVHcloud Manager** for your region (links above).

2. **Create an API key** — navigate to your account settings, then
   Security → API keys, and create a new key. You'll receive three
   values: Application Key, Application Secret, and Consumer Key.

3. **Enter in Setup Wizard** — Open http://localhost:8000, select your
   region, and paste the three keys into the setup form. Saving
   verifies the credentials against OVH first — invalid keys, or keys
   created for a different region, are rejected and nothing is saved.

**Note:** Credentials are region-specific. US credentials only work with `ovh-us`, EU credentials only work with `ovh-eu`.

## API Endpoints

```
# Catalog
GET  /api/catalog?country=IE                - Fetch full server catalog
GET  /api/catalog/plans?country=IE         - List plans + addon prices + product specs
GET  /api/catalog/availability?plan_code=XX - Check plan availability
GET  /api/catalog/stock?plan_code=XX       - Live stock levels per RAM+storage combo

# Monitor
GET  /api/monitor/stream                    - SSE real-time stock updates (+ region_restock events)
GET  /api/monitor/availability?plans=XX,YY  - Current stock for plans
GET  /api/monitor/status                    - Poller state + per-account monitoring rows + the active account's alert count
PUT  /api/monitor/poll-interval             - Set poll interval (body: {poll_interval: 1-60})
POST /api/monitor/poll-interval             - Alias for PUT
GET  /api/monitor/region-watch              - Region restock ticker state (active account)
PUT  /api/monitor/region-watch              - Enable/disable the active account's ticker (body: {enabled})

# Alerts
POST   /api/alerts                          - Create stock alert
GET    /api/alerts                          - List alerts
GET    /api/alerts/{id}                     - Get a single alert
DELETE /api/alerts/{id}                     - Remove alert
PUT    /api/alerts/{id}/enable              - Enable alert
PUT    /api/alerts/{id}/disable             - Disable alert
PUT    /api/alerts/{id}/profile             - Assign checkout profile (for sniper mode)

# Checkout
POST /api/checkout/rush                      - One-shot rush order (builds cart, tries DCs in order, checks out)

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

# Order Management (live OVH orders)
GET  /api/orders?limit=30&days=90               - List all orders (merged local + OVH, enriched)
GET  /api/orders/{order_id}                     - Full order detail (line items + follow-up timeline)
POST /api/orders/{order_id}/refresh             - Re-fetch order status from OVH
POST /api/orders/{order_id}/waive-retraction    - Waive the retraction period (speed up delivery)
POST /api/orders/{order_id}/cancel              - Cancel order (exercise right of retraction)

# Insights (historical data)
GET  /api/insights/summary?days=N&watched_only=  - Cross-plan overview (defaults to watched plans)
GET  /api/insights/history/{plan_code}?days=N    - Recent stock events
GET  /api/insights/patterns/{plan_code}          - Hourly restock count aggregation
GET  /api/insights/region-activity?hours=N       - Region-wide stock events (ticker feed)
GET  /api/insights/promos                        - Recently seen OVH promotions
GET  /api/insights/catalog-changes?days=N        - Plans added to/removed from the catalog
GET  /api/insights/price/{plan_code}             - Price history
POST /api/insights/price/{plan_code}/refresh     - Fetch + log current price
GET  /api/insights/orders                         - Recently placed orders
GET  /api/insights/orders/{order_id}              - Fetch order status from OVH

# Price Watches
GET    /api/price-watches                        - List price watches (active account)
POST   /api/price-watches                        - Create/update a watch (body: {plan_code, threshold_ucents})
DELETE /api/price-watches/{id}                   - Delete a watch

# Owned Servers
GET    /api/servers                                     - List dedicated servers (enriched)
GET    /api/servers/{name}                              - Full server detail + serviceInfos
GET    /api/servers/{name}/capabilities?refresh=        - Which optional features this server has
GET    /api/servers/{name}/resource/{key}               - Read a registry-defined sub-resource
GET    /api/servers/{name}/boot                         - Netboot options (resolved)
PUT    /api/servers/{name}/boot                         - Set next boot (body: {boot_id, reboot?})
POST   /api/servers/{name}/reboot                       - Hard reboot
PUT    /api/servers/{name}/properties                   - Monitoring, no-intervention, rescue mail/key
GET    /api/servers/{name}/tasks                        - Recent tasks (+ /{id}, /{id}/timeslots)
POST   /api/servers/{name}/tasks/{id}/cancel            - Cancel a task
POST   /api/servers/{name}/tasks/{id}/schedule          - Book an intervention slot
GET    /api/servers/{name}/install/templates            - Compatible OS templates
GET    /api/servers/{name}/install/partition-schemes    - Schemes for a template
GET    /api/servers/{name}/install/raid-profile         - Hardware RAID profile (or unsupported)
GET    /api/servers/{name}/install/status               - Installation progress
POST   /api/servers/{name}/reinstall                    - Reinstall the OS (ERASES the server)
GET    /api/servers/{name}/ipmi                         - IPMI state + supported console types
POST   /api/servers/{name}/ipmi/session                 - Open a console session (POST + task poll + fetch)
POST   /api/servers/{name}/ipmi/access                  - Raw access request (returns a task)
POST   /api/servers/{name}/console/session              - Broker a browser console session
WS     /api/servers/{name}/console/ws?session=          - WebSocket relay to the BMC
GET    /console/{name}                                  - Full-page browser KVM console
POST   /api/servers/{name}/ipmi/{test|reset-sessions|reset-interface}
POST   /api/servers/{name}/ola/{group|ungroup|aggregation|reset}
POST   /api/servers/{name}/vni/{uuid}/{enable|disable}  - Virtual network interface
POST   /api/servers/{name}/ip-move                      - Move a failover IP here
POST   /api/servers/{name}/ip-block-merge               - Merge a split block (irreversible)
POST   /api/servers/{name}/virtual-mac                  - Add a virtual MAC (+ /{mac}/address)
POST   /api/servers/{name}/secondary-dns                - Add a secondary DNS domain
POST   /api/servers/{name}/spla                         - Add an SPLA licence (+ /{id}/revoke)
DELETE /api/servers/{name}/option/{option}              - Release an option
POST   /api/servers/{name}/support/replace/{cooling|hard-disk-drive|memory}
PUT    /api/servers/{name}/service-infos                - Renewal settings
POST   /api/servers/{name}/terminate                    - Request termination (OVH emails a token)
POST   /api/servers/{name}/confirm-termination          - Confirm with the token

# Setup Wizard
GET    /api/setup/credentials                    - Check if credentials are configured (masked)
POST   /api/setup/credentials                    - Save OVH credentials to database
POST   /api/setup/test                           - Test credentials via GET /me on OVH
DELETE /api/setup/credentials                    - Delete stored credentials

# Notification Settings
GET    /api/settings/notifications               - Get notifier config (secrets masked) + active channels
PUT    /api/settings/notifications               - Save notifier config (masked values preserved)

# Accounts (multi-region credentials)
GET    /api/accounts                             - List accounts (secrets masked)
POST   /api/accounts                             - Create account (first becomes active)
PUT    /api/accounts/{id}                        - Update account (empty secret preserves stored)
DELETE /api/accounts/{id}                        - Delete account (falls back active if needed)
POST   /api/accounts/{id}/test                   - Test account via GET /me
GET    /api/accounts/active                      - Read active account id + masked preview
PUT    /api/accounts/active                      - Switch active account (reloads monitor)
PUT    /api/accounts/{id}/monitoring             - Per-account switches (body: {monitoring_enabled?, region_ticker_enabled?})

# Logs
GET  /api/logs?limit=&level=&source=&search=     - Recent runtime logs + known sources
GET  /api/logs/stream                             - SSE live tail of new log entries

# Currency (display conversion)
GET  /api/currency/rates                          - ECB/Frankfurter FX rates (EUR-base, 24h cache)

# Account & Billing
GET  /api/account/me                             - OVH account info (name, nichandle, email)
GET  /api/account/payment-methods                - Available payment methods on the account
GET  /api/account/bills?limit=20&months=6        - Recent invoices (totals + PDF links)
GET  /api/account/checkout-defaults              - Default checkout preferences (auto-pay, duration, etc.)
PUT  /api/account/checkout-defaults              - Save default checkout preferences

# Health
GET  /api/health                                - Service health + active-account status
```

## Multiple Accounts

You can store credentials for several OVH accounts (different regions, or
multiple accounts in the same region) and switch between them instantly.

- **Add an account** via the setup wizard (first start) or the Settings
  button → "Add New Account". Each account has a label, region, and the
  three OVH API keys.
- **Switch the active account** with the dropdown in the header. Catalog,
  checkout and billing operations run against the active account, and the
  UI shows its data.
- **Monitoring is never interrupted by a switch**: the background poller
  watches *every* monitored account's alerts at once. The account you switch
  away from keeps logging stock events, keeps firing notifications, and keeps
  its armed snipers live. Restocks on a background account appear in the
  browser tagged with the account's label (they never prefill the rush
  order form, which orders under the active account).
- **Monitoring is per account**, controlled by the "Monitoring: On/Off"
  button on the Monitor tab and shown as a badge on each account in
  Settings → Accounts. It is stored server-side, so it survives restarts and
  applies whether or not a browser is open. Three separate things, three
  separate controls:

  | What | Where | Scope |
  |------|-------|-------|
  | Background poller | Settings → App | all accounts (master switch) |
  | Monitoring | Monitor tab toggle | the active account only |
  | Live updates | header dot | this browser tab only |
- **Data is account-scoped**: alerts, checkout profiles, and orders belong
  to the account they were created under. Switching accounts shows only
  that account's data. The sniper orders under the alert's own account
  (not the currently active one), so an armed sniper keeps targeting the
  right region even after you switch.
- **Notifier settings are global** (shared across accounts) — alerts route
  to your configured Telegram/Discord/Slack/email regardless of account.

The first account created becomes active automatically. Deleting the
active account falls back to another account, or returns to the setup
wizard if none remain. Existing single-credential databases are migrated
to an account automatically on first run.

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
│   ├── logging_config.py    # setup_logging(): rotating file + in-app log handlers
│   ├── api/                 # Route handlers
│   │   ├── catalog.py       # Catalog endpoints + product spec extraction
│   │   ├── monitor.py       # SSE stock streaming + poll-interval
│   │   ├── logs.py          # Runtime log viewer (snapshot + SSE live tail)
│   │   ├── alert.py         # Alert CRUD + enable/disable + profile assignment
│   │   ├── checkout.py      # Rush order (one-shot)
│   │   ├── profiles.py      # Saved checkout profile CRUD (per-account)
│   │   ├── price_watch.py   # Price-drop watch CRUD (per-account)
│   │   ├── sniper.py        # Sniper arm/disarm/status
│   │   ├── insights.py      # History, patterns, price, promos, region activity
│   │   ├── orders.py        # Order management (live OVH list, detail, follow-up, waive, cancel)
│   │   ├── servers.py       # Owned dedicated servers: full control, capability-gated
│   │   ├── console.py       # Browser KVM: session brokering + WebSocket↔BMC relay
│   │   ├── accounts.py      # Multi-account CRUD + active switch + test
│   │   ├── settings.py      # Notification channel settings (Telegram/Discord/Slack/SMTP)
│   │   ├── account.py       # OVH account + payment methods + defaults + bills
│   │   └── errors.py        # OVH->HTTP error mapping
│   ├── models/schemas.py    # Pydantic request/response models
│   └── services/
│       ├── ovh_service.py   # OVH API wrapper (per-account registry)
│       ├── monitor.py       # Stock monitoring + background poller + SniperService
│       ├── notifier.py      # Telegram/Discord/Slack/email fan-out
│       ├── storage.py       # SQLite persistence (alerts, profiles, events, prices, orders)
│       ├── logbus.py        # In-memory log ring buffer + SSE pub/sub (Logs tab)
│       ├── server_features.py # Dedicated-server resource registry + capability probe
│       └── cache.py         # In-memory TTL cache
├── static/js/app.js         # Frontend SPA (vanilla JS, ~4600 lines)
├── static/css/input.css     # Tailwind v4 source
├── static/css/app.css       # Built/minified (do not edit)
├── templates/index.html    # SPA shell with cache-busted asset refs
├── tests/                   # pytest suite (364 tests, uses TestClient)
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

2. **Enable notifications** when prompted (toggle Monitoring on to grant permission)

3. **Keep the tab open** for banners and sound - though you don't have to:
   the server polls every account and fires your notification channels even
   with no browser connected, and the tab reconnects itself on load

4. **Use 1-second polling** for fastest detection

5. **Have payment ready** on your OVH account
