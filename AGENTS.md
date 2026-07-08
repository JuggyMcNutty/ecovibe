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
- **Test credentials**: `.env.test` contains real OVH US API
  credentials for querying the live API during development. Load them
  with `os.environ` and save to a temp DB to test catalog/checkout
  flows (see `app/services/storage.py` for the save flow).

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
      - `app.css?v=N` (currently v=26)
      - `app.js?v=N` (currently v=45)
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
│   ├── catalog.py       # /api/catalog, /api/catalog/plans (+ productSpecs)
│   ├── monitor.py       # SSE stream + poll-interval
│   ├── alert.py         # Alert CRUD + enable/disable
│   ├── checkout.py      # /api/checkout/rush (one-shot order) + legacy /{cart_id}
│   ├── cart.py          # Granular cart lifecycle (legacy)
│   ├── profiles.py      # Saved checkout profile CRUD
│   ├── sniper.py        # Arm/disarm auto-order
│   ├── insights.py      # History, patterns, price, orders
│   ├── setup.py         # Credentials wizard
│   ├── settings.py      # Notification channel settings (Telegram/Discord/Slack/SMTP)
│   ├── account.py       # OVH account + payment methods + defaults
│   └── errors.py        # OVH→HTTP error mapping
├── models/schemas.py    # Pydantic request/response models
└── services/
    ├── ovh_service.py    # OVH SDK wrapper (singleton)
    ├── monitor.py       # Background poller + SSE fan-out + SniperService
    ├── notifier.py      # Telegram/Discord/Slack/email fan-out
    ├── storage.py       # SQLite persistence (singleton)
    └── cache.py         # In-memory TTL cache
static/js/app.js         # Frontend SPA (vanilla JS, ~2600 lines)
static/css/input.css     # Tailwind source
static/css/app.css       # Built/minified (do not edit — rebuild from input.css)
templates/index.html     # SPA shell with cache-busted asset refs
tests/                   # pytest suite (69 tests, uses TestClient)
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
- **Product specs**: CPU/chassis/service details are in the catalog's
  top-level `products` array (linked via `plan.product`), under
  `blobs.technical.server`. The `_build_product_specs()` helper in
  `catalog.py` extracts these into a `productSpecs` map keyed by
  product name. Frontend uses this for CPU model, cores, frequency,
  benchmark score, SLA, anti-DDoS, and chassis info.
- **ovhSubsidiary**: cart creation MUST pass `ovhSubsidiary`
  matching the endpoint (US/CA/EU). Without it, OVH US/CA returns
  404 "Invalid Cart ID" on all subsequent cart calls. The
  `_default_subsidiary()` method on OVHService handles this.
- **Route ordering**: in `checkout.py`, `POST /rush` must be
  registered BEFORE `POST /{cart_id}` or FastAPI matches the
  wildcard route first, causing "Invalid Cart ID" 404s.
- **Addon labels**: addon cards use OVH's `invoiceName` as the
  primary label (from `addonPrices` map). The `humanizeAddon()`
  functions are only a fallback when no price entry exists.
- **Frontend**: no framework, no build step for JS. `app.js` is a
  ~2300-line vanilla SPA using a custom `el()` DOM helper. Cache
  busting is via `?v=N` query strings on `<link>` and `<script>`.
- **CSS**: Tailwind v4 with `@source` directives in
  `static/css/input.css` pointing at `templates/index.html` and
  `static/js/app.js` for class detection. Output is minified to
  `static/css/app.css`.
- **DB path**: defaults to an absolute path anchored to the project
  root via `BASE_PATH` in `config.py`, so credentials persist
  regardless of CWD. `OVH_DB_PATH` env var overrides.

## Things to watch for

- **`_iso()` in storage.py** must return `dt.isoformat()` — it was
  once an empty function and silently broke `notified_at` persistence.
- **`refreshCatalogSilent`** must handle the `{plans, addonPrices,
  productSpecs}` response shape (not just a bare array), or addon
  prices and product specs vanish after auto-refresh.
- **`max_price`** must be passed in rush-order requests from both
  the catalog "Order Now" form AND the monitor tab's rush form.
- **Route order** in `checkout.py`: `/rush` before `/{cart_id}` or
  FastAPI's wildcard match shadows the static route.
- **Cache busters** (`?v=N`) in `templates/index.html` must be
  bumped on every JS/CSS change or browsers serve stale assets.
- **Logging**: uvicorn's default config filters out logs from
  non-uvicorn loggers (even WARNING level). Use `print(...,
  file=sys.stderr)` for debug tracing that must appear in the
  console, or configure logging explicitly.
- **Shared `ovh.Client` concurrency**: the `OVHService` singleton's
  `ovh.Client` (and its bundled `requests.Session` + lazily-cached
  server-time delta) is used from multiple threads via
  `asyncio.to_thread` (monitor poller, rush orders, account
  endpoints). `OVHService._call` serialises all calls with a
  `threading.Lock` so the SDK's shared state is never touched
  concurrently. On a 403 "This application key is invalid" the lock
  holder resets `client._time_delta = None` and retries once - the
  SDK caches the delta forever, so clock drift (NTP step,
  suspend/resume) otherwise permanently breaks every signature and
  OVH reports the signature mismatch as an invalid application key.
- **Cart configuration endpoint**: the EU-only
  `/order/cart/{cartId}/eco/configuration` path 404s on ovh-us with
  "Got an invalid (or empty) URL". Use `/order/cart/{cartId}/item/{itemId}/configuration`
  instead (works on both EU and US). The `itemId` goes in the URL path,
  not the POST body.
- **Region config value**: OVH US expects `region=united_states`, NOT
  `us`. The `OVH_REGIONS` map in `app.js` maps each endpoint to its
  correct OVH region config value (`europe`, `united_states`, `canada`).
- **Live stock**: `/dedicated/server/datacenter/availabilities?planCode=X`
  returns per-DC availability for each RAM+storage combo. Availability
  values are `unavailable`, `comingSoon`, `1H-low`, `1H-high`, `72H`,
  etc. `comingSoon` is NOT orderable — treat as out-of-stock for the
  catalog OOS badge but show DC names in the detail panel so users can
  set up monitors.
- **Stock matching**: catalog addon codes and stock API codes use
  inconsistent naming. `addonShortCode()` strips the region suffix,
  `normalizeAddonCode()` rounds capacity numbers (512→500), and
  `addonCodesMatch()` also checks prefix match (catalog `ram-16g`
  matches stock `ram-16g-ecc-2133`).
- **OOS badge**: the catalog list badge checks only the included (free)
  memory+storage combo (`fam.default` from `addonFamilies`), not all
  combos. A plan is OOS if its default config is unavailable in all DCs.
- **Notifier settings**: stored in the DB `settings` table with
  `notifier_` prefix (e.g. `notifier_telegram_bot_token`). The notifier
  reads from DB first, then env vars. Secrets (bot tokens, SMTP
  passwords) are masked on GET. Masked values (containing `...`) are
  preserved on PUT so users don't re-enter them.
- **500 retry**: `OVHService._call` retries once on 500/502/503/504
  after a 0.5s backoff — OVH returns transient 500s that succeed on
  retry.
- **Settings UI**: notification settings live in the credentials-view
  (accessed via the Settings button in the header), NOT as a tab. A
  back button returns to the monitor view.
