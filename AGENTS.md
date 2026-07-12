# AGENTS.md

Reference for AI sessions working on this repo. Read this first.

## Project

ECOVibe — FastAPI backend + vanilla JS SPA frontend for
monitoring OVH ECO server flash sales and placing rush orders via the
OVH API. Python 3.10+, single-process, SQLite persistence.

## Design Philosophy
When developing or coming up with decisions on this project always choose to do the proper and modern choice. DO NOT come up with hacky solutions, and choose to solve bugs when they are noticed.

## Environment

- **Python venv**: `.venv/` (Python 3.14). Use `.venv/bin/python` and
  `.venv/bin/pytest`, `.venv/bin/ruff`. The system `python` has no
  pytest/ruff installed. this venv is build with pyenv and is installed into system
- **Tailwind CSS binary**: `/tmp/tailwindcss` (standalone v4.3.2, not
  on PATH). The `make css` target calls `tailwindcss` which is NOT on
  PATH — invoke `/tmp/tailwindcss` directly instead.
- **PYTHONPATH**: tests require `PYTHONPATH=.` because the `app`
  package is not pip-installed (no editable install). Run tests as
  `PYTHONPATH=. .venv/bin/pytest`.
- **Test credentials**: `.env.test` contains real OVH US AND CA API
  credentials for querying the live API during development. Load them
  with `os.environ` and save to a temp dev DB to test catalog/checkout
  flows (see `app/services/storage.py` for the save flow).
  **dev to test enviroment**: we are building in the dev folder, when the user runs the current iteration of the project they always rsync into the test enviroment which is currently located at `/var/home/corpeder/Documents/EcoVibe`

## Security

- **No in-app authentication.** The app is a single-user tool; it relies
  on a reverse proxy (Caddy/nginx with HTTP Basic Auth) for access
  control when exposed publicly. See README.md > Deployment.
- **Localhost-only by default.** `python run.py` binds `127.0.0.1`
  (configurable via `OVH_HOST`/`OVH_PORT`). Set `OVH_HOST=0.0.0.0`
  only behind a reverse proxy.
- **CSRF middleware** (`CsrfMiddleware` in `app/main.py`): state-changing
  requests (`POST`/`PUT`/`PATCH`/`DELETE`) to `/api/*` are blocked unless
  they carry `X-Requested-With: XMLHttpRequest` OR have a same-origin
  `Origin`/`Referer`. Requests with no `Origin`/`Referer` (curl, scripts,
  TestClient) are allowed. The SPA's `apiRequest()` in `static/js/app.js`
  sends the `X-Requested-With` header on every call. Tests live in
  `tests/test_security.py`. Basic Auth credentials are auto-attached by
  browsers on cross-origin requests, so this middleware is required even
  behind a proxy.

## Commands

```bash
# Tests (must use PYTHONPATH=. since app/ is not installed)
PYTHONPATH=. .venv/bin/pytest

# Lint
.venv/bin/ruff check app/ tests/ run.py

# Rebuild + minify CSS (uses the standalone binary in /tmp if not found redownload the static binary)
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
   - Cache busters are **automatic** — no manual `?v=N` bumping.
     `templates/index.html` is a Jinja2 template rendered with
     `{{ css_hash }}` / `{{ js_hash }}` (SHA256[:12] of file contents,
     computed at runtime by `app/utils/cache_buster.py`, memoised
     with `lru_cache`). Editing `app.css`/`app.js` invalidates the
     cache on the next request. `CachedStaticFiles` in `main.py`
     serves any `/static/...?v=` request with
     `Cache-Control: public, max-age=31536000, immutable`.
   - **Commit after changes**: use git to commit logical units with a
     short descriptive message matching the existing style (see
     `git log --oneline`). Use prefixes like `Fix:`, `Add`,
     `Catalog:`, etc. Make one logical commit per change;
     split bug fixes from features. Run, from the project root:
     ```bash
     git add <files>          # stage only the intended files
     git commit -m "<message>" # concise, single line
     ```
     Before committing, inspect `git status` and `git diff`; stage
     only intended files and never commit secrets.
  - Keep the AGENTS.md and README.md up to date as we make commits to this project so future AI sessions can easily get up to speed witht this project.

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
│   ├── profiles.py      # Saved checkout profile CRUD (per-account)
│   ├── sniper.py        # Arm/disarm auto-order
│   ├── insights.py      # History, patterns, price, orders (local)
│   ├── orders.py        # Order management (live OVH list, detail, follow-up, waive)
│   ├── accounts.py      # Multi-account CRUD + active switch + test
│   ├── settings.py      # Notification channel settings (Telegram/Discord/Slack/SMTP)
│   ├── account.py       # OVH account + payment methods + defaults
│   └── errors.py        # OVH→HTTP error mapping
├── models/schemas.py    # Pydantic request/response models
├── utils/
│   └── cache_buster.py  # Content-hash cache busting for static assets
└── services/
    ├── ovh_service.py    # OVH SDK wrapper (per-account registry)
    ├── monitor.py       # Background poller + SSE fan-out + SniperService
    ├── notifier.py      # Telegram/Discord/Slack/email fan-out
    ├── storage.py       # SQLite persistence (singleton)
    └── cache.py         # In-memory TTL cache
static/js/app.js         # Frontend SPA (vanilla JS, ~3300 lines)
static/css/input.css     # Tailwind source
static/css/app.css       # Built/minified (do not edit — rebuild from input.css)
templates/index.html     # SPA shell with cache-busted asset refs
tests/                   # pytest suite (108 tests, uses TestClient)
```

## Multi-account model

The app stores N OVH credential sets (one row per account in the
`accounts` table) with a single **active account** id in `settings`.
All catalog/monitor/checkout/billing operations run against the active
account; alerts, profiles, and orders are scoped to the account they
were created under (`account_id` column on each table).

- **OVHService registry** (`ovh_service.py`): `_services: dict[str, OVHService]`
  keyed by account_id, each a cached `OVHService(endpoint, ak, as, ck)`
  with its own `ovh.Client` + `threading.Lock`. `get_active_ovh_service()`
  resolves the active account; `get_ovh_service(account_id)` targets a
  specific one (used by the sniper). `reset_ovh_service(account_id)` /
  `reset_all_services()` invalidate the cache. OVHService takes creds in
  its constructor (no DB read) — construct directly in tests.
- **Active account**: stored in `settings.active_account_id`; cached by
  the registry. Switching via `PUT /api/accounts/active` calls
  `monitor.reload()` which clears in-memory alerts, stock cache, and
  `_last_stock` (the stock-diff baseline) and re-reads the active
  account's alerts. If `reload()` fails, the active account is reverted
  so the monitor doesn't poll the new account with the old account's
  alerts.
- **Monitor**: polls the active account only (Decision 1A). The poller
  early-returns when there are no enabled alerts, so idle polling does
  no OVH network I/O. Multi-account simultaneous polling is a future
  iteration; `_poll_once` is structured with a `_poll_account`-style
  seam to make that jump cheap.
- **Sniper**: fires under the alert's own `account_id`
  (`get_ovh_service(alert.account_id)`), not the active one — so an
  armed sniper keeps targeting the right region after a switch.
- **Notifier + checkout_defaults**: global (not per-account).
- **Frontend account switch**: `switchAccount()` in `app.js` tears down
  the SSE monitor + catalog auto-refresh, resets 8 account-scoped state
  fields, and uses a request-generation token (`_switchGen`) so stale
  async responses from the previous account are ignored after each
  `await`.
- **Network error wrapping**: `OVHService._do_call` wraps non-`APIError`
  exceptions (`ConnectionError`, `TimeoutError`, `SSLError`) in
  `OVHServiceError` so they surface as proper error responses instead of
  unhandled 500s.
- **Migration**: on `init()`, if `accounts` is empty and the legacy
  `credentials` table has rows, one account is created from them, set
  active, and all data rows backfilled with its id. Idempotent.

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
- **Subsidiary is endpoint-scoped**: each endpoint only accepts its
  own subsidiaries (`OVHService.valid_subsidiaries()`; ovh-ca accepts
  ONLY `CA` — verified live, `WORLD`/`US`/`FR`/`IE` all 400). The
  currency selector maps a display currency to a subsidiary, but that
  subsidiary is only fetched when the active endpoint accepts it;
  otherwise `catalogSubsidiaryForCurrency()` (app.js) and
  `_resolve_subsidiary()` (catalog.py) fall back to the endpoint's
  default and rely on FX conversion. A "CA/world" account billed in USD
  otherwise sends `?country=US` to ca.api.ovh.com → 400
  "invalid ovhSubsidiary". `detectCatalogCurrency()` always reads the
  catalog's real currency from the response (not the display currency).
- **Catalog currency source**: ovh-ca leaves `currencyCode` null on
  individual pricing entries and exposes the native currency only via
  the catalog's top-level `locale.currencyCode` (verified live: pricings
  have `currencyCode: None`, `locale.currencyCode: "CAD"`). `/api/catalog/plans`
  propagates `locale.currencyCode` into each `addonPrices[code].currencyCode`
  and a top-level `currencyCode` field; `detectCatalogCurrency()` (app.js)
  prefers that field. Without it, CAD microcents would be mislabelled as
  EUR and FX-converted against the wrong base.
- **Route ordering**: in `checkout.py`, `POST /rush` must be
  registered BEFORE `POST /{cart_id}` or FastAPI matches the
  wildcard route first, causing "Invalid Cart ID" 404s.
- **Addon labels**: addon cards use OVH's `invoiceName` as the
  primary label (from `addonPrices` map). The `humanizeAddon()`
  functions are only a fallback when no price entry exists.
- **Order line items**: OVH's `/me/order/{id}/details` splits every
  ordered component into separate rows by `detailType` (`INSTALLATION` =
  one-time setup fee, `DURATION` = recurring monthly) grouped under a
  hierarchical `domain` (`*001` = the server, `*001.001`, `*001.002`, ...
  = its options), so an 8-row order is really ~4 items. `_group_line_items()`
  in `orders.py` collapses them by domain into one `line_items` entry each
  (`setup_price`/`recurring_price` merged, label cleaned of OVH's "rental -
  1 month" boilerplate via `_pick_label`); `get_order_detail` returns
  both the raw `details` and grouped `line_items`, and the frontend renders
  `line_items` (falling back to `details`).
- **Order title (server name)**: OVH server orders carry no name on the order
  object, so the list title is derived from the line items. `_name_from_details`
  picks the **server** line — the priciest grouped item (options are
  included/$0), *not* the first detail row (which is often the RAM) — falling
  back to a real `domain` hostname. The derived name is persisted; a title cached
  wrong won't self-heal on a plain list load, so the "Refresh all" button hits
  `GET /api/orders?refresh=true`, which re-derives names instead of trusting the
  cache (still `name_budget`-limited so it can't hang).
- **Frontend**: no framework, no build step for JS. `app.js` is a
  ~3300-line vanilla SPA using a custom `el()` DOM helper. Cache
  busting is automatic via content-hash query strings
  (`?v=<sha256[:12]>`) injected by `app/utils/cache_buster.py`.
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
  (Resolved — now correct, kept as a historical note.)
- **`refreshCatalogSilent`** must handle the `{plans, addonPrices,
  productSpecs, currencyCode}` response shape (not just a bare array),
  or addon prices and product specs vanish after auto-refresh.
  (Resolved — now handles the full shape including `currencyCode`.)
- **`max_price`** must be passed in rush-order requests from both
  the catalog "Order Now" form AND the monitor tab's rush form.
  (Resolved — all three paths pass it: catalog form, monitor form, sniper.)
- **Route order** in `checkout.py`: `/rush` before `/{cart_id}` or
  FastAPI's wildcard match shadows the static route.
- **Cache busters** are automatic (content-hash based, see
  `app/utils/cache_buster.py`); no manual `?v=N` bumping is needed.
- **Logging**: uvicorn's default config only configures its own loggers.
  `run.py` extends `LOGGING_CONFIG` to add an `"app"` logger so all
  `app.*` loggers emit to the console at INFO level. Use
  `logger.info()`/`logger.warning()` — never `print(file=sys.stderr)`.
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
  `_reset_time_delta()` guards with `hasattr` and falls back to a full
  client reconstruction if the SDK renames/removes the private attribute.
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
  see upcoming availability.
- **Stock matching**: catalog addon codes and stock API codes use
  inconsistent naming. `addonShortCode()` strips the region suffix,
  `normalizeAddonCode()` maps known capacity mismatches via an explicit
  equivalence table (512→500, 1920→1900, 3840→3800), and
  `addonCodesMatch()` also checks prefix match (catalog `ram-16g`
  matches stock `ram-16g-ecc-2133`). `refreshStockForAllPlans` logs a
  `console.warn` when a plan's default combo fails to match any stock
  entry, so silent match failures are detectable.
- **OOS badge**: the catalog list badge checks only the included (free)
  memory+storage combo (`fam.default` from `addonFamilies`), not all
  combos. A plan is OOS if its default config is unavailable in all DCs.
- **Notifier settings**: stored in the DB `settings` table with
  `notifier_` prefix (e.g. `notifier_telegram_bot_token`). The notifier
  reads from DB first, then env vars. Secrets (bot tokens, SMTP
  passwords, Discord/Slack webhook URLs) are masked on GET. Masked
  values (containing `...`) are preserved on PUT so users don't re-enter
  them.
- **500 retry**: `OVHService._call` retries once on 500/502/503/504
  after a 0.5s backoff, but **only for non-POST methods** — POST (e.g.
  checkout) is never retried to prevent duplicate orders when OVH
  processed the request but the response was lost.
- **Settings UI**: separate full-page views (not monitor tabs),
  each a direct child of `#app` toggled by `showView()`:
  `#setup-view` (first-run wizard, shown only when unconfigured — add the
  first account, then land on the monitor), `#accounts-view` (manage OVH
  accounts: list + inline add/edit editor), `#notifications-view`
  (Telegram/Discord/Slack/SMTP), and `#billing-view` (Default Checkout
  Preferences — the `checkout-defaults-form`, moved out of the monitor's
  Billing tab, which now shows only OVH account info + payment methods).
  The header "Settings" gear opens `#accounts-view` via
  `showSettings('accounts')`; a sub-nav of `[data-settings-nav]` buttons
  switches between Accounts / Notifications / Billing (`showSettings(page)`
  also loads that page's data); `.settings-back-btn` returns to the monitor. The
  setup wizard has NO skip — an account is required. Setup uses
  `setup-*` field ids and `saveSetupAccount()` (onboarding: activate +
  go to monitor); the accounts editor uses `acct-*` ids and
  `saveManagedAccount()` (never changes the active account); both share
  `submitAccount()`. Deleting the last account returns to `#setup-view`.
- **Currency (display-only)**: a currency selector (EUR/USD/GBP/CAD)
  converts prices for display via cached ECB/Frankfurter FX rates
  (`app/services/currency.py`, 24h cache, EUR-base). It defaults to the
  account's billing currency from `/me` and is a view preference (not
  persisted). OVH charges in the catalog's native currency regardless.
  The catalog's `currencyCode` is passed through in `addonPrices` so the
  frontend can convert. By default prices show OVH's **native** catalog
  currency (`priceMode='ovh'` — OVH's exact `formattedPrice`); the
  "Convert pricing" checkbox (always visible) opts into FX conversion to
  the selected currency (`priceMode='fx'`). All price rendering goes
  through `effectiveDisplayCurrency()` (=`catalogCurrency` in 'ovh' mode,
  `displayCurrency` in 'fx' mode) so the toggle has one effect. The old
  per-country catalog dropdown was replaced by the currency selector.
  `max_price` stays in the catalog's native currency (microcents) and is
  labelled with the currency code; the budget-guard error message uses
  the 10^8 divisor + currency code (the old 10^6 `price_eur` field was
  removed — it was wrong by 100× and unused).
