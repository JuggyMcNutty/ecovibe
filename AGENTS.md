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
  **dev to test enviroment**: we are building in the dev folder, when the user runs the current iteration of the project they always rsync into a separate test environment (e.g. `/path/to/EcoVibe-test`)

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

## Release process

The remote URL, transport (SSH vs HTTPS), credential setup, and how `gh`
was installed are all per-deployment — check `git remote -v` and
`gh auth status` rather than assuming any of them.

- **Cutting a release**: bump `version` in `pyproject.toml`, commit it,
  then:
  ```bash
  git tag -a vX.Y.Z -m "EcoVibe vX.Y.Z"
  git push origin vX.Y.Z
  gh release create vX.Y.Z --generate-notes
  ```
  `--generate-notes` auto-summarizes commits since the previous tag.
  Current released version: **v0.2.0** (first public release, 2026-07-14).

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
     computed at runtime by `app/utils/cache_buster.py`). The hash is
     cached on `(path, mtime_ns, size)`, so editing `app.css`/`app.js`
     invalidates it on the next request — **including under a running
     server**. It was previously keyed on the path alone, which meant
     `python run.py` (no `--reload`) kept emitting the pre-deploy `?v=`
     until someone restarted the process, so rsync'ing new assets into a
     live test environment silently served the OLD JS. `CachedStaticFiles`
     in `main.py` serves any `/static/...?v=` request with
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
     Always make commits per feature or bug fix and dont make very large commits.
  - Keep the AGENTS.md and README.md up to date as we make commits to this project so future AI sessions can easily get up to speed witht this project.

## Architecture

```
app/
├── main.py              # FastAPI app + lifespan (starts background poller)
├── config.py            # pydantic-settings (env vars prefixed OVH_)
├── logging_config.py    # setup_logging(): rotating file + LogBus handlers
├── api/                 # Route handlers (one file per resource)
│   ├── catalog.py       # /api/catalog, /api/catalog/plans (+ productSpecs)
│   ├── monitor.py       # SSE stream + poll-interval
│   ├── logs.py          # Runtime log viewer: GET /api/logs + SSE /api/logs/stream
│   ├── alert.py         # Alert CRUD + enable/disable
│   ├── checkout.py      # /api/checkout/rush (one-shot order)
│   ├── profiles.py      # Saved checkout profile CRUD (per-account)
│   ├── price_watch.py   # Price-drop watch CRUD (per-account)
│   ├── sniper.py        # Arm/disarm auto-order
│   ├── insights.py      # History, patterns, price, promos, region activity, catalog changes
│   ├── orders.py        # Order management (live OVH list, detail, follow-up, waive)
│   ├── servers.py       # Owned dedicated servers (read-only list + detail)
│   ├── accounts.py      # Multi-account CRUD + active switch + test
│   ├── settings.py      # Notification channel settings (Telegram/Discord/Slack/SMTP)
│   ├── account.py       # OVH account + payment methods + defaults + bills
│   └── errors.py        # OVH→HTTP error mapping
├── models/schemas.py    # Pydantic request/response models
├── utils/
│   └── cache_buster.py  # Content-hash cache busting for static assets
└── services/
    ├── ovh_service.py    # OVH SDK wrapper (per-account registry)
    ├── monitor.py       # Background poller + SSE fan-out + SniperService
    ├── notifier.py      # Telegram/Discord/Slack/email fan-out
    ├── storage.py       # SQLite persistence (singleton)
    ├── logbus.py        # In-memory log ring buffer + SSE pub/sub (Logs tab)
    └── cache.py         # In-memory TTL cache
static/js/app.js         # Frontend SPA (vanilla JS, ~4600 lines)
static/css/input.css     # Tailwind source
static/css/app.css       # Built/minified (do not edit — rebuild from input.css)
templates/index.html     # SPA shell with cache-busted asset refs
tests/                   # pytest suite (306 tests, uses TestClient)
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
- **Credential verification on save**: `POST`/`PUT /api/accounts`
  verify the credentials against OVH's `GET /me` BEFORE persisting
  (hard block — invalid or wrong-region keys get a 400 and nothing is
  saved; the response carries the verified `nichandle` on success).
  Updates verify the MERGED credentials (empty fields preserve stored
  values). The seam is `accounts._verify_credentials`, stubbed by
  `conftest.isolated_state` so offline tests can create fake accounts.
- **Active account**: stored in `settings.active_account_id`; cached by
  the registry. It scopes the **UI and one-shot operations** (catalog,
  checkout, billing, `GET /api/alerts`) — NOT the poller, which watches
  every account. Switching via `PUT /api/accounts/active` calls
  `monitor.reload()`, a re-sync (re-read alerts + account metadata, prune
  state for deleted accounts) that deliberately **preserves stock
  baselines** so a switch never re-primes or drops an edge. If `reload()`
  fails the active account is reverted, so the app never proceeds with a
  half-applied switch.
- **Monitor: polls EVERY account** whose `monitoring_enabled` is set
  (supersedes the old active-account-only Decision 1A). `_poll_once` groups
  enabled alerts by `account_id`, adds accounts whose region ticker is on,
  drops the ones with monitoring off, and polls each group **concurrently**
  (`asyncio.gather`) via `_poll_one_account` → `_poll_account(account_id,
  service, plan_codes, region_enabled)` under that account's own
  credentials; each `OVHService` has its own client lock, so a cycle costs
  the slowest account, not their sum. It early-returns before building any
  service when nothing is watched, so idle polling still does no OVH
  network I/O. This is what makes monitoring survive an account switch:
  insight data keeps accruing and alerts keep firing for accounts the user
  isn't looking at.
- **Per-account state keys**: `_stock_cache`, `_last_stock` and `_primed`
  are keyed by `(account_id, plan_code)` — two accounts may watch the same
  plan code in different regions with completely different stock.
  `_region_enabled` / `_last_region_avail` / `_region_primed` are keyed by
  `account_id`. A `None` key is the pre-account bucket (fresh install or a
  test), resolved through `get_active_ovh_service()`.
- **Event tagging**: every `stock_update` diff and `region_restock` event
  carries `account_id` + `account_label`, and notifications get an
  `account_label` suffix (`_account_suffix()` in `notifier.py`) — with all
  accounts polled at once, an alert has to say which one it came from. The
  frontend applies stock dots only for the active account but shows every
  account's restock, tagged.
- **Deleted accounts**: `delete_account` drops only the `accounts` row (its
  history rows stay queryable), so `_alerts_from_rows` skips alerts whose
  `account_id` is not a live account — otherwise they'd inflate the alert
  counts and sniper status with entries that have no credentials.
- **Batch polling**: with 2+ watched plans (or the region ticker on),
  `_fetch_availability_map` makes ONE unfiltered
  `/dedicated/server/datacenter/availabilities` call (~28k entries /
  ~680 plans / ~1.3s, verified live) instead of a call per plan, and
  groups it in-process. Batch mode clamps the poll interval to
  `BATCH_MIN_POLL_INTERVAL` (3s); a single watched plan keeps the small
  filtered call and its 1s snipe fidelity. A failed batch fetch keeps
  every plan's baseline (it must not read as a region-wide sell-out).
  There is no server-side multi-planCode filter (comma lists return 0
  rows — verified live).
- **Silent baseline priming**: the FIRST poll for an
  `(account, plan)` after startup only records the baseline — no SSE
  broadcast, notifications, or stock events (an empty baseline would
  otherwise mark everything "newly available" on every restart). Armed
  snipers still fire on already-available stock during priming. The
  region ticker primes the same way (`_region_primed`). An account switch
  does NOT re-prime any more — that account was being polled all along.
- **Region restock ticker** — **per-account**
  (`accounts.region_ticker_enabled` column, set via
  `PUT /api/accounts/{id}/monitoring` alongside the monitoring master
  switch; `GET/PUT /api/monitor/region-watch` remains as an active-account
  alias onto the same setter; the old global
  `settings.region_ticker_enabled` row is migrated onto the active account
  by `Storage._migrate_region_ticker_setting` and deleted). Each ticking
  account's batch cycle diffs ITS ENTIRE region; unwatched plans'
  transitions are logged to stock_events (watched plans stay with the
  per-plan loop — no duplicate rows) and a `region_restock` SSE event
  (capped 50 plans × 5 FQNs) is broadcast alongside the classic
  `stock_update`. Feed API: `GET /api/insights/region-activity`.
  `insights/summary` defaults to `watched_only=true` so ticker volume
  doesn't drown the overview.
- **Stock-event retention**: the monitor prunes `stock_events` hourly —
  rows older than `OVH_STOCK_EVENT_RETENTION_DAYS` (90) deleted, table
  hard-capped at `OVH_STOCK_EVENT_MAX_ROWS` (500k, oldest dropped).
- **Price watches + promo scan**: every `OVH_PRICE_CHECK_INTERVAL`
  seconds (900; 0 disables) the monitor fetches the catalog once
  **per account** (not just the active one, same reason as the poller),
  evaluates enabled `price_watches` (notify at/below threshold; re-fire
  only when the price moves; watched prices logged to price_history),
  and scans every `pricings[].promotions` entry — new promos
  (sha256-hash-deduped in `promo_events`) notify via all channels and
  feed `GET /api/insights/promos`. The populated promotions shape is
  UNVERIFIED (always empty outside sales) — the scan reads it
  defensively; check against live data during the next OVH sale.
  NOTE: `price_watches`/`promo_events` upserts are manual
  SELECT-then-write because SQLite UNIQUE treats NULL account_ids as
  distinct (ON CONFLICT/OR IGNORE would silently duplicate).
- **Catalog watch**: `_diff_catalog()` runs at the tail of
  `_check_prices_and_promos`, reusing the catalog **already fetched** for the
  price watches and promo scan — it must stay there, a separate fetch would
  double the catalog traffic for no new data. It diffs the account's plan
  codes against `catalog_plans` (the persisted snapshot) and writes
  `added`/`removed` rows to `catalog_changes`, broadcasts a `catalog_change`
  SSE event via `_publish()`, and notifies once per account per cycle
  (`notify_catalog_change`, never per plan — the promo campaign lesson).
  Three rules, all load-bearing:
  (1) **The snapshot is in SQLite, not memory** — a restart compares against
  the last observed catalog, so a plan added while the app was down is still
  reported. (2) **First scan for an account primes silently**
  (`apply_catalog_diff(..., log_changes=False)`): with no baseline the whole
  ~700-plan catalog would read as new. (3) **Bad-response guards** — an empty
  `plans` list, or removals exceeding half the snapshot, leave the snapshot
  untouched and log a WARNING; a truncated fetch must never read as OVH
  retiring a region (same reasoning as a failed batch availability fetch
  keeping every plan's baseline). Two switches in Settings → App:
  `catalog_watch_enabled` (track at all) and `catalog_watch_notify` (fan out
  to channels), read live per cycle via `app_setting_bool`. `catalog_changes`
  is not pruned — additions/removals are rare enough to be unbounded in
  practice, unlike `stock_events`.
- **Delivery watch (orders + owned servers)**: every
  `OVH_ORDER_CHECK_INTERVAL` seconds (300; 0 disables, Settings → App)
  `_maybe_check_orders_and_servers` runs `_check_orders` and `_check_servers`
  per account, behind the same `monitoring_enabled` gate as everything else.
  It exists because **the Orders and Servers tabs only talk to OVH when they
  are opened** — before it, an order sat at whatever status it had when the tab
  was last viewed (a real order was stored `delivering` while OVH said
  `delivered`), and OVH's own delivery email was the only notice a server was
  ready. Four rules, all load-bearing:
  (1) **Notify only on a transition from a status we already knew.** An order id
  seen for the first time is recorded silently — otherwise a fresh install would
  fan out one message per historical `delivered` order. This is the catalog
  watch's priming lesson expressed as a rule about transitions rather than a
  first-scan branch, so it also covers an account that gains orders later.
  (2) **Only terminal statuses notify** (`delivered`/`cancelled`,
  `NOTIFY_ORDER_STATUSES`). Intermediate churn (`checking`→`delivering`) is
  persisted and streamed over SSE but never fanned out.
  (3) **Terminal orders are never re-queried** (`TERMINAL_ORDER_STATUSES`), and
  status calls are capped at `ORDER_STATUS_BUDGET` (10) per account per cycle,
  newest first — every OVH call serialises on the account's client lock (same
  reasoning as `name_budget` in `api/orders.py`). A settled account costs one
  `/me/order` call per cycle.
  (4) **The server snapshot primes behind an explicit marker**
  (`settings.server_watch_primed_<account_id>`), not the emptiness of
  `owned_servers`: an empty snapshot means both "never scanned" AND "owns no
  servers", and buying your first server through this app is exactly the case
  that must not be swallowed as a baseline. Note there is deliberately **no
  "more than half missing" guard** here (unlike the catalog watch) — a truncated
  ~700-plan catalog is indistinguishable from a retired region, whereas
  `/dedicated/server` returns a short, complete list where `[]` is a legitimate
  answer; a failed fetch raises and leaves the snapshot untouched, which is the
  guard that matters. `owned_servers` is snapshot-only (no history table): the
  Servers tab renders the live OVH list, so nothing would consume one.
  SSE events `order_update` / `server_change` let an open browser refresh the
  relevant tab in place instead of showing pre-delivery state indefinitely.
- **Sniper**: fires under the alert's own `account_id`
  (`get_ovh_service(alert.account_id)`), not the active one — so an
  armed sniper keeps targeting the right region after a switch. Sniper
  matching in `_poll_account` is **level-triggered** (any matching config
  *currently* orderable), unlike notifications, which stay edge-triggered
  on `newly_available`: a sniper must also fire on stock that was already
  there when it was armed. `SniperService.maybe_fire`'s per-arm
  `fqns_seen` set is what stops duplicate orders. (This replaced the old
  `_sweep_snipers()` safety net, which existed only because non-active
  accounts weren't polled — it is now redundant and was removed.)
  Snipers are **disarmed automatically** when their account is deleted
  (`disarm_for_account`), their alert is deleted, or their alert is
  paused (a disabled alert is never polled, and a "paused" alert must
  never silently auto-order; the disable endpoint reports
  `sniper_disarmed: true` and the UI toasts it). Re-enabling does NOT
  re-arm — arming is always an explicit user action.
- **Notifier + checkout_defaults**: global (not per-account).
- **Frontend account switch**: `switchAccount()` in `app.js` tears down
  the SSE monitor + catalog auto-refresh, resets 8 account-scoped state
  fields, reloads all scoped data for the new account, and uses a
  request-generation token (`_switchGen`) so stale async responses from the
  previous account are ignored after each `await`. The full Orders tab is
  lazy-loaded on tab switch, so `switchAccount()` also reloads it in place
  (`loadOrdersTab()`) when it's the visible tab — otherwise it would keep
  showing the previous account's orders until the user re-opened the tab.
  It **restarts the SSE stream in a `finally`** if it was running (guarded
  by `_switchGen`): the stream is account-agnostic and the server never
  stopped polling, so leaving it torn down made the monitor look "stopped"
  after every switch. The Start/Stop button only controls THIS browser's
  live view — the `#monitor-poller-state` hint (fed by
  `refreshMonitorRunState()` ← `GET /api/monitor/status`'s
  `running`/`accounts_polled`/`total_alerts_count`) says so.
- **THE THREE MONITORING CONCEPTS.** These are separate things and must
  never be conflated again — doing so is what produced a UI showing
  "Start Monitor" + "Disconnected" + "Background poller running" all at
  once, and a start/stop button that appeared to act on every account:

  | Concept | Source of truth | Control | Indicator |
  |---|---|---|---|
  | Global poller | `app_monitor_enabled` (DB, Settings → App) | Settings → App checkbox | `status.running` |
  | Per-account monitoring | `accounts.monitoring_enabled` | `#toggle-monitor-btn` → `PUT /api/accounts/{id}/monitoring` | button label + `#monitor-poller-state` |
  | Browser event stream | `state.streamOpen` (transport only) | none — it just connects | header dot: Live / Reconnecting… / Offline |

  Per-account monitoring is the **master switch for one account**: off means
  the poller does zero OVH work for it (no stock poll, no region ticker, no
  price/promo scan, no sniper fire), enforced by a single gate in
  `_poll_once` plus one in `_maybe_check_prices_and_promos`.
  `set_monitoring_enabled()` drops that account's baselines via
  `_forget_account_state()` so re-enabling **primes silently** — stock moved
  unobserved while it was off, so diffing against the stale baseline would
  report a burst of fake restocks.
- **The stream is a transport, not a setting.** `openStream()` is called
  once by `init()` and never torn down by an account switch (the server
  streams every account, tagged with `account_id`). There is no
  user-facing start/stop for it and no localStorage preference — an earlier
  design had both, and every internal teardown risked leaving a working
  poller looking stopped. `closeStream()` exists but nothing in the normal
  flow calls it. `init()` does NOT request notification permission or
  unlock audio (both need a user gesture; `unlockAudio()` swallows the
  autoplay rejection and is called from the monitoring toggle instead).
- **Rush-form autofill is active-account-only**: `showStockAlert()` takes
  an `accountLabel`, and a tagged (background-account) alert never
  prefills the rush form or offers "Use this config" — the form orders
  under the ACTIVE account's credentials, so prefilling another account's
  plan would place the order in the wrong region.
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
- **Addon labels**: addon cards use OVH's `invoiceName` as the
  primary label (from `addonPrices` map). The `humanizeAddon()`
  functions are only a fallback when no price entry exists.
- **Order line items**: OVH's `/me/order/{id}/details` splits every
  ordered component into a setup row (one-time fee) and a monthly row, so an
  8-row order is really ~4 items. `_group_line_items()` in `orders.py`
  collapses them into one `line_items` entry each (`setup_price`/
  `recurring_price` merged, label cleaned via `_pick_label`); `get_order_detail`
  returns both the raw `details` and grouped `line_items`, and the frontend
  renders `line_items` (falling back to `details`). **The raw detail shape
  differs by region** and the grouping handles both:
  - **ovh-ca/eu**: rows tagged with `detailType` (`INSTALLATION` = setup,
    `DURATION` = monthly) under a hierarchical `domain` (`*001` = server,
    `*001.00x` = options). Grouped by domain, sorted (server first).
  - **ovh-us**: **no `detailType`**, and every row's `domain` is `*`; the
    setup/monthly split is encoded only in the description (`"X"` vs
    `"X - 1 month"`). Grouped by the cleaned product label in OVH's order.
    `_row_kind()` bridges the two (detailType when present, else the
    `- 1 month` suffix); `_group_line_items` groups by domain only when it
    actually distinguishes items, else by label.
- **Order title (server name)**: OVH server orders carry no name on the order
  object, so the list title is derived from the line items. `_name_from_details`
  picks the **server** line — the priciest grouped item (options are
  included/$0), *not* the first detail row (which is often the RAM/storage) —
  falling back to a real `domain` hostname. This is region-agnostic (works for
  both the ca/eu and us detail shapes above). The derived name is persisted; a
  title cached wrong won't self-heal on a plain list load, so the "Refresh all"
  button hits `GET /api/orders?refresh=true`, which re-derives names instead of
  trusting the cache (still `name_budget`-limited so it can't hang).
- **Order follow-up dates are naive and in the ORDER's timezone**: `/me/order/
  {id}/followUp` history dates come back as `"2026-08-04 20:05:02"` — space
  separator, **no offset, not ISO 8601** — while the order object's own `date`
  carries a real one (`"2026-08-04T20:01:19.979215-04:00"`). Verified identical
  on ovh-us and ovh-ca. `_normalize_followup` in `orders.py` attaches the
  order's offset to each naive history date and returns `dt.isoformat()`.
  Rendering them raw was a live bug twice over: the browser parsed them as ITS
  own local time (an hour late on this `America/Chicago` host for a -04:00
  order), and the space-separated form is `Invalid Date` on Safari. It also
  sorts each step's history **ascending** — OVH sends it newest-first while the
  steps themselves run forwards, so raw output read backwards halfway down.
  `label` (the enum, `ORDER_ACCEPTED`) and `description` (the human string) are
  both preserved deliberately; the frontend shows `description`, keeping the
  enum as a hover title. Note `followUp` legitimately **lags** `/status`: an
  order can read `delivered` while the timeline still shows `DELIVERING/DOING`
  because invoicing is running. The UI annotates that rather than faking the
  step states (`followupDisagreementNote` in `app.js`).
- **Catalog config-option order**: OVH returns `plan.addonFamilies` (and the
  addons within each) in arbitrary order. `renderCatalogDetail` standardizes them
  once — families in `CATALOG_FAMILY_ORDER` (memory→storage→bandwidth→vrack),
  addons within each via `compareAddonCodes` (price ascending, then capacity,
  then code — included→small→large). Families are shallow-cloned before sorting
  so `state.plans` isn't mutated; both the option cards and the order-form
  RAM/Storage/Bandwidth dropdowns read the same sorted `families` array.
- **Catalog location badges**: badges/filtering come from each plan's REAL
  deployable locations (`configurations.dedicated_datacenter` →
  `DC_REGION_GROUPS` → `planLocations()` in app.js), NOT the plan-code
  suffix. Verified live (2026-07): ovh-ca's catalog has **zero** `-eu`
  plan codes (unlike ovh-us, which lists 41), yet 38-46 of its 47
  suffixless home plans deploy to European DCs (gra/fra/sbg/waw/rbx/lon)
  — a suffix-derived `[Canada]` badge hid exactly those. The suffix logic
  (`planRegion()`) remains only as the fallback for plans with no DC
  configuration. Unknown DC codes surface uppercased (never mislabelled
  as a known group). The `catalog-location-filter` dropdown is populated
  by `populateLocationFilter()` from the loaded catalog and reset on
  account switch; catalog search also matches DC codes.
- **Frontend**: no framework, no build step for JS. `app.js` is a
  ~4600-line vanilla SPA using a custom `el()` DOM helper. Cache
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
- **Legacy cart API removed** (2026-07): the granular `/api/cart/*`
  router and `POST /api/checkout/{cart_id}` were deleted — the frontend
  never called them and `/api/checkout/rush` is the only order path.
  The `OVHService` cart primitives (create/assign/add/checkout) remain;
  the rush flow uses them directly.
- **Cache busters** are automatic (content-hash based, see
  `app/utils/cache_buster.py`); no manual `?v=N` bumping is needed.
- **Logging**: use `logger.info()`/`logger.warning()` on a
  `logging.getLogger(__name__)` — never `print(file=sys.stderr)`. The system
  has three sinks, all wired by `setup_logging()` in `app/logging_config.py`:
  (1) the console (via `run.py`'s `LOGGING_CONFIG` `"app"` logger, level from
  `OVH_LOG_LEVEL`); (2) a `RotatingFileHandler` → `ecovibe.log`
  (`OVH_LOG_FILE`); (3) a `LogBusHandler` → the in-memory ring buffer in
  `app/services/logbus.py`, read by the webui Logs tab (`GET /api/logs`) and
  live-tailed over SSE (`GET /api/logs/stream`). Key points:
  - **`setup_logging()` is called from BOTH `create_app()` (so capture works in
    tests / before the server starts) and the lifespan startup** — the second
    call re-attaches after uvicorn's own dict-config strips handlers off the
    loggers it configures. It's idempotent (handlers are tagged with
    `_ecovibe_log_handler` and refreshed, not duplicated).
  - **Scope is `app.*` + `uvicorn.error`** — `uvicorn.access` is deliberately
    left out (per-request logs would flood the viewer).
  - **Off-loop safety**: log records are emitted from worker threads too (OVH
    `_call` runs under `asyncio.to_thread`), so `LogBus` fans out to the
    loop-bound SSE queues via `loop.call_soon_threadsafe`. The loop is captured
    in the lifespan startup via `get_log_bus().set_loop(...)`; until then the
    ring buffer still fills but live SSE delivery is a no-op.
  - **The ring buffer is not durable** — it clears on restart; `ecovibe.log` is
    the durable record. Domain events worth an audit line (orders placed, sniper
    auto-orders, stock changes, notifications sent, OVH failures) are logged at
    their source module so the viewer's "source" column names them.
  - Tests reset the bus (`logbus._log_bus = None` + `setup_logging()`) and point
    `OVH_LOG_FILE` at a tmp path in `conftest.py`'s `isolated_state`.
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
- **Availability endpoint**: `/order/eco/availableConfiguration` is EU-only —
  it 404s on ovh-us AND ovh-ca ("Got an invalid (or empty) URL"), which
  silently broke the monitor poller (and therefore the sniper) on those two
  regions. `OVHService.get_availability()` is derived from `get_stock()`
  (`/dedicated/server/datacenter/availabilities`, works on all regions):
  it returns the entries orderable in ≥1 datacenter (availability not in
  `{"unavailable", "comingSoon"}` — same rule as the catalog OOS badge),
  keeping the `fqn` key every caller reads. Verified live on us/ca.
- **Geekbench 6 scores**: CPU-score sorting (`app.js`) and the catalog CPU
  badge read `productSpecs[...].cpu.geekbench6`, a curated single/multi table in
  `app/services/geekbench.py` keyed by normalised `"<brand> <model>"`. It covers
  every CPU in the live US/EU/CA ECO catalogs; a plan whose CPU has no entry gets
  no badge and sorts **last** (`?? 0`). When OVH adds a new CPU,
  `_build_product_specs()` in `catalog.py` logs a **once-per-CPU** warning
  (`"No Geekbench 6 score for CPU ..."`, deduped via `_warned_missing_gb6`) — add
  the chip to the table to fix the sort. Verify coverage against the live catalog
  by fetching it and checking `geekbench.lookup()` for each product's CPU.
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
- **FQNs are looked up, never constructed** (2026-07-31). An availability
  FQN is `{planCode}.{memory}.{storage}[.{system-storage}][.{gpu}]`. Verified
  against all 25,511 live entries: the plan segment **always keeps the plan
  code's region suffix** (`25skb012-eu`, `21adv01-v1-us` — all 666 distinct
  plan segments), and **no FQN ever contains a bandwidth or vrack segment**
  (0 of 25,511). Nor can the segments be derived from catalog addon codes by
  string surgery: plan `24skstor012-v1-us` lists addon `ram-16g-24skstor01-us`
  where the stock API reports `ram-16g-ecc-2133` (the `ecc-2133` appears
  nowhere in the addon code), and the trailing product token varies in length
  (`-ks40`, `-24risegame01-ca`, `-25risel01-v1-ca`) so no fixed slice works.
  `buildFqn()` in `app.js` therefore resolves the FQN out of the live
  `/catalog/stock` feed via `addonCodesMatch()` and falls back to
  `{planCode}.*` when the plan has no stock to copy from.
  It previously *built* the string — stripping the region suffix and
  appending `.bandwidth-N` — which matched **0 of 141** catalog plans, so
  every "Watch This Plan" alert was silently unmatchable
  (`_matches_pattern` is `fnmatch`, and the pattern carried no wildcard) and
  the rush form's `arm_if_oos` check always missed. Symptom: `monitor` logs
  `stock change ...` with no matching `notifier` line, and no notification of
  any kind is sent. If you touch `buildFqn()`, re-verify by resolving a
  pattern for every catalog plan and matching it against live `get_stock()`.
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
  processed the request but the response was lost. The 403
  stale-signature retry path, by contrast, **deliberately retries POST
  too**: a signature rejection happens at the auth layer before OVH
  processes the request, so a replay cannot duplicate an order, and
  excluding POST would leave sniper rush orders broken after clock
  drift. Both behaviours are locked in by tests in
  `tests/test_ovh_service.py`.
- **Settings UI**: separate full-page views (not monitor tabs),
  each a direct child of `#app` toggled by `showView()`:
  `#setup-view` (first-run wizard, shown only when unconfigured — add the
  first account, then land on the monitor), `#accounts-view` (manage OVH
  accounts: list + inline add/edit editor), `#notifications-view`
  (Telegram/Discord/Slack/SMTP), `#billing-view` (Default Checkout
  Preferences — the `checkout-defaults-form`, moved out of the monitor's
  Billing tab, which now shows only OVH account info + payment methods),
  and `#app-view` (App Options — see the app settings bullet below).
  The header "Settings" gear opens `#accounts-view` via
  `showSettings('accounts')`; a sub-nav of `[data-settings-nav]` buttons
  switches between Accounts / Notifications / Billing / App
  (`showSettings(page)` also loads that page's data);
  `.settings-back-btn` returns to the monitor. The
  setup wizard has NO skip — an account is required. Setup uses
  `setup-*` field ids and `saveSetupAccount()` (onboarding: activate +
  go to monitor); the accounts editor uses `acct-*` ids and
  `saveManagedAccount()` (never changes the active account); both share
  `submitAccount()`. Deleting the last account returns to `#setup-view`.
- **App settings (`app_` prefix)**: runtime options on Settings → App are
  stored in the DB `settings` table under `app_<key>` and read DB-first
  with env fallback via `app/services/app_settings.py` (`APP_SETTINGS`
  registry + `app_setting_int/bool/str`). **Always read these keys
  through those helpers, never `get_settings()` directly** — the
  lru-cached Settings object only sees env vars; `cache_clear()` cannot
  pick up DB overrides. Covered keys: price_check_interval,
  order_check_interval,
  stock_event_retention_days/max_rows (read per monitor cycle — live),
  use_cache/cache_ttl (frozen per OVHService — the PUT hook calls
  `reset_ovh_service(None)` + clears the cache; `get_cache(ttl)` now
  updates the TTL live instead of freezing it at first use),
  log_level/log_file_max_bytes/log_backup_count (applied by re-running
  the idempotent `setup_logging()`), log_buffer_size (`LogBus.resize()`
  rebuilds the deque keeping the newest entries), and six `ui_*`
  preferences consumed only by the frontend (`state.uiPrefs`, loaded by
  `loadUiPrefs()`). `PUT /api/settings/app` validates everything before
  writing (all-or-nothing 422) and returns the rebuild hooks it applied.
  `log_file`, host/port, db_path, and CORS stay env-only (restart).
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
