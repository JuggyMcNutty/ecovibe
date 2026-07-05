# OVH Flash Sale Monitor - TODO Plan

## Project Overview
**Primary Goal**: Never miss a flash server sale. Real-time stock monitoring with instant alerts and fast checkout.

## Tech Stack
- **Backend**: FastAPI + Uvicorn
- **Frontend**: HTML + TailwindCSS + vanilla JS
- **API**: python-ovh library
- **Real-time**: Server-Sent Events (SSE) for stock updates
- **Caching**: Optional in-memory cache

## Project Structure
```
ovh-gui/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entry
│   ├── config.py            # Environment/config loading
│   ├── api/
│   │   ├── catalog.py       # Catalog endpoints
│   │   ├── monitor.py       # Stock monitoring & SSE
│   │   ├── alert.py         # Alert management
│   │   ├── cart.py          # Cart & checkout
│   │   └── order.py         # Checkout endpoint
│   ├── models/
│   │   └── schemas.py       # Pydantic models
│   └── services/
│       ├── ovh_service.py   # OVH API wrapper
│       ├── cache.py         # Simple in-memory cache
│       └── monitor.py       # Stock monitoring service
├── static/
│   ├── js/
│   │   └── app.js           # Main SPA logic
│   │   └── audio.js         # Sound alert handling (optional)
├── templates/
│   └── index.html
├── requirements.txt
└── README.md
```

## Core Features

### 1. Stock Monitor Dashboard
- [x] List of plans to monitor (user selects desired configs)
- [x] Real-time availability status for each monitored plan
- [x] Color-coded stock indicators (green=available, red=out)
- [x] Polling interval configurable (1-10 seconds)
- [x] Start/stop monitoring controls

### 2. Real-Time Updates (SSE)
- [x] `GET /api/monitor/stream` - SSE endpoint streaming stock changes
- [x] `GET /api/monitor/availability?plans=XX` - Get current availability
- [x] Delta tracking (what changed since last check)
- [x] Automatic reconnection on disconnect

### 3. Stock Alerts
- [x] Browser Notification API integration
- [x] Sound alert when desired config becomes available
- [x] Desktop notification even when tab is backgrounded
- [x] Configurable alert per monitored server
- [x] `POST /api/alerts` - Create alert
- [x] `GET /api/alerts` - List alerts
- [x] `DELETE /api/alerts/{id}` - Remove alert

### 4. Rush Mode Checkout
- [x] Pre-fill checkout configurations
- [x] One-click "Rush Order" when stock detected
- [x] Minimal confirmation step
- [x] Pre-validated cart ready to go
- [x] Auto-pay and waive retraction options

### 5. Flash Sale Optimizations
- [x] 1-10 second polling intervals
- [x] SSE connection with auto-reconnect
- [x] Connection status indicator
- [x] Failed request retry

### 6. Catalog Browser
- [x] Browse all available ECO servers
- [x] Quick-add to watchlist from catalog
- [x] Country/subsidiary filter

### 7. Multi-Region Support
- [x] Support for OVH Europe (ovh-eu)
- [x] Support for OVH US (ovh-us)
- [x] Support for OVH Canada (ovh-ca)
- [x] Region-specific API credential setup
- [x] Region-specific URL links in UI

## Implementation Phases

### Phase 1: Foundation
- [x] Create `requirements.txt`
- [x] Setup FastAPI app skeleton
- [x] Create config module with env var loading
- [x] Setup simple in-memory cache with `use_cache=True/False` toggle

### Phase 2: OVH Service Layer
- [x] Create `ovh_service.py` - wrapped OVH client with error handling
- [x] Implement catalog fetching with caching toggle
- [x] Implement availability checking
- [x] Implement full cart lifecycle

### Phase 3: API Endpoints
- [x] `GET /api/catalog?country=IE` - Fetch server catalog
- [x] `GET /api/catalog/availability?planCode=XX` - Check server availability
- [x] `POST /api/cart` - Create cart
- [x] `GET /api/cart/{cartId}` - Get cart
- [x] `POST /api/cart/{cartId}/server` - Add server
- [x] `POST /api/cart/{cartId}/options` - Add options
- [x] `POST /api/cart/{cartId}/config` - Set config
- [x] `GET /api/cart/{cartId}/summary` - Order summary
- [x] `POST /api/checkout/{cartId}` - Checkout

### Phase 4: Flash Sale Monitor
- [x] `GET /api/monitor/stream` - SSE real-time stock updates
- [x] `GET /api/monitor/availability` - Current stock for watched plans
- [x] Monitor service with configurable polling
- [x] Stock change detection and diffing

### Phase 5: Alert System
- [x] Browser Notification API integration
- [x] Sound alert playback
- [x] Alert CRUD endpoints
- [x] Alert persistence in memory

### Phase 6: Rush Mode Checkout
- [x] Pre-fill checkout form from alert
- [x] Fast checkout flow
- [x] One-click order from alert
- [x] Checkout status tracking

### Phase 7: Frontend Dashboard
- [x] Monitor dashboard with real-time updates
- [x] Alert configuration UI
- [x] Sound toggle
- [x] Connection status indicator
- [x] Rush mode panel

### Phase 8: Polish
- [x] Error handling
- [x] Loading states
- [ ] README with flash sale tips

## Flash Sale Best Practices
1. **Credentials pre-configured** - No time wasted on auth
2. **Cache disabled** - Always fresh stock data
3. **Short poll interval** - 1-3 seconds for real-time
4. **Sound alerts** - Don't need to watch screen constantly
5. **Rush mode** - One-click checkout when stock detected
6. **Payment method ready** - Auto-pay enabled or card on file

## Key Implementation Notes
- **SSE**: Server-Sent Events for real-time stock updates (simpler than WebSocket)
- **Polling**: Monitor service polls OVH API at configurable intervals
- **Alerts stored in memory** - Lost on restart (acceptable for MVP)
- **Browser Notifications**: Require user permission, work even when tabbed out
- **Sound**: Base64-encoded WAV sound embedded in JS for instant playback

## Usage for Flash Sales

### 1. Setup
```bash
export OVH_APPLICATION_KEY=your_key
export OVH_APPLICATION_SECRET=your_secret
export OVH_CONSUMER_KEY=your_consumer_key
uvicorn app.main:app --reload
```

### 2. Configure Alerts
1. Open http://localhost:8000
2. Load catalog and select desired server plan
3. Click "Watch" to add monitoring
4. Enable browser notifications when prompted
5. Click "Start Monitor"

### 3. Rush Order
When stock is detected:
1. Stock alert panel appears with "RUSH ORDER" button
2. Click to auto-fill the rush order form
3. Pre-configure RAM, storage, bandwidth, datacenter
4. Enable auto-pay for instant checkout
5. Click "Quick Order"

### 4. Be Fast
- Keep the tab open and monitoring active
- Sound alerts will notify you even when tabbed away
- Have payment method ready on OVH account
