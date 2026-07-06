# AGENTS.md

Reference for AI sessions working on this repo. Read this first.

## Project

OVH Flash Sale Monitor — FastAPI backend + vanilla JS SPA frontend for
monitoring OVH ECO server flash sales and placing rush orders via the
OVH API. Python 3.10+, single-process, SQLite persistence.

## Environment

- **Python venv**: `.venv/` (Python 3.14). Use `.venv/bin/python` and
  `.venv/bin/pytest`, `.venv/bin/ruff`. The system `python` has no
  pytest/ruff installed.
- **Tailwind CSS binary**: `/tmp/tailwindcss` (standalone v4.3.2, not
  on PATH). The `make css` target calls `tailwindcss` which is NOT on
  PATH — invoke `/tmp/tailwindcss` directly instead.
- **PYTHONPATH**: tests require `PYTHONPATH=.` because the `app`
  package is not pip-installed (no editable install). Run tests as
  `PYTHONPATH=. .venv/bin/pytest`.

## Commands

```bash
# Tests (must use PYTHONPATH=. since app/ is not installed)
PYTHONPATH=. .venv/bin/pytest

# Lint
.venv/bin/ruff check app/ tests/ run.py

# Rebuild + minify CSS (uses the standalone binary in /tmp)
/tmp/tailwindcss --input static/css/input.css --output static/css/app.css --minify

# Run dev server
.venv/bin/uvicorn app.main:app --reload
# or
python run.py
```

The `Makefile` has `install`, `dev`, `test`, `lint`, `css`, `run`,
`clean` targets — but `make css` will fail because `tailwindcss` is
not on PATH; use the absolute path above.

## Workflow (follow every session)

1. **Before editing**: read the target file and its neighbors to
   match existing style and patterns.
2. **After code changes**:
   - Run lint: `.venv/bin/ruff check app/ tests/ run.py`
   - Run tests: `PYTHONPATH=. .venv/bin/pytest`
   - If you changed `static/css/input.css` or any class names in
     `templates/index.html` / `static/js/app.js`: rebuild CSS with
     `/tmp/tailwindcss`.
   - Bump cache busters in `templates/index.html`:
     - `app.css?v=N` (currently v=10)
     - `app.js?v=N` (currently v=20)
   - Commit with a short descriptive message matching the existing
     style (see `git log --oneline`). Use prefixes like `Fix:`,
     `Add`, `Catalog:`, `Humanize`, etc. Make one logical commit per
     change; split bug fixes from features.
3. **Do not commit unless the user asks**. The user explicitly
   requests commits in this repo.

## Architecture

```
app/
├── main.py              # FastAPI app + lifespan (starts background poller)
├── config.py            # pydantic-settings (env vars prefixed OVH_)
├── api/                 # Route handlers (one file per resource)
│   ├── catalog.py       # /api/catalog, /api/catalog/plans
│   ├── monitor.py       # SSE stream + poll-interval
│   ├── alert.py         # Alert CRUD + enable/disable
│   ├── checkout.py      # /api/checkout/rush (one-shot order)
│   ├── cart.py          # Granular cart lifecycle (legacy)
│   ├── profiles.py      # Saved checkout profile CRUD
│   ├── sniper.py        # Arm/disarm auto-order
│   ├── insights.py      # History, patterns, price, orders
│   ├── setup.py         # Credentials wizard
│   ├── account.py       # OVH account + payment methods + defaults
│   └── errors.py        # OVH→HTTP error mapping
├── models/schemas.py    # Pydantic request/response models
└── services/
    ├── ovh_service.py    # OVH SDK wrapper (singleton)
    ├── monitor.py       # Background poller + SSE fan-out + SniperService
    ├── notifier.py      # Telegram/Discord/Slack/email fan-out
    ├── storage.py       # SQLite persistence (singleton)
    └── cache.py         # In-memory TTL cache
static/js/app.js         # Frontend SPA (vanilla JS, no framework)
static/css/input.css     # Tailwind source
static/css/app.css       # Built/minified (do not edit — rebuild from input.css)
templates/index.html     # SPA shell with cache-busted asset refs
tests/                   # pytest suite (56 tests, uses TestClient)
```

## Key conventions

- **Singletons**: `ovh_service`, `storage`, `monitor_service`,
  `cache`, `settings` are module-level singletons with
  `get_*()` accessors. Tests reset them via the `isolated_state`
  fixture in `conftest.py` (monkeypatches `OVH_DB_PATH` and clears
  caches).
- **Sync OVH calls**: the `ovh` SDK uses `requests` (sync). All OVH
  calls in async handlers are wrapped with `asyncio.to_thread()`.
- **Prices**: OVH stores prices in **microcents** (integer, divide by
  10^8 for currency units). The `price` field on pricings is raw
  microcents; `formattedPrice` is OVH's pre-formatted string.
- **Setup fees**: OVH lists one-time setup/installation fees as a
  pricing entry with `interval=0`, `intervalUnit='none'`, distinct
  from monthly (`interval=1`, `intervalUnit='month'`). Both are
  surfaced in `addonPrices` from `/api/catalog/plans`.
- **ovhSubsidiary**: cart creation MUST pass `ovhSubsidiary`
  matching the endpoint (US/CA/EU). Without it, OVH US/CA returns
  404 "Invalid Cart ID" on all subsequent cart calls. The
  `_default_subsidiary()` method on OVHService handles this.
- **Frontend**: no framework, no build step for JS. `app.js` is a
  ~2200-line vanilla SPA using a custom `el()` DOM helper. Cache
  busting is via `?v=N` query strings on `<link>` and `<script>`.
- **CSS**: Tailwind v4 with `@source` directives in
  `static/css/input.css` pointing at `templates/index.html` and
  `static/js/app.js` for class detection. Output is minified to
  `static/css/app.css`.

## Things to watch for

- **`_iso()` in storage.py** must return `dt.isoformat()` — it was
  once an empty function and silently broke `notified_at` persistence.
- **`refreshCatalogSilent`** must handle the `{plans, addonPrices}`
  response shape (not just a bare array), or addon prices vanish
  after auto-refresh.
- **`max_price`** must be passed in rush-order requests from both
  the catalog "Order Now" form AND the monitor tab's rush form.
- **Cache busters** (`?v=N`) in `templates/index.html` must be
  bumped on every JS/CSS change or browsers serve stale assets.
