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
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set environment variables (for your region)
export OVH_APPLICATION_KEY="your_key"
export OVH_APPLICATION_SECRET="your_secret"
export OVH_CONSUMER_KEY="your_consumer_key"
export OVH_ENDPOINT="ovh-eu"   # or ovh-us or ovh-ca

# 3. Run
python run.py
```

Open http://localhost:8000 in your browser.

## Requirements

- Python 3.9+
- OVH API credentials for your region

## Features

### Flash Sale Monitor
- **Real-time stock tracking** via SSE (Server-Sent Events)
- **Browser notifications** when desired configs become available
- **Sound alerts** - audio notification even when tab is backgrounded
- **1-10 second polling** configurable interval
- **One-click Rush Order** when stock is detected

### Server Catalog
- Browse ECO server catalog
- Filter by country/subsidiary (IE, FR, DE, UK, etc.)
- View configurations, pricing, and availability
- Quick-add servers to watchlist

### Checkout
- Full cart management
- Configure RAM, storage, bandwidth options
- Set datacenter and region
- Auto-pay and waive retraction options

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OVH_ENDPOINT` | `ovh-eu` | API endpoint (ovh-eu, ovh-us, ovh-ca) |
| `OVH_APPLICATION_KEY` | - | Your application key |
| `OVH_APPLICATION_SECRET` | - | Your application secret |
| `OVH_CONSUMER_KEY` | - | Your consumer key |
| `OVH_USE_CACHE` | `false` | Enable in-memory caching |
| `OVH_CACHE_TTL` | `300` | Cache TTL in seconds |

## Region-Specific Setup

### United States (ovh-us)

For US-based OVHcloud accounts:

```bash
export OVH_ENDPOINT="ovh-us"
export OVH_APPLICATION_KEY="your_us_application_key"
export OVH_APPLICATION_SECRET="your_us_application_secret"
export OVH_CONSUMER_KEY="your_us_consumer_key"
```

**US API Endpoints:**
- API Base: `https://api.us.ovhcloud.com/v1`
- Create App: `https://api.us.ovhcloud.com/createApp/`
- Create Token: `https://api.us.ovhcloud.com/createToken/`

### Europe (ovh-eu)

```bash
export OVH_ENDPOINT="ovh-eu"
export OVH_APPLICATION_KEY="your_eu_application_key"
export OVH_APPLICATION_SECRET="your_eu_application_secret"
export OVH_CONSUMER_KEY="your_eu_consumer_key"
```

**EU API Endpoints:**
- API Base: `https://eu.api.ovh.com/v1`
- Create App: `https://eu.api.ovh.com/createApp/`
- Create Token: `https://eu.api.ovh.com/createToken/`

### Canada (ovh-ca)

```bash
export OVH_ENDPOINT="ovh-ca"
export OVH_APPLICATION_KEY="your_ca_application_key"
export OVH_APPLICATION_SECRET="your_ca_application_secret"
export OVH_CONSUMER_KEY="your_ca_consumer_key"
```

**CA API Endpoints:**
- API Base: `https://ca.api.ovh.com/v1`
- Create App: `https://ca.api.ovh.com/createApp/`
- Create Token: `https://ca.api.ovh.com/createToken/`

## Getting OVH Credentials

1. **Create Application** - Visit your region's API console:
   - Europe: https://eu.api.ovh.com/createApp/
   - US: https://api.us.ovhcloud.com/createApp/
   - Canada: https://ca.api.ovh.com/createApp/

2. **Create Token** - Visit token creation page for your region:
   - Europe: https://eu.api.ovh.com/createToken/
   - US: https://api.us.ovhcloud.com/createToken/
   - Canada: https://ca.api.ovh.com/createToken/

3. **Required Permissions**:
   - GET/PUT/POST/DELETE on `/order/*`
   - GET on `/me`

**Note:** Credentials are region-specific. US credentials only work with `ovh-us`, EU credentials only work with `ovh-eu`.

## API Endpoints

```
GET  /api/catalog?country=IE          - Fetch server catalog
GET  /api/catalog/availability       - Check plan availability
GET  /api/catalog/plans               - List available plans
GET  /api/monitor/stream               - SSE real-time stock updates
GET  /api/monitor/availability         - Current stock status
POST /api/alerts                       - Create stock alert
GET  /api/alerts                       - List alerts
DELETE /api/alerts/{id}               - Remove alert
POST /api/cart                        - Create cart
POST /api/cart/{id}/server            - Add server
POST /api/cart/{id}/options           - Add options
POST /api/cart/{id}/config            - Set configuration
GET  /api/cart/{id}/summary           - Order summary
POST /api/checkout/{id}               - Place order
```

## Building a Binary

Requires Python 3.10-3.13 (not 3.14):

```bash
./build.sh python3.12
```

Binary output: `dist/ovh-flash-monitor/ovh-flash-monitor`

## Project Structure

```
ovh-gui/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # Environment/config
│   ├── api/                 # Route handlers
│   │   ├── catalog.py
│   │   ├── monitor.py       # SSE stock streaming
│   │   ├── alert.py
│   │   ├── cart.py
│   │   └── order.py
│   ├── models/schemas.py     # Pydantic models
│   └── services/
│       ├── ovh_service.py   # OVH API wrapper
│       ├── monitor.py       # Stock monitoring
│       └── cache.py
├── static/js/
│   └── app.js               # Frontend SPA
├── templates/
│   └── index.html           # UI with TailwindCSS
├── requirements.txt
├── run.py                   # Entry point
├── build.sh                 # Binary build
└── README.md
```

## Tips for Flash Sales

1. **Pre-configure everything** before the sale starts:
   - Credentials already set
   - Servers added to watchlist
   - Rush Order form pre-filled
   - Auto-pay enabled if you want instant checkout

2. **Enable notifications** when prompted by browser

3. **Keep the tab open** and monitoring active

4. **Use 1-second polling** for fastest detection

5. **Have payment ready** on your OVH account
