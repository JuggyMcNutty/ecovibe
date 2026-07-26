// ECOVibe - frontend SPA (vanilla JS, no framework)

const API_BASE = '/api';

const OVH_REGIONS = {
    'ovh-eu': {
        name: 'Europe',
        managerUrl: 'https://www.ovh.com/manager/',
        apiEndpoint: 'https://eu.api.ovh.com/v1',
        rushRegion: 'europe'
    },
    'ovh-us': {
        name: 'United States',
        managerUrl: 'https://us.ovhcloud.com/manager/',
        apiEndpoint: 'https://api.us.ovhcloud.com/v1',
        rushRegion: 'united_states'
    },
    'ovh-ca': {
        name: 'Canada',
        managerUrl: 'https://ca.ovh.com/manager/',
        apiEndpoint: 'https://ca.api.ovh.com/v1',
        rushRegion: 'canada'
    }
};

// SSE reconnect backoff: start at 1s, double per failed attempt, cap at
// 30s; reset on a successful open or an explicit stop.
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

const ALERT_SOUND_DATA = 'data:audio/wav;base64,UklGRnoGAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQoGAACBhYqFbF1fdH2Onp6dn5yXl5aXmJmam5ydn56dn5+goaKjpKWlp6eorK2tr7GxsrKys7S0tbW2tra3t7e4uLm5uru7u7y8vL29vr6/v8DAwMHBwsLCwsPDw8TExMXFxcbGxsfHx8jIyMnJysrKy8vLzMzMzc3Ozs/Pz9DQ0NHR0tLS09PT1NTU1dXW1tbX19fY2NjZ2dra29vb3Nzc3d3e3t/f3+Dg4OHh4uLi4+Pj5OTk5eXm5ubn5+fo6Ojp6erq6+vr7Ozs7e3u7u/v7/Dw8PHx8vLy8/Pz9PT09fX29vb39/f4+Pj5+fr6+vv7+/z8/P39/v7///8=';

let state = {
    view: 'loading',
    configured: false,
    endpoint: 'ovh-eu',
    accounts: [],
    activeAccountId: null,
    editingAccountId: null,
    monitoring: false,
    eventSource: null,
    reconnectTimer: null,
    reconnectDelay: 1000,      // RECONNECT_BASE_MS (declared above state)
    logsReconnectDelay: 1000,
    catalog: null,
    plans: [],
    catalogCountry: null,
    catalogAutoRefresh: false,
    catalogRefreshTimer: null,
    selectedPlanCode: null,
    alerts: [],
    profiles: [],
    recentAlerts: [],
    currentStock: {},
    lastStockAlert: null,
    regionTicker: false,
    regionFeed: [],   // [{time, planCode, fqns: []}] newest first
    editingProfileId: null,
    // UI preferences (Settings → App, ui_* keys). Defaults mirror the
    // app_settings registry; refreshed by loadUiPrefs().
    uiPrefs: {
        alertAutohideMs: 30000,
        ordersDays: 90,
        ordersLimit: 50,
        logsLimit: 1000,
        regionFeedCap: 100,
        recentAlertsShown: 5,
    },
    billingLoaded: false,
    checkoutDefaults: null,
    addonPrices: {},
    productSpecs: {},
    stockByPlan: {},
    // Orders tab state.
    allOrders: [],
    selectedOrderId: null,
    ordersFilter: 'all',
    // Logs tab state. Its SSE tail uses separate keys from the monitor
    // stream so the two EventSources never clash.
    logsBuffer: [],
    logsFilter: { level: 'INFO', source: 'all', search: '' },
    logsPaused: false,
    logsEventSource: null,
    logsReconnectTimer: null,
    // Request-generation token for account switches. Incremented on each
    // switch; async callbacks check that their generation is still current
    // before writing to state, so stale responses from a previous account
    // are ignored.
    _switchGen: 0,
    // Display-only currency conversion (visual only; OVH charges in the
    // catalog's native currency regardless). catalogCurrency is the ISO code
    // the active catalog is denominated in; displayCurrency is what the user
    // sees. fxRates is the cached Frankfurter/ECB payload (EUR-base).
    // priceMode: 'ovh' = show OVH's native catalog currency (its exact
    // formattedPrice) — this is the default, so prices always reflect what
    // OVH actually charges. 'fx' = FX-convert to the user's selected display
    // currency. Set by the "Convert pricing" checkbox (always visible). All
    // price rendering goes through effectiveDisplayCurrency().
    catalogCurrency: 'EUR',
    // Authoritative native currency from the backend's top-level
    // `currencyCode` (OVH `locale.currencyCode`). Some endpoints (ovh-ca)
    // leave currencyCode null on pricings, so this is the reliable source.
    catalogCurrencyFromApi: '',
    displayCurrency: 'EUR',
    fxRates: null,
    priceMode: 'ovh',
    _currencyUserSet: false,
    sniperStatus: null,
};

// Currency symbols for the four supported display currencies. Used as a
// fallback when Intl.NumberFormat isn't available; otherwise Intl handles
// symbols/positioning per locale.
const CURRENCY_SYMBOLS = { EUR: '€', USD: '$', GBP: '£', CAD: 'C$' };
const SUPPORTED_CURRENCIES = ['EUR', 'USD', 'GBP', 'CAD'];

// Maps a display currency to the OVH subsidiary whose catalog is
// natively priced in that currency. Used in 'ovh' price mode so the
// user sees OVH's real prices rather than FX-converted estimates.
const CURRENCY_SUBSIDIARY = {
    EUR: 'IE',
    USD: 'US',
    GBP: 'GB',
    CAD: 'CA',
};

// Resolve the catalog subsidiary to fetch from for a given price mode.
// 'ovh' (native): fetch the subsidiary matching the display currency when
// the active endpoint accepts it, so OVH's exact prices are shown.
// 'fx' (converted): always fetch the endpoint's default subsidiary and let
// the frontend FX-convert for display. Each endpoint only accepts its own
// subsidiaries (ovh-ca -> {CA}, ovh-us -> {US}, ovh-eu -> {IE, FR, ...});
// a foreign subsidiary is rejected with HTTP 400 "invalid ovhSubsidiary".
function subsidiaryForMode(mode) {
    const valid = SUBSIDIARIES_BY_ENDPOINT[state.endpoint] || SUBSIDIARIES_BY_ENDPOINT['ovh-eu'];
    if (mode === 'ovh') {
        const mapped = CURRENCY_SUBSIDIARY[state.displayCurrency];
        if (mapped && valid.includes(mapped)) return mapped;
    }
    return valid[0];
}

function formatCurrency(amount, code = effectiveDisplayCurrency()) {
    if (typeof amount !== 'number' || !isFinite(amount)) return 'On request';
    try {
        return new Intl.NumberFormat(undefined, { style: 'currency', currency: code }).format(amount);
    } catch (e) {
        const sym = CURRENCY_SYMBOLS[code] || '';
        return `${sym}${amount.toFixed(2)}`;
    }
}

function loadFxRates() {
    return apiRequest('GET', '/currency/rates').then(rates => {
        state.fxRates = rates;
        updateCurrencyStatus();
    }).catch(() => {
        state.fxRates = null;
        updateCurrencyStatus();
    });
}

function updateCurrencyStatus() {
    const el = document.getElementById('currency-status');
    if (!el) return;
    // 'ovh' (native) mode always shows the catalog's native currency, even
    // when the selector differs — surface that so the user knows why prices
    // aren't in their selected currency (and how to convert).
    if (state.priceMode === 'ovh') {
        el.textContent = (state.displayCurrency === state.catalogCurrency)
            ? ''
            : `(native ${state.catalogCurrency})`;
        return;
    }
    // 'fx' mode: converting to the selected display currency.
    if (state.displayCurrency === state.catalogCurrency) {
        el.textContent = '';
        return;
    }
    if (!state.fxRates) {
        el.textContent = '(rates unavailable — showing native prices)';
    } else {
        el.textContent = `· FX ${state.fxRates.date || ''}`;
    }
}

// The currency prices are actually rendered in. 'ovh' mode always shows
// OVH's native catalog currency (OVH's exact formattedPrice); 'fx' mode
// converts to the user's selected display currency. All price rendering
// goes through this so the "Convert pricing" toggle has one effect.
function effectiveDisplayCurrency() {
    return state.priceMode === 'ovh' ? state.catalogCurrency : state.displayCurrency;
}

// Keep the "Convert pricing" checkbox in sync with priceMode. The checkbox
// is always visible — FX conversion is a user option; it's unchecked by
// default (OVH native pricing shown) and checking it converts to the
// selected display currency.
function updatePriceModeVisibility() {
    const cb = document.getElementById('price-mode-ovh');
    if (cb) cb.checked = (state.priceMode === 'fx');
}

function convertMicrocents(microcents, fromCode = state.catalogCurrency, toCode = effectiveDisplayCurrency()) {
    // microcents (1 unit = 10^8 microcents) → currency units → FX → display units
    const units = microcents / 100000000;
    if (fromCode === toCode || !state.fxRates) return units; // no conversion
    const rateMap = state.fxRates.rates || {};
    const base = state.fxRates.base || 'EUR';
    const fromRate = fromCode === base ? 1 : rateMap[fromCode];
    const toRate = toCode === base ? 1 : rateMap[toCode];
    if (!fromRate || !toRate) return units; // unknown: don't convert
    return units * (toRate / fromRate);
}

function displayPrice(microcents, formattedPrice, fromCode = state.catalogCurrency) {
    // Render in the effective display currency (native in 'ovh' mode, the
    // selected currency in 'fx' mode). Prefer OVH's exact formattedPrice
    // when it already matches the effective currency.
    const eff = effectiveDisplayCurrency();
    if (eff === fromCode && formattedPrice) return formattedPrice;
    if (microcents == null) return 'On request';
    return formatCurrency(convertMicrocents(microcents, fromCode, eff), eff);
}

let audioContext = null;
let alertBuffer = null;
let alertPanelTimer = null;

// DOM helpers

function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
        if (key === 'class') {
            node.className = value;
        } else if (key === 'text') {
            node.textContent = value;
        } else if (key.startsWith('data-')) {
            node.dataset[key.slice(5)] = value;
        } else if (key.startsWith('on') && typeof value === 'function') {
            node[key.toLowerCase()] = value;
        } else if (key === 'checked' || key === 'selected' || key === 'disabled') {
            if (value) node.setAttribute(key, key);
            // Don't set the attribute if false - presence/absence is what matters
        } else if (key === 'value' && (tag === 'input' || tag === 'option' || tag === 'select' || tag === 'textarea')) {
            node.value = value;
        } else if (typeof value === 'string' || typeof value === 'number') {
            node.setAttribute(key, value);
        }
    }
    for (const child of [].concat(children)) {
        if (child == null) continue;
        node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return node;
}

function showView(viewName) {
    document.querySelectorAll('#app > div[id$="-view"]').forEach(div => {
        div.classList.add('hidden');
    });
    const view = document.getElementById(`${viewName}-view`);
    if (view) {
        view.classList.remove('hidden');
    }
    state.view = viewName;
}

// Switch between the two settings pages (Accounts / Notifications) and
// highlight the active sub-nav button.
function showSettings(page) {
    const target = ['accounts', 'notifications', 'billing', 'app'].includes(page) ? page : 'accounts';
    document.querySelectorAll('[data-settings-nav]').forEach(btn => {
        const active = btn.dataset.settingsNav === target;
        btn.classList.toggle('bg-blue-600', active);
        btn.classList.toggle('text-white', active);
        btn.classList.toggle('bg-gray-700', !active);
        btn.classList.toggle('text-gray-300', !active);
    });
    showView(target);
    if (target === 'notifications') loadNotificationSettings();
    else if (target === 'billing') loadCheckoutDefaults();
    else if (target === 'app') loadAppSettings();
    else loadAccountsPage();
}

async function loadAccountsPage() {
    closeAccountEditor();
    await loadAccounts();
    renderAccountList();
}

function showLoading() {
    document.getElementById('loading-overlay').classList.remove('hidden');
}

function hideLoading() {
    document.getElementById('loading-overlay').classList.add('hidden');
}

function showError(message) {
    const errorView = document.getElementById('error-view');
    document.getElementById('error-message').textContent = message;
    errorView.classList.remove('hidden');
}

function hideError() {
    document.getElementById('error-view').classList.add('hidden');
}

let toastTimer = null;
function showToast(message, duration = 2500) {
    const toast = document.getElementById('toast-view');
    document.getElementById('toast-message').textContent = message;
    toast.classList.remove('hidden');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        toast.classList.add('hidden');
        toastTimer = null;
    }, duration);
}

function updateConnectionStatus(connected) {
    const dot = document.getElementById('connection-dot');
    const text = document.getElementById('connection-text');
    if (connected) {
        dot.className = 'w-3 h-3 rounded-full bg-green-500';
        text.textContent = 'Connected';
    } else {
        dot.className = 'w-3 h-3 rounded-full bg-gray-500';
        text.textContent = 'Disconnected';
    }
}

// API client

async function apiRequest(method, path, body = null) {
    const options = {
        method,
        headers: {
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        }
    };
    if (body !== null) {
        options.body = JSON.stringify(body);
    }
    const response = await fetch(`${API_BASE}${path}`, options);
    if (!response.ok) {
        let errorDetail = 'API request failed';
        try {
            const error = await response.json();
            errorDetail = error.detail || error.message || errorDetail;
        } catch (e) {
            errorDetail = response.statusText || errorDetail;
        }
        throw new Error(errorDetail);
    }
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
        return null;
    }
    try {
        return await response.json();
    } catch (e) {
        return null;
    }
}

async function checkHealth() {
    try {
        const health = await apiRequest('GET', '/health');
        state.configured = health.configured;
        state.endpoint = health.endpoint || 'ovh-eu';
        state.activeAccountId = health.active_account_id || null;
        return health.configured;
    } catch (e) {
        return false;
    }
}

// ----- multi-account -----

async function loadAccounts() {
    try {
        state.accounts = await apiRequest('GET', '/accounts') || [];
        renderAccountSelect();
    } catch (e) {
        console.error('Failed to load accounts:', e);
    }
}

function renderAccountSelect() {
    const sel = document.getElementById('account-select');
    if (!sel) return;
    if (!state.accounts.length) {
        sel.classList.add('hidden');
        return;
    }
    sel.classList.remove('hidden');
    sel.innerHTML = '';
    state.accounts.forEach(a => {
        const opt = el('option', { value: a.id, text: `${a.label} (${a.endpoint})` });
        if (a.id === state.activeAccountId) opt.selected = true;
        sel.appendChild(opt);
    });
}

async function switchAccount(accountId) {
    if (accountId === state.activeAccountId) return;
    // Tear down all background activity from the previous account so it
    // doesn't race with the new account's data load or leak stale state.
    if (state.monitoring) stopMonitoring();
    stopCatalogAutoRefresh();
    // Reset stale account-scoped state so the new account starts clean.
    state.currentStock = {};
    state.recentAlerts = [];
    state.lastStockAlert = null;
    state.regionFeed = [];
    state.editingProfileId = null;
    state.selectedPlanCode = null;
    state.stockByPlan = {};
    state.allOrders = [];
    state.selectedOrderId = null;
    state.plans = [];
    state.addonPrices = {};
    state.productSpecs = {};
    state.catalogCountry = null;
    // The new endpoint's catalog offers a different set of location groups;
    // reset the filter so plans aren't hidden by a stale selection.
    const locFilter = document.getElementById('catalog-location-filter');
    if (locFilter) locFilter.value = '';
    // Clear detail panels so stale content doesn't linger.
    const catDetail = document.getElementById('catalog-detail');
    if (catDetail) catDetail.innerHTML = '<p class="text-gray-500 text-sm">Select a plan to see details.</p>';
    const ordDetail = document.getElementById('order-detail');
    if (ordDetail) ordDetail.innerHTML = '<p class="text-gray-500 text-sm">Select an order to see details.</p>';
    // Repaint the recent-alerts list from the (now-empty) state and dismiss any
    // live stock-alert banner — these are only otherwise refreshed by incoming
    // SSE events, which stop the moment we tore down monitoring above, so
    // without this they'd keep showing the previous account's restocks.
    renderRecentAlerts();
    document.getElementById('stock-alerts-panel')?.classList.add('hidden');

    // Increment the generation token so in-flight callbacks from the
    // previous account know they're stale and bail out.
    const gen = ++state._switchGen;
    try {
        await apiRequest('PUT', '/accounts/active', { account_id: accountId });
        if (gen !== state._switchGen) return;  // superseded by a newer switch
        state.activeAccountId = accountId;
        const acct = state.accounts.find(a => a.id === accountId);
        if (acct) state.endpoint = acct.endpoint;
        // Reload all scoped data for the new account.
        state._currencyUserSet = false;  // allow /me to re-default the currency
        await loadFxRates();
        if (gen !== state._switchGen) return;
        await loadAccountInfo();
        if (gen !== state._switchGen) return;
        await loadAlerts();
        if (gen !== state._switchGen) return;
        await loadCatalog();
        if (gen !== state._switchGen) return;
        await loadProfiles();
        if (gen !== state._switchGen) return;
        await loadOrders();
        if (gen !== state._switchGen) return;
        // The full Orders tab is otherwise only (re)loaded lazily on tab
        // switch, so if it's the active tab during an account switch it would
        // keep showing the previous account's orders. Reload it in place.
        const ordersTabVisible = !document.getElementById('orders-tab')?.classList.contains('hidden');
        if (ordersTabVisible) {
            await loadOrdersTab();
            if (gen !== state._switchGen) return;
        }
        // Same in-place reload for the lazy-loaded Servers tab.
        const serversTabVisible = !document.getElementById('servers-tab')?.classList.contains('hidden');
        if (serversTabVisible) {
            await loadServersTab();
            if (gen !== state._switchGen) return;
            const detail = document.getElementById('server-detail');
            if (detail) detail.innerHTML = '<p class="text-gray-500 text-sm">Select a server to see details.</p>';
        }
        // The Insights tab is likewise only (re)loaded lazily on tab switch, so
        // if it's active during an account switch it keeps rendering the
        // previous account's overview/charts/dropdown. Reload it in place.
        const insightsTabVisible = !document.getElementById('insights-tab')?.classList.contains('hidden');
        if (insightsTabVisible) {
            populateInsightsPlanSelect();
            await loadInsightsData();
            if (gen !== state._switchGen) return;
        }
        await loadSniperStatus();
        if (gen !== state._switchGen) return;
        // The ticker setting is global, but the feed's events are
        // account-scoped — refill it for the new account (self-guarded).
        loadRegionWatch();
        if (state.billingLoaded) {
            loadPaymentMethods();
            loadCheckoutDefaults();
        }
    } catch (e) {
        if (gen === state._switchGen) {
            showError(`Failed to switch account: ${e.message}`);
        }
    }
}

function renderAccountList() {
    const container = document.getElementById('accounts-list');
    if (!container) return;
    container.innerHTML = '';
    if (!state.accounts.length) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No accounts yet. Click "+ Add account" to create one.' }));
        return;
    }
    state.accounts.forEach(a => {
        const isActive = a.id === state.activeAccountId;
        const card = el('div', {
            class: `flex justify-between items-center rounded p-3 ${isActive ? 'bg-blue-900/40 border border-blue-700' : 'bg-gray-700'}`,
        }, [
            el('div', {}, [
                el('div', { class: 'font-bold', text: a.label }),
                el('div', { class: 'text-xs text-gray-400', text: `${a.endpoint} · ${a.application_key_masked || '****'}` }),
            ]),
            el('div', { class: 'flex gap-2' }, [
                isActive
                    ? el('span', { class: 'text-xs text-blue-400 font-bold', text: 'ACTIVE' })
                    : el('button', {
                          class: 'bg-blue-600 hover:bg-blue-700 px-3 py-1 rounded text-sm',
                          text: 'Switch',
                          onclick: () => switchAccount(a.id),
                      }),
                el('button', {
                    class: 'bg-gray-600 hover:bg-gray-500 px-3 py-1 rounded text-sm',
                    text: 'Edit',
                    onclick: () => openAccountEditor(a.id),
                }),
            ]),
        ]);
        container.appendChild(card);
    });
}

// Open the add/edit account editor. id=null => add mode; otherwise edit.
function openAccountEditor(accountId) {
    state.editingAccountId = accountId || null;
    const editor = document.getElementById('acct-editor');
    const title = document.getElementById('acct-editor-title');
    const del = document.getElementById('acct-delete-btn');
    document.getElementById('acct-test-result').classList.add('hidden');
    document.getElementById('acct-app-key').value = '';
    document.getElementById('acct-app-secret').value = '';
    document.getElementById('acct-consumer-key').value = '';
    if (accountId) {
        const a = state.accounts.find(x => x.id === accountId);
        if (!a) return;
        title.textContent = `Edit "${a.label}"`;
        document.getElementById('acct-label').value = a.label;
        document.getElementById('acct-region').value = a.endpoint;
        document.getElementById('acct-app-secret').placeholder = 'leave blank to keep the stored secret';
        del.classList.remove('hidden');
        updateManagerLink(a.endpoint, document.getElementById('acct-manager-link'));
    } else {
        title.textContent = 'Add account';
        document.getElementById('acct-label').value = '';
        document.getElementById('acct-region').value = 'ovh-eu';
        del.classList.add('hidden');
        updateManagerLink('ovh-eu', document.getElementById('acct-manager-link'));
    }
    editor.classList.remove('hidden');
    editor.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function closeAccountEditor() {
    state.editingAccountId = null;
    document.getElementById('acct-editor')?.classList.add('hidden');
}

// Catalog

const SUBSIDIARIES_BY_ENDPOINT = {
    'ovh-eu': ['IE', 'FR', 'DE', 'GB', 'ES', 'PL', 'IT', 'PT', 'CZ', 'FI'],
    'ovh-us': ['US'],
    'ovh-ca': ['CA'],
};

function catalogSubsidiaryForCurrency() {
    // Delegate to subsidiaryForMode() so the fetch and the display mode
    // agree on which subsidiary's catalog is the source of truth.
    return subsidiaryForMode(state.priceMode);
}

async function loadCatalog(country, force = false) {
    showLoading();
    // Snapshot the generation token: a slow response from a previous
    // account must not clobber state after the user switched away (the
    // same guard refreshCatalogSilent/refreshStockForAllPlans use).
    const gen = state._switchGen;
    // Keep the "Convert pricing" checkbox in sync with the current
    // endpoint/currency (covers paths that bypass loadAccountInfo, e.g.
    // when /account/me fails). Idempotent.
    updatePriceModeVisibility();
    const subsidiary = country || catalogSubsidiaryForCurrency();
    state.catalogCountry = subsidiary;
    try {
        const params = new URLSearchParams();
        if (subsidiary) params.set('country', subsidiary);
        if (force) params.set('force_refresh', 'true');
        const qs = params.toString();
        const url = qs ? `/catalog/plans?${qs}` : '/catalog/plans';
        const resp = await apiRequest('GET', url);
        if (gen !== state._switchGen) return;  // superseded by account switch
        state.plans = resp.plans || [];
        state.addonPrices = resp.addonPrices || {};
        state.productSpecs = resp.productSpecs || {};
        // The backend surfaces the catalog's native currency as an
        // authoritative top-level field (sourced from OVH's `locale`).
        // Some endpoints (ovh-ca) omit currencyCode on pricing entries, so
        // this is the reliable source for FX-aware price display.
        state.catalogCurrencyFromApi = resp.currencyCode || '';
        // Detect the catalog's native currency from the first addon price
        // that carries a currencyCode (or from a plan's pricing).
        detectCatalogCurrency();
        // Re-render prices if the display currency differs from native.
        renderPlanSelect();
        populateLocationFilter();
        renderCatalogList();
        if (state.selectedPlanCode) {
            const p = state.plans.find(x => x.planCode === state.selectedPlanCode);
            if (p) renderCatalogDetail(p);
        }
        // Fetch stock levels for all plans and re-render with badges.
        // Awaited so the loading overlay stays visible until stock data
        // is ready — otherwise the list shows without OOS badges briefly.
        await refreshStockForAllPlans();
        if (gen !== state._switchGen) return;
        renderCatalogList();
        // Plans (and their configurations) just arrived — refresh the rush
        // form's DC/OS options for whatever plan code it currently holds.
        updateRushConfigOptionsForPlanCode(
            document.getElementById('rush-plan-code')?.value.trim() || ''
        );
    } catch (e) {
        if (gen === state._switchGen) showError(e.message);
    } finally {
        hideLoading();
    }
}

function detectCatalogCurrency() {
    // Detect the catalog's native currency from the response data. We can no
    // longer assume it matches the display currency: when the display
    // currency's subsidiary isn't valid for the active endpoint (e.g. USD on
    // ovh-ca), we fall back to the endpoint's default subsidiary, so the
    // catalog may be denominated in a different currency (e.g. CAD) and need
    // FX conversion for display.
    //
    // The backend surfaces the catalog's native currency authoritatively via
    // the top-level `currencyCode` field (sourced from OVH's `locale`); this
    // is the reliable source because some endpoints (ovh-ca) leave
    // currencyCode null on individual pricings. Fall back to addon/plan
    // pricing entries for older responses that lack the field.
    if (state.catalogCurrencyFromApi) {
        state.catalogCurrency = state.catalogCurrencyFromApi;
        updateMaxPriceLabel();
        updateCurrencyStatus();
        return;
    }
    for (const code in state.addonPrices) {
        const cc = state.addonPrices[code]?.currencyCode;
        if (cc) { state.catalogCurrency = cc; updateMaxPriceLabel(); updateCurrencyStatus(); return; }
    }
    for (const plan of state.plans) {
        const mp = getPlanMonthlyPrice(plan);
        if (mp?.currencyCode) {
            state.catalogCurrency = mp.currencyCode;
            updateMaxPriceLabel();
            updateCurrencyStatus();
            return;
        }
    }
    updateCurrencyStatus();
}

function updateMaxPriceLabel() {
    const el = document.getElementById('max-price-currency');
    if (el) el.textContent = `(${state.catalogCurrency})`;
}

async function refreshCatalogSilent() {
    if (!state.configured || !state.catalogCountry) return;
    const gen = state._switchGen;
    try {
        const url = `/catalog/plans?country=${encodeURIComponent(state.catalogCountry)}`;
        const resp = await apiRequest('GET', url);
        // An account switch happened while this request was in flight -
        // discard the stale response instead of clobbering the new
        // account's freshly-loaded catalog state.
        if (gen !== state._switchGen) return;
        const oldCount = state.plans.length;
        state.plans = resp.plans || [];
        state.addonPrices = resp.addonPrices || {};
        state.productSpecs = resp.productSpecs || {};
        state.catalogCurrencyFromApi = resp.currencyCode || '';
        detectCatalogCurrency();
        // Carry over stock flags until the fresh fetch completes.
        for (const p of state.plans) {
            p._inStock = state.stockByPlan[p.planCode] ?? true;
        }
        renderPlanSelect();
        populateLocationFilter();
        renderCatalogList();
        if (state.selectedPlanCode) {
            const p = state.plans.find(x => x.planCode === state.selectedPlanCode);
            if (p) renderCatalogDetail(p);
        }
        if (state.plans.length !== oldCount) {
            updateCatalogRefreshBadge(`${state.plans.length} plans`, true);
        } else {
            updateCatalogRefreshBadge(`${state.plans.length} plans`, false);
        }
        refreshStockForAllPlans().then(() => {
            if (gen === state._switchGen) renderCatalogList();
        }).catch(() => {});
    } catch (e) {
        // Silent fail - don't disrupt the user with error banners on background polls
        console.error('Catalog auto-refresh failed:', e);
    }
}

function startCatalogAutoRefresh(intervalSec) {
    stopCatalogAutoRefresh();
    state.catalogAutoRefresh = true;
    state.catalogRefreshTimer = setInterval(refreshCatalogSilent, intervalSec * 1000);
    updateCatalogRefreshBadge(`${state.plans.length} plans`, false);
}

function stopCatalogAutoRefresh() {
    state.catalogAutoRefresh = false;
    if (state.catalogRefreshTimer) {
        clearInterval(state.catalogRefreshTimer);
        state.catalogRefreshTimer = null;
    }
    updateCatalogRefreshBadge(null, false);
}

// Fetch stock for all plans in the catalog and store a boolean
// (true = the default/included config is in stock somewhere) on each
// plan as `_inStock`. Only checks the included (free) memory+storage
// combo — the ones that ship with the server at no extra cost — so
// users see whether the base server is orderable, not whether any
// paid upgrade happens to be in stock.
// Called after catalog load + on auto-refresh so the list can show
// "Out of stock" badges. Requests are batched with limited concurrency.
async function refreshStockForAllPlans() {
    if (!state.plans.length) return;
    // Snapshot the account-switch generation. Per-plan stock is fetched from
    // the active account's endpoint; if the user switches accounts while these
    // requests are in flight, the results belong to the old account and must
    // not clobber the new account's freshly-loaded state below.
    const gen = state._switchGen;
    const planCodes = state.plans.map(p => p.planCode).filter(Boolean);
    const stockByPlan = {};
    const CONCURRENCY = 5;
    let i = 0;
    async function fetchOne() {
        while (i < planCodes.length) {
            const idx = i++;
            const pc = planCodes[idx];
            const plan = state.plans.find(p => p.planCode === pc);
            try {
                const data = await apiRequest('GET', `/catalog/stock?plan_code=${encodeURIComponent(pc)}`);
                // Find the default (included) memory and storage addons
                const families = plan?.addonFamilies || [];
                const defaultMem = families.find(f => f.name === 'memory')?.default || '';
                const defaultStor = families.find(f => f.name === 'storage')?.default || '';
                const memShort = addonShortCode(defaultMem);
                const storShort = addonShortCode(defaultStor);
                // Only consider the default combo as "in stock"
                let matched = false;
                let hasAvailable = false;
                for (const entry of (data || [])) {
                    if (memShort && !addonCodesMatch(memShort, entry.memory)) continue;
                    if (storShort && !addonCodesMatch(storShort, entry.storage)) continue;
                    matched = true;
                    if ((entry.datacenters || []).some(dc => dc.availability !== 'unavailable' && dc.availability !== 'comingSoon')) {
                        hasAvailable = true;
                        break;
                    }
                }
                // Warn if no stock entry matched the default combo — likely a
                // code-naming mismatch rather than genuine OOS.
                if (!matched && (memShort || storShort) && (data || []).length) {
                    console.warn(`Stock matching failed for ${pc}: default mem=${memShort} stor=${storShort} did not match any stock entry`);
                }
                // Only record a definitive result when we actually matched the
                // default combo against a stock entry. A match failure (naming
                // mismatch, warned above) or an empty payload is *unknown*
                // stock, not confirmed OOS — leave it unset so the `?? true`
                // fallbacks below keep the plan visible and unbadged rather
                // than hiding an orderable server behind the "Orderable only"
                // filter (a false OOS is worse than an unverified in-stock).
                if (matched) {
                    stockByPlan[pc] = hasAvailable;
                }
            } catch (e) {
                console.warn(`Stock fetch failed for ${pc}, assuming in-stock:`, e);
                stockByPlan[pc] = true;
            }
        }
    }
    await Promise.all(Array.from({ length: CONCURRENCY }, fetchOne));
    // An account switch landed while we were fetching — discard these stale
    // results rather than writing the previous account's stock onto the new
    // account's plans.
    if (gen !== state._switchGen) return;
    state.stockByPlan = stockByPlan;
    state.plans.forEach(p => { p._inStock = stockByPlan[p.planCode] ?? true; });
}

function updateCatalogRefreshBadge(text, changed) {
    const badge = document.getElementById('catalog-refresh-badge');
    if (!badge) return;
    if (!text) {
        badge.textContent = '';
        badge.className = 'text-xs text-gray-500';
        return;
    }
    badge.textContent = text + (state.catalogAutoRefresh ? ' (auto)' : '');
    badge.className = changed
        ? 'text-xs text-yellow-400 font-bold'
        : 'text-xs text-gray-500';
}

// Extract the monthly renewal price (as a raw integer) from a catalog plan.
// OVH stores prices in `plan.pricings[]` where each entry has:
//   mode: 'default' | 'upfront12' | 'upfront24' (we want 'default' = monthly)
//   interval: 0 (setup) | 1 (monthly) | 12 | 24 (we want 1)
//   intervalUnit: 'month' | 'none'
//   capacities: ['installation'] | ['renew'] (we want 'renew')
//   price: integer in microcents (divide by 10^8 to get currency units)
//   formattedPrice: "$90.00 USD" (pre-formatted by OVH - use for display)
function getPlanMonthlyPrice(plan) {
    const pricings = plan.pricings || [];
    const monthly = pricings.find(p => p.mode === 'default' && p.interval === 1 && p.intervalUnit === 'month');
    if (monthly) return monthly;
    // Fallback: any default-mode pricing with a renew capacity
    return pricings.find(p => p.mode === 'default' && (p.capacities || []).includes('renew'));
}

// One-time setup/installation fee (interval=0, intervalUnit='none').
// OVH charges this at checkout on top of the first month's price.
function getPlanSetupFee(plan) {
    const pricings = plan.pricings || [];
    const setup = pricings.find(p => p.mode === 'default' && p.interval === 0 && p.intervalUnit === 'none');
    if (setup) return setup;
    // Fallback: any default-mode pricing with an installation capacity
    return pricings.find(p => p.mode === 'default' && (p.capacities || []).includes('installation'));
}

function formatPrice(priceValue) {
    if (typeof priceValue !== 'number' || !isFinite(priceValue)) {
        return 'On request';
    }
    if (priceValue === 0) {
        return formatCurrency(0);
    }
    // OVH stores prices in microcents: divide by 10^8 to get currency units.
    return formatCurrency(priceValue / 100000000);
}

// Region suffixes on planCode (e.g. "24sk10-eu") map to readable labels.
// This is the single source of truth for "is this planCode segment a region?"
// — version/generation segments like "v1"/"v3" are NOT regions and must not be
// matched here, otherwise they'd be mislabelled as a region badge.
const REGION_LABELS = {
    'eu': 'Europe',
    'us': 'US',
    'ca': 'Canada',
    'sgp': 'Singapore',
    'syd': 'Sydney',
    'lon': 'London',
    'mum': 'Mumbai',
    'fr': 'France',
    'de': 'Germany',
    'gb': 'UK',
    'es': 'Spain',
    'pl': 'Poland',
    'it': 'Italy',
    'pt': 'Portugal',
    'cz': 'Czechia',
    'fi': 'Finland',
    'ie': 'Ireland',
};

// Endpoint -> its "home" region label. OVH is inconsistent about region
// suffixes: ovh-us tags every plan (-us/-eu/-sgp/...), but ovh-ca leaves its
// Canadian plans suffixless (bare "24sk102", or version-only "24rise01-v1")
// and only tags the APAC variants (-sgp/-syd/-mum). A plan with no explicit
// region suffix is the home-region offering for the active endpoint, so it
// gets the endpoint's home-region badge rather than no badge at all.
const ENDPOINT_HOME_REGION = {
    'ovh-eu': 'Europe',
    'ovh-us': 'US',
    'ovh-ca': 'Canada',
};

// Datacenter code → coarse location group. Badges/filtering use the plan's
// REAL deployable locations (configurations.dedicated_datacenter), not the
// plan-code suffix: ovh-ca has no "-eu" plan codes at all, yet most of its
// suffixless home plans deploy to European DCs (verified live 2026-07 —
// gra/fra/sbg/waw/rbx/lon appear in 38-46 of its 47 home plans), so a
// suffix-derived [Canada] badge hides exactly the servers a user scanning
// for Europe is looking for.
const DC_REGION_GROUPS = {
    gra: 'EU', sbg: 'EU', rbx: 'EU', fra: 'EU', waw: 'EU', lon: 'EU', eri: 'EU',
    'eu-west-par-a': 'EU', 'eu-west-par-b': 'EU', 'eu-west-par-c': 'EU',
    bhs: 'CA',
    vin: 'US', hil: 'US',
    sgp: 'APAC', syd: 'APAC', mum: 'APAC', ynm: 'APAC',
};
const LOCATION_GROUP_ORDER = ['EU', 'CA', 'US', 'APAC'];

// Ordered unique location groups a plan can actually deploy to, from its
// dedicated_datacenter configuration. Unknown DC codes surface uppercased
// (never silently mislabelled — same principle as planRegion's unknown-
// suffix rule). Falls back to the suffix-derived planRegion() when the
// plan carries no DC configuration.
function planLocations(plan) {
    const dcs = (plan?.configurations || [])
        .find(c => c.name === 'dedicated_datacenter')?.values || [];
    if (!dcs.length) {
        const region = planRegion(plan?.planCode, state.endpoint);
        return region ? [region] : [];
    }
    const groups = new Set(dcs.map(dc => DC_REGION_GROUPS[dc.toLowerCase()] || dc.toUpperCase()));
    return [
        ...LOCATION_GROUP_ORDER.filter(g => groups.has(g)),
        ...[...groups].filter(g => !LOCATION_GROUP_ORDER.includes(g)).sort(),
    ];
}

// The raw DC codes a plan deploys to (for tooltips + search matching).
function planDatacenters(plan) {
    return (plan?.configurations || [])
        .find(c => c.name === 'dedicated_datacenter')?.values || [];
}

function planRegion(planCode, endpoint) {
    // planCode looks like "24sk102-ca" → extract "ca" → "Canada".
    // PlanCodes may also carry a version/generation segment (e.g. "26sk10b-v1"
    // or "24rise04-v1-mum") where "v1" is NOT a region. When the trailing
    // segment is a known region, use it; otherwise the plan carries no explicit
    // region and belongs to the endpoint's home region — fall back to that
    // (never mislabel a version like "v1" as a region).
    const parts = (planCode || '').split('-');
    const suffix = parts.length > 1 ? parts[parts.length - 1].toLowerCase() : '';
    if (suffix in REGION_LABELS) return REGION_LABELS[suffix];
    // An unrecognised but region-code-shaped suffix (2–4 letters, e.g. a new
    // OVH datacenter like "waw"/"bhs" not yet mapped in REGION_LABELS) is a
    // *foreign* region — surface it uppercased rather than silently
    // mislabelling it as the endpoint's home region. Versions like "v1"/"v3"
    // contain a digit so they never match here.
    if (/^[a-z]{2,4}$/.test(suffix)) return suffix.toUpperCase();
    // No suffix (bare home plan) or a version/generation segment: OVH leaves
    // the home-region offering un-suffixed, so it belongs to the home region.
    return ENDPOINT_HOME_REGION[endpoint] || '';
}

function planLabel(plan) {
    const name = plan.invoiceName || plan.planCode;
    const locations = planLocations(plan);
    return locations.length ? `${name} [${locations.join('·')}]` : name;
}

function renderPlanSelect() {
    const select = document.getElementById('plan-select');
    select.innerHTML = '';
    select.appendChild(el('option', { value: '', text: 'Select a plan...' }));
    // List every plan in the catalog — all of them are orderable on the active
    // endpoint's account (OVH scopes the catalog to the account's subsidiary).
    // Do NOT filter by home region here: that would make foreign-datacenter
    // plans (e.g. Singapore/Sydney on a Canada/WORLD account) un-alertable,
    // which are exactly the flash-sale targets users watch for.
    state.plans.forEach(plan => {
        const opt = el('option', { value: plan.planCode, text: planLabel(plan) });
        select.appendChild(opt);
    });
}

// Fill the location filter with the groups actually present in the loaded
// catalog (ovh-ca offers EU/CA/APAC, ovh-us adds US, ...), preserving the
// current selection when it still exists.
function populateLocationFilter() {
    const select = document.getElementById('catalog-location-filter');
    if (!select) return;
    const current = select.value;
    const groups = new Set();
    (state.plans || []).forEach(p => planLocations(p).forEach(g => groups.add(g)));
    const ordered = [
        ...LOCATION_GROUP_ORDER.filter(g => groups.has(g)),
        ...[...groups].filter(g => !LOCATION_GROUP_ORDER.includes(g)).sort(),
    ];
    select.innerHTML = '';
    select.appendChild(el('option', { value: '', text: 'All locations' }));
    ordered.forEach(g => select.appendChild(el('option', { value: g, text: g })));
    if (ordered.includes(current)) select.value = current;
}

function getFilteredPlans() {
    const q = (document.getElementById('catalog-search')?.value || '').trim().toLowerCase();
    const sort = document.getElementById('catalog-sort')?.value || 'default';
    const orderableOnly = document.getElementById('catalog-orderable-filter')?.checked;
    const stockFirst = document.getElementById('catalog-stock-first')?.checked;
    const location = document.getElementById('catalog-location-filter')?.value || '';
    let plans = state.plans.slice();
    if (orderableOnly) {
        // Show only servers that are actually orderable right now — i.e. their
        // default config has live availability in at least one datacenter.
        // `_inStock` is set from the datacenter availabilities API; it is only
        // false when the plan is confirmed out of stock (unknown/unfetched
        // stock stays visible so nothing is hidden before stock loads).
        plans = plans.filter(p => p._inStock !== false);
    }
    if (location) {
        // Filter by REAL deployable location (dedicated_datacenter groups),
        // not the plan-code suffix — see planLocations().
        plans = plans.filter(p => planLocations(p).includes(location));
    }
    if (q) {
        plans = plans.filter(p =>
            (p.invoiceName || '').toLowerCase().includes(q) ||
            (p.planCode || '').toLowerCase().includes(q) ||
            // Match deployable DC codes ("gra") and location groups ("eu").
            planDatacenters(p).some(dc => dc.toLowerCase().includes(q)) ||
            planLocations(p).some(loc => loc.toLowerCase() === q)
        );
    }
    const priceOf = (p) => {
        const mp = getPlanMonthlyPrice(p);
        return mp?.price ?? Infinity;
    };
    if (sort === 'price-asc') plans.sort((a, b) => priceOf(a) - priceOf(b));
    else if (sort === 'price-desc') plans.sort((a, b) => priceOf(b) - priceOf(a));
    else if (sort === 'name') plans.sort((a, b) => (a.invoiceName || '').localeCompare(b.invoiceName || ''));
    else if (sort === 'score-desc' || sort === 'score-asc') {
        const scoreOf = (p) => {
            const ps = state.productSpecs[p.product] || {};
            // Sort by Geekbench 6 multi-core; plans without a GB6 score (CPU
            // not in our table) fall to the bottom, as before.
            return ps.cpu?.geekbench6?.multi ?? 0;
        };
        plans.sort((a, b) => sort === 'score-desc' ? scoreOf(b) - scoreOf(a) : scoreOf(a) - scoreOf(b));
    }
    // Stock-first: push in-stock plans to the top as a stable primary sort.
    // Applied after the secondary sort so in-stock plans are grouped together
    // and sorted by the user's chosen criteria within each group.
    if (stockFirst) {
        const stockRank = (p) => p._inStock === false ? 1 : 0;
        plans.sort((a, b) => stockRank(a) - stockRank(b));
    }
    return plans;
}

function renderCatalogList() {
    const container = document.getElementById('catalog-plans');
    container.innerHTML = '';
    const filtered = getFilteredPlans();
    const plans = filtered.slice(0, 100);
    if (plans.length === 0) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No plans match your search.' }));
        return;
    }
    plans.forEach(plan => {
        const monthly = getPlanMonthlyPrice(plan);
        const priceText = displayPrice(monthly?.price, monthly?.formattedPrice, monthly?.currencyCode || state.catalogCurrency);

        const inStock = plan._inStock !== false;
        const name = el('span', {
            class: inStock ? 'font-bold text-blue-400' : 'font-bold text-gray-500',
            text: plan.invoiceName || plan.planCode,
        });
        const stockBadge = inStock ? null : el('span', {
            class: 'ml-1 bg-red-600/30 text-red-400 text-xs px-1.5 py-0.5 rounded font-bold',
            text: 'OUT OF STOCK',
        });
        // One badge per real deployable location group; tooltip lists the DCs.
        const dcList = planDatacenters(plan).join(', ');
        const locationSpans = planLocations(plan).map(loc => el('span', {
            class: 'text-yellow-400 ml-1 text-xs',
            text: `[${loc}]`,
            title: dcList || undefined,
        }));
        const code = el('span', { class: 'text-gray-400 ml-2 text-xs', text: plan.planCode });
        const left = el('div', {}, [name, stockBadge, ...locationSpans, code].filter(Boolean));
        // Out-of-stock rows still show a readable price — the red badge marks
        // availability, so the price stays legible (muted, no dimming/strike).
        const price = el('span', {
            class: inStock ? 'text-green-400 text-sm' : 'text-gray-300 text-sm',
            text: priceText,
        });

        const isSelected = state.selectedPlanCode === plan.planCode;
        const div = el('div', {
            class: [
                'rounded p-2 text-sm flex justify-between items-center cursor-pointer transition-colors',
                'hover:bg-gray-600',
                isSelected
                    ? 'bg-blue-600/40 border border-blue-500'
                    : inStock
                        ? 'bg-gray-700'
                        : 'bg-gray-700/50',
            ].join(' '),
            role: 'button',
            tabindex: '0'
        }, [left, price]);
        // Each row remembers its own unselected background so deselection
        // restores it correctly — the closure's `inStock` belongs to the
        // clicked row, not the previously selected one.
        div.dataset.baseBg = inStock ? 'bg-gray-700' : 'bg-gray-700/50';

        const selectPlan = () => {
            document.getElementById('plan-select').value = plan.planCode;
            renderCatalogDetail(plan);
            // Hand-toggle the highlight classes on the two affected rows;
            // cheaper than re-rendering the list and keeps focus intact.
            const prev = container.querySelector('.bg-blue-600\\/40');
            if (prev) {
                prev.classList.remove('bg-blue-600/40', 'border', 'border-blue-500');
                prev.classList.add(prev.dataset.baseBg || 'bg-gray-700');
            }
            div.classList.remove(div.dataset.baseBg);
            div.classList.add('bg-blue-600/40', 'border', 'border-blue-500');
        };
        div.addEventListener('click', selectPlan);
        div.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                selectPlan();
            }
        });
        container.appendChild(div);
    });
    if (filtered.length > plans.length) {
        container.appendChild(el('p', {
            class: 'text-gray-500 text-xs text-center py-2',
            text: `Showing ${plans.length} of ${filtered.length} plans — refine your search to see the rest.`,
        }));
    }
}

// Human-readable addon label parsers

function humanizeAddon(code) {
    if (!code) return 'Unknown';
    const lower = code.toLowerCase();
    if (lower.startsWith('ram-')) return humanizeRam(code);
    if (lower.startsWith('hybridsoftraid-')) return humanizeHybridStorage(code);
    if (lower.startsWith('softraid-') || lower.startsWith('noraid-')) return humanizeStorage(code);
    if (lower.startsWith('bandwidth-')) return humanizeBandwidth(code);
    if (lower.startsWith('vrack-')) return humanizeVrack(code);
    if (lower.startsWith('traffic-')) return humanizeTraffic(code);
    return code;
}

function humanizeRam(code) {
    // ram-{size}g[-{type}]-[{speed}-]{product}-{region}
    // Patterns seen in the wild:
    //   ram-32g-ecc-2400-24risegame01-eu      → "32 GB ECC @ 2400 MHz"
    //   ram-64g-noecc-2133-25skle04-us        → "64 GB non-ECC @ 2133 MHz"
    //   ram-16g-24skstor01-us                 → "16 GB ECC" (no speed in code)
    //   ram-128g-on-die-ecc-3600-25risel01-eu → "128 GB On-Die ECC @ 3600 MHz"
    const m = code.match(/^ram-(\d+)g(?:-(on-die-ecc|ecc|noecc))?(?:-(\d+))?-/i);
    if (m) {
        const size = m[1];
        let type;
        if (!m[2]) type = 'ECC';
        else if (m[2].toLowerCase() === 'noecc') type = 'non-ECC';
        else if (m[2].toLowerCase() === 'on-die-ecc') type = 'On-Die ECC';
        else type = 'ECC';
        const speed = m[3];
        return speed ? `${size} GB ${type} @ ${speed} MHz` : `${size} GB ${type}`;
    }
    return code;
}

function humanizeStorage(code) {
    // softraid-{count}x{size}{type}-{product}-{region}
    // noraid-{count}x{size}{type}-{product}-{region}
    // e.g. softraid-2x480ssd-24sk60b-eu → "2× 480 GB SSD (SoftRAID)"
    //      noraid-1x120ssd-25skb01-eu → "1× 120 GB SSD (No RAID)"
    //      softraid-2x512nvme-... → "2× 512 GB NVMe (SoftRAID)"
    //      softraid-2x2000sa-... → "2× 2000 GB SATA HDD (SoftRAID)"
    const isRaid = code.toLowerCase().startsWith('softraid');
    const m = code.match(/^(?:softraid|noraid)-(\d+)x(\d+)(ssd|nvme|sa)-/i);
    if (m) {
        const count = m[1];
        const size = m[2];
        let typeLabel;
        switch (m[3].toLowerCase()) {
            case 'ssd': typeLabel = 'SSD'; break;
            case 'nvme': typeLabel = 'NVMe'; break;
            case 'sa': typeLabel = 'SATA HDD'; break;
            default: typeLabel = m[3].toUpperCase();
        }
        const raidLabel = isRaid ? 'SoftRAID' : 'No RAID';
        return `${count}× ${size} GB ${typeLabel} (${raidLabel})`;
    }
    return code;
}

function humanizeHybridStorage(code) {
    // hybridsoftraid-{count}x{size}{type}-{count}x{size}{type}-{product}-{region}
    // Mixed NVMe + HDD in soft RAID. Each disk group is {count}x{size}{type}.
    // e.g. hybridsoftraid-4x4000sa-1x500nvme-24skstor-us
    //      → "4× 4000 GB SATA HDD + 1× 500 GB NVMe (Hybrid SoftRAID)"
    const m = code.match(/^hybridsoftraid-((?:\d+x\d+(?:ssd|nvme|sa)-?)+)-/i);
    if (m) {
        const diskGroup = m[1];
        // Parse all disk segments: {count}x{size}{type}
        const diskRe = /(\d+)x(\d+)(ssd|nvme|sa)/gi;
        let disk;
        const disks = [];
        while ((disk = diskRe.exec(diskGroup)) !== null) {
            const count = disk[1];
            const size = disk[2];
            let typeLabel;
            switch (disk[3].toLowerCase()) {
                case 'ssd': typeLabel = 'SSD'; break;
                case 'nvme': typeLabel = 'NVMe'; break;
                case 'sa': typeLabel = 'SATA HDD'; break;
                default: typeLabel = disk[3].toUpperCase();
            }
            disks.push(`${count}× ${size} GB ${typeLabel}`);
        }
        if (disks.length) {
            return `${disks.join(' + ')} (Hybrid SoftRAID)`;
        }
    }
    return code;
}

function humanizeBandwidth(code) {
    // bandwidth-{speed}[-upto-{max}][-unguaranteed]-{product}-{region}
    // e.g. bandwidth-500-25sk-eu → "500 Mbps"
    //      bandwidth-1000-upto-2000-24sys3p-eu → "1-2 Gbps (burstable)"
    //      bandwidth-300-unguaranteed-25skle-us → "300 Mbps (unguaranteed)"
    //      bandwidth-6000-upto-12000-24sys3p-eu → "6-12 Gbps (burstable)"
    const m = code.match(/^bandwidth-(\d+)(?:-upto-(\d+))?(?:-(unguaranteed))?-/i);
    if (m) {
        const min = parseInt(m[1], 10);
        const fmt = (v) => v >= 1000 ? `${v / 1000} Gbps` : `${v} Mbps`;
        let label;
        if (m[2]) {
            const max = parseInt(m[2], 10);
            label = `${fmt(min)}-${fmt(max)} (burstable)`;
        } else {
            label = fmt(min);
        }
        if (m[3]) label += ' (unguaranteed)';
        return label;
    }
    return code;
}

function humanizeVrack(code) {
    // vrack-bandwidth-{min}[-upto-{max}]-{product}-{region}
    // e.g. vrack-bandwidth-1000-24sys-eu → "vRack 1 Gbps"
    //      vrack-bandwidth-1000-upto-2000-24rise-eu → "vRack 1-2 Gbps (burstable)"
    //      vrack-bandwidth-50000-upto-100000-24sys3p-eu → "vRack 50-100 Gbps (burstable)"
    //      vrack-bandwidth-500-25sk-eu → "vRack 500 Mbps"
    const m = code.match(/^vrack-bandwidth-(\d+)(?:-upto-(\d+))?/i);
    if (m) {
        const min = parseInt(m[1], 10);
        const fmt = (v) => v >= 1000 ? `${v / 1000} Gbps` : `${v} Mbps`;
        if (m[2]) {
            const max = parseInt(m[2], 10);
            return `vRack ${fmt(min)}-${fmt(max)} (burstable)`;
        }
        return `vRack ${fmt(min)}`;
    }
    return code;
}

function humanizeTraffic(code) {
    // traffic-{quota}-{speed}[-burst{burstspeed}]-{product}-{region}
    // e.g. traffic-unlimited-500-24sys-apac-ca → "500 Mbps unlimited"
    //      traffic-10tb-500-24sys-apac-ca → "500 Mbps · 10 TB quota"
    //      traffic-25tb-250-burst1g-24risegame-apac-ca → "250 Mbps (burst 1Gbps) · 25 TB quota"
    //      traffic-unlimited-250-burst1g-24risegame-sgp-ca → "250 Mbps (burst 1Gbps) unlimited"
    const m = code.match(/^traffic-(unlimited|\d+tb)-(\d+)(?:-burst(\d+[gm]))?-/i);
    if (m) {
        const quota = m[1].toLowerCase();
        const speed = parseInt(m[2], 10);
        const burst = m[3];
        const fmt = (v) => v >= 1000 ? `${v / 1000} Gbps` : `${v} Mbps`;
        let label = fmt(speed);
        if (burst) {
            const burstNum = parseInt(burst, 10);
            const burstLabel = burst.toLowerCase().endsWith('g') ? `${burstNum} Gbps` : `${burstNum} Mbps`;
            label += ` (burst ${burstLabel})`;
        }
        if (quota === 'unlimited') {
            label += ' unlimited';
        } else {
            label += ` · ${quota.toUpperCase()} quota`;
        }
        return label;
    }
    return code;
}

const DC_NAMES = {
    gra: 'GRA (Gravelines)',
    sbg: 'SBG (Strasbourg)',
    rbx: 'RBX (Roubaix)',
    bhs: 'BHS (Beauharnois)',
    fra: 'FRA (Frankfurt)',
    waw: 'WAW (Warsaw)',
    lon: 'LON (London)',
    sgp: 'SGP (Singapore)',
    syd: 'SYD (Sydney)',
    eri: 'ERI (Érije)',
    vin: 'VIN (Vint Hill)',
    hil: 'HIL (Hillsboro)',
};

function humanizeDatacenter(code) {
    return DC_NAMES[code?.toLowerCase()] || (code ? code.toUpperCase() : 'Unknown');
}

const OS_NAMES = {
    'none_64.en': 'No OS',
    'none_64.fr': 'No OS (FR)',
    'debian_64': 'Debian (64-bit)',
    'debian_11_64': 'Debian 11 (64-bit)',
    'debian_12_64': 'Debian 12 (64-bit)',
    'ubuntuserver_64': 'Ubuntu Server (64-bit)',
    'ubuntu_2004_64': 'Ubuntu 20.04 (64-bit)',
    'ubuntu_2204_64': 'Ubuntu 22.04 (64-bit)',
    'ubuntu_2404_64': 'Ubuntu 24.04 (64-bit)',
    'proxmox_64': 'Proxmox VE (64-bit)',
    'freebsd_64': 'FreeBSD (64-bit)',
    'windows_2022_64': 'Windows Server 2022 (64-bit)',
    'windows_2019_64': 'Windows Server 2019 (64-bit)',
    'windows_2016_64': 'Windows Server 2016 (64-bit)',
    'esxi_70_64': 'VMware ESXi 7.0 (64-bit)',
    'esxi_80_64': 'VMware ESXi 8.0 (64-bit)',
    'opnsense_64': 'OPNsense (64-bit)',
    'pfsense_64': 'pfSense (64-bit)',
    'rocky_9_64': 'Rocky Linux 9 (64-bit)',
    'alma_9_64': 'AlmaLinux 9 (64-bit)',
};

function humanizeOs(code) {
    return OS_NAMES[code?.toLowerCase()] || (code ? code : 'Unknown');
}

// Map addon full code -> short code (strip region suffix) for matching
// against stock data. e.g. 'ram-32g-ecc-2666-24sys-us' -> 'ram-32g-ecc-2666'
function addonShortCode(code) {
    if (!code) return '';
    const segs = code.split('-');
    return segs.length > 2 ? segs.slice(0, -2).join('-') : code;
}

// Normalize storage capacity codes for matching against stock data.
// OVH's catalog reports raw physical capacity (e.g. 512), while the stock
// API reports "marketed" capacity (e.g. 500). Use an explicit equivalence
// map for the known mismatches instead of blind rounding so unexpected
// capacity values aren't silently altered.
const STORAGE_CAPACITY_MAP = { 512: 500, 1920: 1900, 3840: 3800 };
function normalizeAddonCode(code) {
    if (!code) return '';
    // Match the disk-type suffix after the capacity. `ssd` was missing, so
    // marketed SSD capacities (e.g. 1920→1900, 3840→3800) were never
    // normalized and silently failed to match the stock API. Longer units
    // (sata, sas) come before their prefixes so they win the alternation.
    return code.replace(/(\d+)(ssd|nvme|sata|sas|sa|hdd)/gi, (m, num, unit) => {
        const n = parseInt(num, 10);
        if (n in STORAGE_CAPACITY_MAP) return STORAGE_CAPACITY_MAP[n] + unit;
        return m;
    });
}

function addonCodesMatch(a, b) {
    if (!a || !b) return true;
    const na = normalizeAddonCode(a);
    const nb = normalizeAddonCode(b);
    if (na === nb) return true;
    // Catalog addon codes are often truncated versions of the stock API
    // codes. e.g. catalog 'ram-16g' vs stock 'ram-16g-ecc-2133', or
    // catalog 'ram-32g-ecc-2666' vs stock 'ram-32g-ecc-2666'. Match if
    // one is a prefix of the other (on a segment boundary).
    const shorter = na.length < nb.length ? na : nb;
    const longer = na.length < nb.length ? nb : na;
    if (longer.startsWith(shorter + '-') || longer === shorter) return true;
    return false;
}

// Standardize the order of catalog config options. OVH returns addonFamilies
// (and the addons within each) in an arbitrary order; render them in a fixed
// family sequence and, within each family, from included → small → large.
const CATALOG_FAMILY_ORDER = ['memory', 'storage', 'bandwidth', 'vrack'];

function addonCapacity(code) {
    // A comparable size for tie-breaking same-priced options. Storage codes
    // like 'softraid-2x960nvme' → 2*960; otherwise the first integer
    // ('ram-32g' → 32, 'bandwidth-500' → 500); else 0.
    const lower = (code || '').toLowerCase();
    const nx = lower.match(/(\d+)x(\d+)/);
    if (nx) return parseInt(nx[1], 10) * parseInt(nx[2], 10);
    const m = lower.match(/(\d+)/);
    return m ? parseInt(m[1], 10) : 0;
}

function compareAddonCodes(a, b) {
    // Price ascending (included/$0 first, missing last), then capacity, then
    // code — a deterministic, sensible ordering. Reuses the same price map the
    // rest of the UI reads via getAddonPrice/addonPriceLabel.
    const pa = state.addonPrices[a]?.price ?? Infinity;
    const pb = state.addonPrices[b]?.price ?? Infinity;
    if (pa !== pb) return pa - pb;
    const ca = addonCapacity(a);
    const cb = addonCapacity(b);
    if (ca !== cb) return ca - cb;
    return (a || '').localeCompare(b || '');
}

function renderCatalogDetail(plan) {
    state.selectedPlanCode = plan.planCode;
    const container = document.getElementById('catalog-detail');
    container.innerHTML = '';

    const monthly = getPlanMonthlyPrice(plan);
    const setup = getPlanSetupFee(plan);
    const priceText = displayPrice(monthly?.price, monthly?.formattedPrice, monthly?.currencyCode || state.catalogCurrency);
    const setupText = displayPrice(setup?.price, setup?.formattedPrice, setup?.currencyCode || state.catalogCurrency);

    // Parse server name + CPU from invoiceName (format: "MODEL | CPU")
    const parts = (plan.invoiceName || plan.planCode).split('|');
    const serverModel = parts[0].trim();
    const cpuFromInvoice = parts.length > 1 ? parts[1].trim() : null;

    // Commercial info from blobs (use cases, etc.)
    const blobs = plan.blobs || {};
    const commercial = blobs.commercial || {};
    const useCase = (commercial.features || []).find(f => f.name === 'baremetal-server-usecases')?.value;

    // Product specs (CPU, chassis, services) come from the catalog's
    // top-level products array, linked via plan.product. These contain
    // the real CPU model/cores/frequency even for LE/flash-sale plans
    // whose invoiceName is just "SYS-LE-1" with no CPU info.
    const productSpec = state.productSpecs[plan.product] || {};
    const cpu = productSpec.cpu;
    const frame = productSpec.frame;
    const services = productSpec.services;
    const productDesc = productSpec.description;

    // Build a CPU description from product specs, falling back to
    // the invoiceName's "| CPU" suffix for older catalog responses.
    function buildCpuDescription() {
        if (cpu && cpu.model) {
            let desc = `${cpu.brand} ${cpu.model}`.trim();
            const coreInfo = cpu.cores ? `${cpu.cores}c/${cpu.threads || cpu.cores}t` : '';
            const freqInfo = cpu.frequency ? `${cpu.frequency}GHz` : '';
            const boostInfo = cpu.boost ? `(${cpu.boost}GHz boost)` : '';
            const parts2 = [coreInfo, freqInfo, boostInfo].filter(Boolean);
            if (parts2.length) desc += ` · ${parts2.join(' ')}`;
            if (cpu.number && cpu.number > 1) desc += ` · ${cpu.number} CPU`;
            return desc;
        }
        if (cpu && cpu.cores) {
            let desc = `${cpu.cores} cores`;
            if (cpu.threads) desc += ` / ${cpu.threads} threads`;
            if (cpu.frequency) desc += ` · ${cpu.frequency}GHz`;
            return desc;
        }
        // Fall back to the invoiceName's CPU suffix (regular plans)
        if (cpuFromInvoice) return cpuFromInvoice;
        // Fall back to the product description (usually the CPU model)
        if (productDesc) return productDesc;
        return null;
    }

    const cpuDesc = buildCpuDescription();

    // Header
    container.appendChild(el('h2', { class: 'text-2xl font-bold text-blue-400 mb-1', text: serverModel }));
    if (cpuDesc) {
        container.appendChild(el('p', { class: 'text-gray-300 mb-1', text: cpuDesc }));
    }
    // Hardware spec badges: CPU score, chassis, SLA, anti-DDoS, range
    const specBadges = [];
    if (cpu && cpu.geekbench6) {
        const gb = cpu.geekbench6;
        specBadges.push(`Geekbench 6: ${gb.single.toLocaleString()} single · ${gb.multi.toLocaleString()} multi`);
    }
    if (frame && frame.size) specBadges.push(`${frame.size} chassis`);
    if (frame && frame.dualPowerSupply) specBadges.push('Dual PSU');
    if (services && services.sla) specBadges.push(`${services.sla}% SLA`);
    if (services && services.antiddos) specBadges.push(`Anti-DDoS ${services.antiddos}`);
    if (productSpec.range) specBadges.push(`${productSpec.range.toUpperCase()} range`);
    if (specBadges.length) {
        const badgesRow = el('div', { class: 'flex flex-wrap gap-1 mb-2' });
        for (const badge of specBadges) {
            badgesRow.appendChild(el('span', {
                class: 'inline-block bg-gray-700 text-gray-300 text-xs px-2 py-1 rounded',
                text: badge,
            }));
        }
        container.appendChild(badgesRow);
    }
    // Location badges from the plan's real deployable datacenters (hover
    // for the DC list); the per-DC stock table below gives the live detail.
    const detailDcs = planDatacenters(plan).join(', ');
    for (const loc of planLocations(plan)) {
        container.appendChild(el('span', {
            class: 'inline-block bg-yellow-600/30 text-yellow-400 text-xs px-2 py-1 rounded mb-2 mr-1',
            text: loc,
            title: detailDcs || undefined,
        }));
    }
    container.appendChild(el('p', { class: 'text-gray-500 text-xs font-mono mb-4', text: plan.planCode }));

    // Price section (updates live as you change options)
    const priceSection = el('div', { class: 'bg-gray-700 rounded p-3 mb-4' });
    priceSection.appendChild(el('div', { class: 'flex justify-between items-center' }, [
        el('span', { class: 'text-gray-400 text-sm', text: 'Monthly price' }),
        el('span', { id: 'detail-total-price', class: 'text-green-400 font-bold text-lg', text: priceText }),
    ]));
    // One-time setup/installation fee row (shown only if OVH lists one)
    const setupRow = el('div', { id: 'detail-setup-row', class: 'flex justify-between items-center mt-1 hidden' }, [
        el('span', { class: 'text-gray-400 text-sm', text: 'Setup fee (one-time)' }),
        el('span', { id: 'detail-setup-price', class: 'text-yellow-400 font-bold text-sm' }),
    ]);
    priceSection.appendChild(setupRow);
    if (monthly?.promotions?.length) {
        const promo = monthly.promotions[0];
        priceSection.appendChild(el('p', { class: 'text-yellow-400 text-xs mt-1', text: `Promo: ${promo.name} (${promo.formattedValue || promo.value + '%'} off)` }));
    }
    container.appendChild(priceSection);

    function getAddonPrice(addonCode) {
        const info = state.addonPrices[addonCode];
        if (!info) return null;
        return info;
    }

    function calcTotal() {
        let total = (monthly?.price || 0);
        for (const famName of ['memory', 'storage', 'bandwidth', 'vrack']) {
            const addon = selectedAddons[famName];
            if (!addon) continue;
            const info = getAddonPrice(addon);
            if (info && info.price) total += info.price;
        }
        return total;
    }

    function calcSetupTotal() {
        let total = (setup?.price || 0);
        for (const famName of ['memory', 'storage', 'bandwidth', 'vrack']) {
            const addon = selectedAddons[famName];
            if (!addon) continue;
            const info = getAddonPrice(addon);
            if (info && info.setup_price) total += info.setup_price;
        }
        return total;
    }

    function updateTotalPrice() {
        const total = calcTotal();
        const el2 = document.getElementById('detail-total-price');
        if (el2) {
            if (total > 0) {
                el2.textContent = formatCurrency(convertMicrocents(total));
            } else {
                el2.textContent = priceText;
            }
        }
        // Setup fee row: plan setup + sum of selected addon setup fees
        const setupTotal = calcSetupTotal();
        const setupRowEl = document.getElementById('detail-setup-row');
        const setupPriceEl = document.getElementById('detail-setup-price');
        if (setupRowEl && setupPriceEl) {
            if (setupTotal > 0) {
                setupPriceEl.textContent = '+' + formatCurrency(convertMicrocents(setupTotal));
                setupRowEl.classList.remove('hidden');
            } else {
                setupRowEl.classList.add('hidden');
            }
        }
    }

    function addonPriceLabel(addonCode) {
        const info = getAddonPrice(addonCode);
        if (!info) return '';
        if (info.price === 0) return 'included';
        const fromCode = info.currencyCode || state.catalogCurrency;
        return '+' + displayPrice(info.price, info.formattedPrice, fromCode);
    }

    // Hardware specs from addonFamilies - selectable cards with prices.
    // Only hardware families are shown; license families (application-license,
    // distribution-license) are excluded because they don't participate in the
    // FQN, totals, order form, or rush order — showing them as selectable
    // cards would be misleading (the CA catalog includes them, the US one
    // doesn't, so filtering also makes both endpoints render consistently).
    const HARDWARE_FAMILIES = new Set(['memory', 'storage', 'bandwidth', 'vrack']);
    // OVH returns families and their addons in arbitrary order. Standardize:
    // fixed family sequence, and included→small→large within each. Shallow-clone
    // so the shared state.plans[...].addonFamilies is never mutated. Both the
    // option cards and the order-form dropdowns consume this same `families`
    // array, so sorting here standardizes both.
    const families = (plan.addonFamilies || [])
        .filter(f => HARDWARE_FAMILIES.has(f.name))
        .map(f => ({ ...f, addons: [...(f.addons || [])].sort(compareAddonCodes) }))
        .sort((a, b) =>
            ((CATALOG_FAMILY_ORDER.indexOf(a.name) + 1) || 99) -
            ((CATALOG_FAMILY_ORDER.indexOf(b.name) + 1) || 99));
    const specsSection = el('div', { class: 'space-y-3 mb-4' });
    specsSection.appendChild(el('h3', { class: 'font-bold text-gray-400 text-sm uppercase mb-2', text: 'Configuration Options' }));

    // Track selected addon per family (defaults to the plan's default)
    const selectedAddons = {};
    for (const fam of families) {
        selectedAddons[fam.name] = fam.default || (fam.addons || [])[0] || null;
    }

    // Build the FQN string from the plan base + selected addon short codes.
    // OVH FQN format: {planBase}.{memory}.{storage}.{bandwidth}.{vrack} - order matters!
    function buildFqn() {
        const planBase = plan.planCode.split('-').slice(0, -1).join('-') || plan.planCode;
        const parts = [planBase];
        for (const famName of ['memory', 'storage', 'bandwidth', 'vrack']) {
            const addon = selectedAddons[famName];
            if (!addon) continue;
            const segs = addon.split('-');
            const short = segs.length > 2 ? segs.slice(0, -2).join('-') : addon;
            parts.push(short);
        }
        return parts.join('.');
    }

    const fqnPreview = el('div', { class: 'bg-gray-700 rounded p-2 mb-3' }, [
        el('span', { class: 'text-gray-500 text-xs', text: 'FQN: ' }),
        el('code', { id: 'fqn-preview', class: 'text-blue-300 text-xs font-mono', text: buildFqn() }),
    ]);

    function updateFqnPreview() {
        const el2 = document.getElementById('fqn-preview');
        if (el2) el2.textContent = buildFqn();
    }

    function syncOrderForm(famName, addon) {
        const sel = document.getElementById(`order-${famName}`);
        if (sel) sel.value = addon;
    }

    function renderCard(fam, addon, isSelected, isDefault) {
        const card = el('div', {
            class: `flex items-center justify-between rounded px-3 py-2 cursor-pointer transition-colors ${isSelected ? 'bg-blue-600/30 border border-blue-500' : 'bg-gray-700 border border-gray-600 hover:bg-gray-600'}`,
            role: 'button',
            tabindex: '0',
        });
        card.dataset.addon = addon;
        card.dataset.default = isDefault ? '1' : '0';

        // Prefer OVH's invoiceName (always present, descriptive) and fall
        // back to our humanizer only if the addon has no price entry.
        const info = getAddonPrice(addon);
        const ovhName = info?.invoiceName;
        const labelText = ovhName || humanizeAddon(addon);
        const labelSpan = el('span', { class: isSelected ? 'text-blue-300 font-bold' : 'text-gray-300', text: labelText });
        labelSpan.dataset.label = '1';

        const leftSide = el('div', {}, [labelSpan]);

        const priceLabel = addonPriceLabel(addon);
        const rightSide = el('div', { class: 'flex items-center gap-2' });
        if (priceLabel) {
            rightSide.appendChild(el('span', {
                class: priceLabel === 'included' ? 'text-gray-500 text-xs' : 'text-yellow-400 text-xs font-bold',
                text: priceLabel,
            }));
        }
        const badgeSpan = el('span', {
            class: isSelected ? 'text-blue-400 text-xs font-bold' : (isDefault ? 'text-green-500 text-xs' : ''),
            text: isSelected ? 'SELECTED' : (isDefault ? 'DEFAULT' : ''),
        });
        badgeSpan.dataset.badge = '1';
        rightSide.appendChild(badgeSpan);

        card.appendChild(leftSide);
        card.appendChild(rightSide);

        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                card.click();
            }
        });
        return card;
    }

    for (const fam of families) {
        const famName = fam.name.charAt(0).toUpperCase() + fam.name.slice(1);
        const itemsContainer = el('div', { class: 'space-y-1' });

        for (const addon of (fam.addons || [])) {
            const isDefault = addon === fam.default;
            const isSelected = addon === selectedAddons[fam.name];
            const card = renderCard(fam, addon, isSelected, isDefault);

            card.addEventListener('click', () => {
                selectedAddons[fam.name] = addon;
                // Re-render all cards in this family
                itemsContainer.querySelectorAll('[data-addon]').forEach(c => {
                    const cAddon = c.dataset.addon;
                    const cIsDefault = c.dataset.default === '1';
                    const selected = cAddon === addon;
                    // Update card class
                    c.className = `flex items-center justify-between rounded px-3 py-2 cursor-pointer transition-colors ${selected ? 'bg-blue-600/30 border border-blue-500' : 'bg-gray-700 border border-gray-600 hover:bg-gray-600'}`;
                    // Update label class
                    const label = c.querySelector('[data-label]');
                    if (label) label.className = selected ? 'text-blue-300 font-bold' : 'text-gray-300';
                    // Update badge text AND class
                    const badge = c.querySelector('[data-badge]');
                    if (badge) {
                        badge.textContent = selected ? 'SELECTED' : (cIsDefault ? 'DEFAULT' : '');
                        badge.className = selected ? 'text-blue-400 text-xs font-bold' : (cIsDefault ? 'text-green-500 text-xs' : '');
                    }
                });
                updateFqnPreview();
                updateTotalPrice();
                syncOrderForm(fam.name, addon);
                updateStockDisplay();
            });

            itemsContainer.appendChild(card);
        }

        specsSection.appendChild(el('div', {}, [
            el('p', { class: 'text-gray-400 text-sm font-bold mb-1', text: `${famName}${fam.mandatory ? ' *' : ''}` }),
            itemsContainer,
        ]));
    }
    if (!families.length) {
        specsSection.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No addon configurations listed.' }));
    }
    container.appendChild(specsSection);
    container.appendChild(fqnPreview);

    // Initial total price calc
    updateTotalPrice();

    // Plan-level configuration schema (used for OS options + order form DC list)
    const configs = plan.configurations || [];

    // Live stock section - replaces the old static "Available Datacenters"
    // list. Fetches /dedicated/server/datacenter/availabilities and shows
    // which DCs have stock for the currently selected RAM+storage combo.
    // Updates dynamically as the user changes selections.
    const stockSection = el('div', { class: 'mb-4', id: 'stock-section' }, [
        el('p', { class: 'text-gray-400 text-sm font-bold mb-1', text: 'Live Stock' }),
        el('p', { class: 'text-gray-500 text-xs', text: 'Checking availability...' }),
    ]);
    container.appendChild(stockSection);

    // Store stock data for lookups when addons change
    let stockData = [];

    function updateStockDisplay() {
        const sec = document.getElementById('stock-section');
        if (!sec) return;
        if (!stockData.length) {
            sec.innerHTML = '';
            sec.appendChild(el('p', { class: 'text-gray-400 text-sm font-bold mb-1', text: 'Live Stock' }));
            sec.appendChild(el('p', { class: 'text-gray-500 text-xs', text: 'Stock data unavailable.' }));
            return;
        }
        const memShort = addonShortCode(selectedAddons.memory);
        const storShort = addonShortCode(selectedAddons.storage);
        // Find the matching entry in stock data (with capacity normalization)
        const match = stockData.find(e =>
            addonCodesMatch(memShort, e.memory) &&
            addonCodesMatch(storShort, e.storage)
        );
        sec.innerHTML = '';
        sec.appendChild(el('p', { class: 'text-gray-400 text-sm font-bold mb-1', text: 'Live Stock' }));
        if (!match) {
            sec.appendChild(el('p', { class: 'text-gray-500 text-xs', text: 'No stock data for this configuration.' }));
            return;
        }
        const dcs = (match.datacenters || []);
        const available = dcs.filter(d => d.availability !== 'unavailable' && d.availability !== 'comingSoon');
        const comingSoon = dcs.filter(d => d.availability === 'comingSoon');
        const unavailable = dcs.filter(d => d.availability === 'unavailable');
        if (available.length === 0 && comingSoon.length === 0) {
            sec.appendChild(el('p', { class: 'text-red-400 text-xs font-bold', text: 'Out of stock in all datacenters' }));
        } else {
            if (available.length === 0) {
                sec.appendChild(el('p', { class: 'text-yellow-400 text-xs font-bold mb-1', text: 'Coming soon — not yet orderable' }));
            }
            for (const dc of available) {
                const badge = el('span', {
                    class: 'inline-block bg-green-700/30 text-green-400 text-xs px-2 py-1 rounded mr-1 mb-1',
                    text: `${humanizeDatacenter(dc.datacenter)} (${dc.availability})`,
                });
                sec.appendChild(badge);
            }
            for (const dc of comingSoon) {
                const badge = el('span', {
                    class: 'inline-block bg-yellow-600/30 text-yellow-400 text-xs px-2 py-1 rounded mr-1 mb-1',
                    text: `${humanizeDatacenter(dc.datacenter)} (soon)`,
                });
                sec.appendChild(badge);
            }
        }
        // Show unavailable DCs in muted style
        if (unavailable.length) {
            for (const dc of unavailable) {
                sec.appendChild(el('span', {
                    class: 'inline-block bg-gray-700 text-gray-600 text-xs px-2 py-1 rounded mr-1 mb-1 line-through',
                    text: humanizeDatacenter(dc.datacenter),
                }));
            }
        }
    }

    // Fetch stock data asynchronously (don't block rendering). Guard
    // against a stale response landing after the user has since selected
    // a different plan (rapid clicking) - only apply it if this fetch's
    // plan is still the one selected.
    const stockFetchPlanCode = plan.planCode;
    apiRequest('GET', `/catalog/stock?plan_code=${encodeURIComponent(stockFetchPlanCode)}`)
        .then(data => {
            if (state.selectedPlanCode !== stockFetchPlanCode) return;
            stockData = data || [];
            updateStockDisplay();
        })
        .catch(() => { /* stock section stays "unavailable" */ });

    // OS options
    const osConfig = configs.find(c => c.name === 'dedicated_os');
    if (osConfig && osConfig.values && osConfig.values.length) {
        const osList = osConfig.values.map(os => el('span', { class: 'inline-block bg-gray-700 text-gray-300 text-xs px-2 py-1 rounded mr-1 mb-1', text: humanizeOs(os) }));
        container.appendChild(el('div', { class: 'mb-4' }, [
            el('p', { class: 'text-gray-400 text-sm font-bold mb-1', text: 'OS Options' }),
            el('div', {}, osList),
        ]));
    }

    // Use case (split comma-separated values into separate badges)
    if (useCase) {
        const useCases = useCase.split(',').map(s => s.trim()).filter(Boolean);
        const badges = useCases.map(uc => el('span', { class: 'inline-block bg-blue-600/30 text-blue-400 text-xs px-2 py-1 rounded mr-1 mb-1', text: uc }));
        container.appendChild(el('div', { class: 'mb-4' }, badges));
    }

    // Order form + actions
    const orderSection = el('div', { class: 'mt-4 pt-4 border-t border-gray-700' });

    // Quick actions row
    orderSection.appendChild(el('div', { class: 'flex gap-2 mb-4' }, [
        el('button', {
            class: 'bg-blue-600 hover:bg-blue-700 px-4 py-2 rounded font-bold',
            text: 'Watch This Plan',
            onclick: () => {
                // Auto-populate the monitor tab's add-server form
                document.getElementById('plan-select').value = plan.planCode;
                // Build the FQN from selected addons and pre-fill the pattern
                const fqn = buildFqn();
                document.getElementById('fqn-pattern').value = fqn;

                // Also pre-fill the rush order form
                document.getElementById('rush-plan-code').value = plan.planCode;
                document.getElementById('rush-fqn').value = fqn;
                if (selectedAddons.memory) {
                    const ramSelect = document.getElementById('rush-ram');
                    if (ramSelect) {
                        // Add an option for this addon if not already present
                        if (![...ramSelect.options].some(o => o.value === selectedAddons.memory)) {
                            ramSelect.appendChild(el('option', { value: selectedAddons.memory, text: humanizeAddon(selectedAddons.memory) }));
                        }
                        ramSelect.value = selectedAddons.memory;
                    }
                }
                if (selectedAddons.storage) {
                    const storageSelect = document.getElementById('rush-storage');
                    if (storageSelect) {
                        if (![...storageSelect.options].some(o => o.value === selectedAddons.storage)) {
                            storageSelect.appendChild(el('option', { value: selectedAddons.storage, text: humanizeAddon(selectedAddons.storage) }));
                        }
                        storageSelect.value = selectedAddons.storage;
                    }
                }
                if (selectedAddons.bandwidth) {
                    const bwSelect = document.getElementById('rush-bandwidth');
                    if (bwSelect) {
                        if (![].some.call(bwSelect.options, o => o.value === selectedAddons.bandwidth)) {
                            bwSelect.appendChild(el('option', { value: selectedAddons.bandwidth, text: humanizeAddon(selectedAddons.bandwidth) }));
                        }
                        bwSelect.value = selectedAddons.bandwidth;
                    }
                }
                // Pre-fill datacenter from the plan's configurations
                const dcForWatch = configs.find(c => c.name === 'dedicated_datacenter');
                if (dcForWatch?.values?.length) {
                    document.querySelectorAll('.rush-dc').forEach(cb => {
                        cb.checked = dcForWatch.values.includes(cb.value);
                    });
                }
                // Pre-fill region from endpoint
                const regionInfo = OVH_REGIONS[state.endpoint] || OVH_REGIONS['ovh-eu'];
                const rushRegion = document.getElementById('rush-region');
                if (rushRegion) rushRegion.value = regionInfo.rushRegion;

                switchTab('monitor-tab');
                document.getElementById('fqn-pattern').focus();
            }
        }),
        el('button', {
            class: 'bg-green-600 hover:bg-green-700 px-4 py-2 rounded font-bold',
            text: 'Order Now',
            onclick: () => {
                const form = document.getElementById('catalog-order-form');
                if (form) form.classList.toggle('hidden');
            }
        }),
    ]));

    // Inline order form (hidden until "Order Now" is clicked)
    const orderForm = el('div', { id: 'catalog-order-form', class: 'hidden bg-gray-700 rounded p-4 space-y-3' });

    // Build addon dropdowns from the plan's addonFamilies
    const famMap = {};
    for (const fam of families) {
        famMap[fam.name] = fam;
    }

    // RAM dropdown
    if (famMap.memory) {
        const select = el('select', { id: 'order-ram', class: 'w-full bg-gray-900 px-3 py-2 rounded text-sm' });
        select.addEventListener('change', () => { selectedAddons.memory = select.value; updateFqnPreview(); });
        for (const addon of (famMap.memory.addons || [])) {
            const isDefault = addon === famMap.memory.default;
            select.appendChild(el('option', { value: addon, text: `${humanizeAddon(addon)}${isDefault ? ' (default)' : ''}`, selected: addon === selectedAddons.memory }));
        }
        orderForm.appendChild(el('div', {}, [
            el('label', { class: 'block text-gray-400 text-xs mb-1', text: 'Memory' }),
            select,
        ]));
    }

    // Storage dropdown
    if (famMap.storage) {
        const select = el('select', { id: 'order-storage', class: 'w-full bg-gray-900 px-3 py-2 rounded text-sm' });
        select.addEventListener('change', () => { selectedAddons.storage = select.value; updateFqnPreview(); });
        for (const addon of (famMap.storage.addons || [])) {
            const isDefault = addon === famMap.storage.default;
            select.appendChild(el('option', { value: addon, text: `${humanizeAddon(addon)}${isDefault ? ' (default)' : ''}`, selected: addon === selectedAddons.storage }));
        }
        orderForm.appendChild(el('div', {}, [
            el('label', { class: 'block text-gray-400 text-xs mb-1', text: 'Storage' }),
            select,
        ]));
    }

    // Bandwidth dropdown
    if (famMap.bandwidth) {
        const select = el('select', { id: 'order-bandwidth', class: 'w-full bg-gray-900 px-3 py-2 rounded text-sm' });
        select.addEventListener('change', () => { selectedAddons.bandwidth = select.value; updateFqnPreview(); });
        for (const addon of (famMap.bandwidth.addons || [])) {
            const isDefault = addon === famMap.bandwidth.default;
            select.appendChild(el('option', { value: addon, text: `${humanizeAddon(addon)}${isDefault ? ' (default)' : ''}`, selected: addon === selectedAddons.bandwidth }));
        }
        orderForm.appendChild(el('div', {}, [
            el('label', { class: 'block text-gray-400 text-xs mb-1', text: 'Bandwidth' }),
            select,
        ]));
    }

    // Datacenter dropdown from configurations
    const dcConfigForOrder = configs.find(c => c.name === 'dedicated_datacenter');
    if (dcConfigForOrder && dcConfigForOrder.values && dcConfigForOrder.values.length) {
        const select = el('select', { id: 'order-datacenter', class: 'w-full bg-gray-900 px-3 py-2 rounded text-sm' });
        for (const dc of dcConfigForOrder.values) {
            select.appendChild(el('option', { value: dc, text: dc.toUpperCase() }));
        }
        orderForm.appendChild(el('div', {}, [
            el('label', { class: 'block text-gray-400 text-xs mb-1', text: 'Datacenter' }),
            select,
        ]));
    }

    // Duration + OS in a row
    const durOsRow = el('div', { class: 'grid grid-cols-2 gap-2' });
    const durSelect = el('select', { id: 'order-duration', class: 'w-full bg-gray-900 px-3 py-2 rounded text-sm' });
    for (const [val, label] of [['P1M','1 month'],['P3M','3 months'],['P6M','6 months'],['P12M','12 months'],['P24M','24 months']]) {
        durSelect.appendChild(el('option', { value: val, text: label, selected: val === (state.checkoutDefaults?.duration || 'P1M') }));
    }
    durOsRow.appendChild(el('div', {}, [
        el('label', { class: 'block text-gray-400 text-xs mb-1', text: 'Duration' }),
        durSelect,
    ]));

    const osConfigForOrder = configs.find(c => c.name === 'dedicated_os');
    const osSelect = el('select', { id: 'order-os', class: 'w-full bg-gray-900 px-3 py-2 rounded text-sm' });
    if (osConfigForOrder && osConfigForOrder.values) {
        for (const os of osConfigForOrder.values) {
            osSelect.appendChild(el('option', { value: os, text: os }));
        }
    } else {
        osSelect.appendChild(el('option', { value: 'none_64.en', text: 'No OS' }));
    }
    durOsRow.appendChild(el('div', {}, [
        el('label', { class: 'block text-gray-400 text-xs mb-1', text: 'OS' }),
        osSelect,
    ]));
    orderForm.appendChild(durOsRow);

    // Checkboxes (pre-filled from billing defaults)
    const checkboxRow = el('div', { class: 'flex flex-wrap gap-4' }, [
        el('label', { class: 'flex items-center gap-2 text-sm' }, [
            el('input', { type: 'checkbox', id: 'order-auto-pay', class: 'w-4 h-4', checked: state.checkoutDefaults?.auto_pay || false }),
            el('span', { text: 'Auto-pay' }),
        ]),
        el('label', { class: 'flex items-center gap-2 text-sm' }, [
            el('input', { type: 'checkbox', id: 'order-waive', class: 'w-4 h-4', checked: state.checkoutDefaults?.waive_retractation !== false }),
            el('span', { text: 'Waive retraction' }),
        ]),
    ]);
    orderForm.appendChild(checkboxRow);

    // Place Order button
    orderForm.appendChild(el('button', {
        class: 'w-full bg-green-600 hover:bg-green-700 py-2 rounded font-bold',
        text: 'Place Order',
        onclick: async () => {
            const ram = document.getElementById('order-ram')?.value || null;
            const storage = document.getElementById('order-storage')?.value || null;
            const bandwidth = document.getElementById('order-bandwidth')?.value || null;
            const dc = document.getElementById('order-datacenter')?.value;
            const duration = document.getElementById('order-duration')?.value || 'P1M';
            const osVal = document.getElementById('order-os')?.value || 'none_64.en';
            const autoPay = document.getElementById('order-auto-pay')?.checked || false;
            const waive = document.getElementById('order-waive')?.checked || false;
            const regionInfo = OVH_REGIONS[state.endpoint] || OVH_REGIONS['ovh-eu'];
            const maxPrice = state.checkoutDefaults?.max_price ?? null;

            const setupFeeText = setup?.price
                ? ` + ${formatCurrency(convertMicrocents(setup.price))} setup`
                : '';

            if (!confirm(`Place order for ${serverModel}?\n${monthly?.formattedPrice || priceText}/mo${setupFeeText}\nDC: ${(dc||'default').toUpperCase()}\nDuration: ${duration}`)) {
                return;
            }

            try {
                showLoading();
                const result = await apiRequest('POST', '/checkout/rush', {
                    plan_code: plan.planCode,
                    fqn: buildFqn(),
                    ram, storage, bandwidth,
                    datacenters: dc ? [dc] : [],
                    region: regionInfo.rushRegion,
                    os: osVal,
                    duration,
                    auto_pay: autoPay,
                    waive_retractation: waive,
                    max_price: maxPrice,
                });
                document.getElementById('order-id').textContent = `Order ID: ${result.orderId || 'N/A'}`;
                document.getElementById('order-url').href = result.url || '#';
                showView('order-complete');
                await loadOrders();
            } catch (e) {
                showError(e.message);
            } finally {
                hideLoading();
            }
        }
    }));

    orderSection.appendChild(orderForm);
    container.appendChild(orderSection);
}

function switchTab(tabId) {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        const target = btn.dataset.tab;
        if (target === tabId) {
            btn.className = 'tab-btn px-4 py-2 rounded-t-lg bg-gray-800 text-blue-400 font-bold';
        } else {
            btn.className = 'tab-btn px-4 py-2 rounded-t-lg bg-gray-700 text-gray-400 hover:text-gray-200';
        }
    });
    document.getElementById('monitor-tab').classList.toggle('hidden', tabId !== 'monitor-tab');
    document.getElementById('catalog-tab').classList.toggle('hidden', tabId !== 'catalog-tab');
    const billingTab = document.getElementById('billing-tab');
    if (billingTab) billingTab.classList.toggle('hidden', tabId !== 'billing-tab');
    const insightsTab = document.getElementById('insights-tab');
    if (insightsTab) insightsTab.classList.toggle('hidden', tabId !== 'insights-tab');
    const ordersTab = document.getElementById('orders-tab');
    if (ordersTab) ordersTab.classList.toggle('hidden', tabId !== 'orders-tab');
    const serversTab = document.getElementById('servers-tab');
    if (serversTab) serversTab.classList.toggle('hidden', tabId !== 'servers-tab');
    const logsTab = document.getElementById('logs-tab');
    if (logsTab) logsTab.classList.toggle('hidden', tabId !== 'logs-tab');
    // Stop the live log tail whenever we leave the logs tab.
    if (tabId !== 'logs-tab') stopLogStream();
    // Lazy-load billing data when switching to that tab
    if (tabId === 'billing-tab' && !state.billingLoaded) {
        loadBillingInfo();
    }
    // Lazy-load orders data when switching to the orders tab. Returns the
    // promise so callers that need to await it (e.g. openOrderInTab) can;
    // other callers ignore the return value and it runs fire-and-forget.
    if (tabId === 'orders-tab') {
        return loadOrdersTab();
    }
    // Lazy-load owned servers when switching to the servers tab.
    if (tabId === 'servers-tab') {
        return loadServersTab();
    }
    // Refresh insights plan dropdown + overview when switching to that tab
    if (tabId === 'insights-tab') {
        populateInsightsPlanSelect();
        loadInsightsData();
    }
    // Lazy-load logs + start the live tail when switching to the logs tab.
    if (tabId === 'logs-tab') {
        return loadLogsTab();
    }
}

// Billing & account info

async function loadBillingInfo() {
    await Promise.all([loadAccountInfo(), loadPaymentMethods(), loadCheckoutDefaults(), loadBillingInvoices()]);
    state.billingLoaded = true;
}

// Notification settings

async function loadNotificationSettings() {
    try {
        const data = await apiRequest('GET', '/settings/notifications');
        const s = data?.settings || {};
        const setVal = (id, val) => {
            const el2 = document.getElementById(id);
            if (el2) el2.value = val || '';
        };
        setVal('notif-telegram-token', s.telegram_bot_token);
        setVal('notif-telegram-chat-id', s.telegram_chat_id);
        setVal('notif-discord-webhook', s.discord_webhook_url);
        setVal('notif-slack-webhook', s.slack_webhook_url);
        setVal('notif-smtp-host', s.smtp_host);
        setVal('notif-smtp-port', s.smtp_port || 587);
        setVal('notif-smtp-username', s.smtp_username);
        setVal('notif-smtp-password', s.smtp_password);
        setVal('notif-smtp-from', s.smtp_from);
        setVal('notif-smtp-to', s.notify_email_to);
        const channels = data?.configured || [];
        const status = document.getElementById('notif-status');
        if (status) {
            status.textContent = channels.length
                ? `Active: ${channels.join(', ')}`
                : 'No channels configured';
        }
    } catch (e) {
        console.error('Failed to load notification settings:', e);
        showToast(`Failed to load notification settings: ${e.message}`, 4000);
    }
}

function collectNotifierBody() {
    return {
        telegram_bot_token: document.getElementById('notif-telegram-token').value,
        telegram_chat_id: document.getElementById('notif-telegram-chat-id').value,
        discord_webhook_url: document.getElementById('notif-discord-webhook').value,
        slack_webhook_url: document.getElementById('notif-slack-webhook').value,
        smtp_host: document.getElementById('notif-smtp-host').value,
        smtp_port: parseInt(document.getElementById('notif-smtp-port').value) || 587,
        smtp_username: document.getElementById('notif-smtp-username').value,
        smtp_password: document.getElementById('notif-smtp-password').value,
        smtp_from: document.getElementById('notif-smtp-from').value,
        notify_email_to: document.getElementById('notif-smtp-to').value,
    };
}

async function saveNotificationSettings() {
    try {
        await apiRequest('PUT', '/settings/notifications', collectNotifierBody());
        showToast('Notification settings saved.');
        await loadNotificationSettings();
    } catch (e) {
        showError(e.message);
    }
}

// Verify SMTP credentials by actually sending mail. Saves first so the
// test exercises exactly what real alerts will use - and so a masked
// password left untouched in the form resolves to the stored secret.
async function sendTestEmail() {
    const btn = document.getElementById('notif-test-email-btn');
    const status = document.getElementById('notif-test-email-status');
    const setStatus = (msg, tone) => {
        if (status) {
            status.textContent = msg;
            status.className = `text-xs ${tone}`;
        }
    };
    if (btn) {
        btn.disabled = true;
        btn.classList.add('opacity-50', 'cursor-not-allowed');
    }
    setStatus('Saving settings and sending…', 'text-gray-400');
    try {
        await apiRequest('PUT', '/settings/notifications', collectNotifierBody());
        const res = await apiRequest('POST', '/settings/notifications/test-email');
        const to = res?.recipient || 'the To address';
        setStatus(`Sent to ${to} — check the inbox (and the spam folder).`, 'text-green-400');
        showToast('Test email sent.');
        await loadNotificationSettings();
    } catch (e) {
        setStatus(e.message, 'text-red-400');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.classList.remove('opacity-50', 'cursor-not-allowed');
        }
    }
}

// Settings → App: runtime app options (DB-backed, env fallback).

// input id ↔ API key for every editable app option.
const APP_SETTING_FIELDS = [
    ['app-price-check-interval', 'price_check_interval'],
    ['app-stock-retention-days', 'stock_event_retention_days'],
    ['app-stock-max-rows', 'stock_event_max_rows'],
    ['app-cache-ttl', 'cache_ttl'],
    ['app-log-max-bytes', 'log_file_max_bytes'],
    ['app-log-backup-count', 'log_backup_count'],
    ['app-log-buffer-size', 'log_buffer_size'],
    ['app-ui-alert-autohide', 'ui_alert_autohide_ms'],
    ['app-ui-orders-days', 'ui_orders_days'],
    ['app-ui-orders-limit', 'ui_orders_limit'],
    ['app-ui-logs-limit', 'ui_logs_limit'],
    ['app-ui-region-cap', 'ui_region_feed_cap'],
    ['app-ui-recent-alerts', 'ui_recent_alerts_shown'],
];

async function loadAppSettings() {
    try {
        const data = await apiRequest('GET', '/settings/app');
        const s = data.settings || {};
        for (const [id, key] of APP_SETTING_FIELDS) {
            const input = document.getElementById(id);
            if (input && s[key] != null) input.value = s[key];
        }
        document.getElementById('app-use-cache').checked = !!s.use_cache;
        document.getElementById('app-log-level').value = s.log_level || 'INFO';
        const env = data.env || {};
        document.getElementById('app-env-bind').value = `${env.host || ''}:${env.port || ''}`;
        document.getElementById('app-env-cors').value = (env.cors_origins || []).join(', ') || '(none)';
        document.getElementById('app-env-db').value = env.db_path || '';
        document.getElementById('app-env-logfile').value = env.log_file || '';
        document.getElementById('app-settings-status').textContent = '';
    } catch (e) {
        showError(e.message);
    }
}

async function loadUiPrefs() {
    // Refresh state.uiPrefs from the ui_* app settings. Silent fallback to
    // the built-in defaults — a failed load must not break page init.
    try {
        const data = await apiRequest('GET', '/settings/app');
        const s = data?.settings || {};
        state.uiPrefs = {
            alertAutohideMs: s.ui_alert_autohide_ms ?? state.uiPrefs.alertAutohideMs,
            ordersDays: s.ui_orders_days ?? state.uiPrefs.ordersDays,
            ordersLimit: s.ui_orders_limit ?? state.uiPrefs.ordersLimit,
            logsLimit: s.ui_logs_limit ?? state.uiPrefs.logsLimit,
            regionFeedCap: s.ui_region_feed_cap ?? state.uiPrefs.regionFeedCap,
            recentAlertsShown: s.ui_recent_alerts_shown ?? state.uiPrefs.recentAlertsShown,
        };
    } catch (e) {
        console.warn('Failed to load UI preferences (using defaults):', e);
    }
}

async function saveAppSettings() {
    const body = {
        use_cache: document.getElementById('app-use-cache').checked,
        log_level: document.getElementById('app-log-level').value,
    };
    for (const [id, key] of APP_SETTING_FIELDS) {
        body[key] = parseInt(document.getElementById(id).value, 10);
        if (isNaN(body[key])) {
            showError(`Enter a number for ${key.replaceAll('_', ' ')}`);
            return;
        }
    }
    try {
        const resp = await apiRequest('PUT', '/settings/app', body);
        const applied = resp?.applied || [];
        showToast(applied.length
            ? `App options saved — ${applied.join(', ')}`
            : 'App options saved.', 4000);
        const status = document.getElementById('app-settings-status');
        if (status) status.textContent = applied.length ? `Applied: ${applied.join('; ')}` : 'Saved.';
        await loadAppSettings();
        await loadUiPrefs();
    } catch (e) {
        showError(e.message);
    }
}

async function loadAccountInfo() {
    const container = document.getElementById('account-info');
    if (!container) return;
    container.innerHTML = '';
    try {
        const me = await apiRequest('GET', '/account/me');
        if (!me) {
            container.appendChild(el('p', { class: 'text-red-400 text-sm', text: 'Could not load account info.' }));
            return;
        }
        const fields = [
            ['Name', `${me.firstname || ''} ${me.name || ''}`.trim()],
            ['Nichandle', me.nichandle],
            ['Email', me.email],
            ['Country', me.country],
            ['Currency', me.currency?.code || me.currency],
            ['State', me.state],
            ['Legal form', me.legalform],
        ].filter(([, v]) => v);
        // Default the display currency (selector) to the account's invoiced
        // currency. Prices default to OVH's native currency regardless (see
        // effectiveDisplayCurrency / the "Convert pricing" checkbox); the
        // selector just records the user's preferred currency for when they
        // opt into FX conversion.
        const billingCurrency = me.currency?.code || (typeof me.currency === 'string' ? me.currency : null);
        if (billingCurrency && SUPPORTED_CURRENCIES.includes(billingCurrency) && !state._currencyUserSet) {
            state.displayCurrency = billingCurrency;
            const sel = document.getElementById('currency-select');
            if (sel) sel.value = billingCurrency;
        }
        updatePriceModeVisibility();
        updateCurrencyStatus();
        const grid = el('div', { class: 'grid grid-cols-1 sm:grid-cols-2 gap-2' });
        for (const [label, value] of fields) {
            grid.appendChild(el('div', { class: 'bg-gray-700 rounded p-2' }, [
                el('p', { class: 'text-gray-500 text-xs', text: label }),
                el('p', { class: 'text-gray-200 text-sm', text: String(value) }),
            ]));
        }
        container.appendChild(grid);
    } catch (e) {
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

async function loadBillingInvoices() {
    const container = document.getElementById('billing-invoices');
    if (!container) return;
    const gen = state._switchGen;
    try {
        const data = await apiRequest('GET', '/account/bills?limit=20&months=6');
        if (gen !== state._switchGen) return;
        const bills = data?.bills || [];
        container.innerHTML = '';
        if (bills.length === 0) {
            container.appendChild(el('p', { class: 'text-gray-500', text: 'No invoices in the last 6 months.' }));
            return;
        }
        bills.forEach(b => {
            const date = b.date ? new Date(b.date).toLocaleDateString() : '—';
            const row = el('div', { class: 'bg-gray-700/50 rounded p-2 flex justify-between items-center gap-2' }, [
                el('div', {}, [
                    el('span', { class: 'text-gray-200 font-mono', text: b.bill_id || '?' }),
                    el('p', { class: 'text-gray-500 text-xs', text: date }),
                ]),
                el('div', { class: 'flex items-center gap-3' }, [
                    el('span', { class: 'text-green-400 tabular-nums', text: b.price_text || '' }),
                    b.pdf_url ? el('a', {
                        href: b.pdf_url, target: '_blank', rel: 'noopener',
                        class: 'text-blue-400 hover:text-blue-300 text-xs', text: 'PDF',
                    }) : null,
                ].filter(Boolean)),
            ]);
            container.appendChild(row);
        });
    } catch (e) {
        if (gen !== state._switchGen) return;
        container.innerHTML = '';
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

async function loadPaymentMethods() {
    const container = document.getElementById('payment-methods');
    if (!container) return;
    container.innerHTML = '';
    // switchAccount calls this fire-and-forget, so guard internally: a
    // stale response from the previous account must not render.
    const gen = state._switchGen;
    try {
        const data = await apiRequest('GET', '/account/payment-methods');
        if (gen !== state._switchGen) return;
        const methods = data?.payment_methods || [];
        if (methods.length === 0) {
            container.appendChild(el('p', { class: 'text-yellow-400 text-sm', text: 'No payment methods found. Add one in the OVH Manager.' }));
            return;
        }
        for (const m of methods) {
            const isDefault = m.default;
            const label = m.description || m.label || m.paymentMethodType || 'Unknown';
            const status = m.status || 'unknown';
            const card = el('div', {
                class: `rounded p-3 ${isDefault ? 'bg-green-900/30 border border-green-700' : 'bg-gray-700'}`
            }, [
                el('div', { class: 'flex justify-between items-center' }, [
                    el('span', { class: 'text-gray-200 font-bold', text: label }),
                    isDefault ? el('span', { class: 'text-green-500 text-xs font-bold', text: 'DEFAULT' }) : null,
                ]),
                el('p', { class: 'text-gray-400 text-xs mt-1', text: `Status: ${status}` }),
            ]);
            container.appendChild(card);
        }
    } catch (e) {
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

async function loadCheckoutDefaults() {
    // Guarded like loadPaymentMethods: called fire-and-forget on switch.
    const gen = state._switchGen;
    try {
        const defaults = await apiRequest('GET', '/account/checkout-defaults');
        if (gen !== state._switchGen) return;
        if (!defaults) return;
        state.checkoutDefaults = defaults;
        document.getElementById('default-duration').value = defaults.duration || 'P1M';
        document.getElementById('default-auto-pay').checked = defaults.auto_pay || false;
        document.getElementById('default-waive').checked = defaults.waive_retractation !== false;
        if (defaults.max_price) {
            document.getElementById('default-max-price').value = (defaults.max_price / 100000000).toFixed(2);
        }
    } catch (e) {
        console.error('Failed to load checkout defaults:', e);
        showToast(`Failed to load checkout defaults: ${e.message}`, 4000);
    }
}

async function saveCheckoutDefaults(e) {
    e.preventDefault();
    const maxPriceRaw = document.getElementById('default-max-price').value.trim();
    const maxPrice = maxPriceRaw ? Math.round(parseFloat(maxPriceRaw) * 100000000) : null;
    const body = {
        auto_pay: document.getElementById('default-auto-pay').checked,
        waive_retractation: document.getElementById('default-waive').checked,
        duration: document.getElementById('default-duration').value,
        max_price: maxPrice,
    };
    try {
        await apiRequest('PUT', '/account/checkout-defaults', body);
        state.checkoutDefaults = body;
        showToast('Checkout defaults saved.');
    } catch (e) {
        showError(e.message);
    }
}

// Alerts

async function loadAlerts() {
    try {
        state.alerts = await apiRequest('GET', '/alerts') || [];
        renderAlertsList();
        renderMonitoredList();
        renderSniperAlertSelect();
        renderArmedSniper();
    } catch (e) {
        console.error('Failed to load alerts:', e);
        showToast(`Failed to load alerts: ${e.message}`, 4000);
    }
}

function renderAlertsList() {
    const container = document.getElementById('alerts-list');
    container.innerHTML = '';
    if (state.alerts.length === 0) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No servers being monitored. Add servers below.' }));
        return;
    }
    state.alerts.forEach(alert => {
        const name = el('span', {
            class: `font-bold ${alert.enabled ? 'text-blue-400' : 'text-gray-500'}`,
            text: alert.plan_code,
        });
        const pattern = el('span', { class: 'text-gray-400 ml-2 text-sm', text: alert.fqn_pattern });
        const left = el('div', {}, [name, pattern]);
        const toggleBtn = el('button', {
            class: `text-xs px-2 py-1 rounded ${alert.enabled ? 'bg-yellow-700 hover:bg-yellow-600 text-yellow-100' : 'bg-green-700 hover:bg-green-600 text-green-100'}`,
            title: alert.enabled ? 'Pause alert' : 'Resume alert',
            'data-id': alert.id,
        }, [
            el('span', { text: alert.enabled ? 'Pause' : 'Resume' }),
        ]);
        toggleBtn.addEventListener('click', async () => {
            try {
                const resp = await apiRequest('PUT', `/alerts/${encodeURIComponent(alert.id)}/${alert.enabled ? 'disable' : 'enable'}`);
                // Pausing an alert disarms its sniper (a paused alert is
                // never polled) — tell the user so it isn't a surprise.
                if (resp && resp.sniper_disarmed) {
                    showToast('Sniper disarmed: paused alerts never fire', 4000);
                    await loadSniperStatus();
                }
                await loadAlerts();
            } catch (e) {
                showError(e.message);
            }
        });
        const delBtn = el('button', {
            class: 'text-red-400 hover:text-red-300 delete-alert-btn ml-2',
            'data-id': alert.id,
            text: '\u00D7',
            'aria-label': `Delete alert for ${alert.plan_code}`
        });
        delBtn.addEventListener('click', async () => {
            await deleteAlert(alert.id);
        });
        const row = el('div', {
            class: `rounded p-2 flex justify-between items-center ${alert.enabled ? 'bg-gray-700' : 'bg-gray-800 opacity-60'}`,
        }, [left, el('div', { class: 'flex items-center' }, [toggleBtn, delBtn])]);
        container.appendChild(row);
    });
}

// Glob match mirroring the backend's fnmatch (fqn_pattern supports * and ?).
function fqnMatchesPattern(fqn, pattern) {
    if (!pattern || pattern === '*') return true;
    const re = new RegExp('^' + pattern
        .replace(/[.+^${}()|[\]\\]/g, '\\$&')  // escape regex specials
        .replace(/\*/g, '.*')
        .replace(/\?/g, '.') + '$', 'i');
    return re.test(fqn);
}

function renderMonitoredList() {
    const container = document.getElementById('monitored-list');
    container.innerHTML = '';
    if (state.alerts.length === 0) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No servers being monitored' }));
        return;
    }
    state.alerts.forEach(alert => {
        const stock = state.currentStock[alert.plan_code];
        // Green only when a config matching THIS alert's pattern is in stock,
        // not merely any config of the plan (misleading for scoped patterns).
        const available = !!(stock && stock.some(s => fqnMatchesPattern(s.fqn, alert.fqn_pattern)));
        const label = el('span', { class: 'font-bold', text: alert.plan_code });
        const dot = el('span', {
            class: `w-3 h-3 rounded-full ${available ? 'bg-green-500' : 'bg-red-500'}`,
            'aria-hidden': 'true'
        });
        const row = el('div', { class: 'flex justify-between items-center p-2 bg-gray-700 rounded' }, [label, dot]);
        container.appendChild(row);
    });
}

function addToRecentAlerts(planCode, fqns) {
    const alert = {
        planCode,
        fqns,
        timestamp: new Date().toISOString()
    };
    state.recentAlerts.unshift(alert);
    if (state.recentAlerts.length > 10) {
        state.recentAlerts.pop();
    }
    renderRecentAlerts();
}

function renderRecentAlerts() {
    const container = document.getElementById('recent-alerts');
    container.innerHTML = '';
    if (state.recentAlerts.length === 0) {
        container.appendChild(el('p', { class: 'text-gray-500', text: 'No recent alerts' }));
        return;
    }
    state.recentAlerts.slice(0, state.uiPrefs.recentAlertsShown).forEach(alert => {
        const time = new Date(alert.timestamp).toLocaleTimeString();
        const code = el('span', { class: 'text-red-400 font-bold', text: alert.planCode });
        const t = el('span', { class: 'text-gray-400 ml-2 text-xs', text: time });
        const fqns = el('p', { class: 'text-sm', text: (alert.fqns || []).join(', ') });
        const row = el('div', { class: 'bg-red-900/30 border border-red-700 rounded p-2' }, [code, t, fqns]);
        container.appendChild(row);
    });
}

// Region restock ticker

async function loadRegionWatch() {
    // Guarded like other loaders: don't render a stale account's feed.
    const gen = state._switchGen;
    try {
        const resp = await apiRequest('GET', '/monitor/region-watch');
        if (gen !== state._switchGen) return;
        state.regionTicker = !!(resp && resp.enabled);
        const toggle = document.getElementById('region-ticker-toggle');
        if (toggle) toggle.checked = state.regionTicker;
        state.regionFeed = [];
        if (state.regionTicker) {
            const data = await apiRequest('GET', '/insights/region-activity?hours=24&limit=100&event_type=available');
            if (gen !== state._switchGen) return;
            state.regionFeed = (data.events || []).map(e => ({
                time: e.timestamp,
                planCode: e.plan_code,
                fqns: [e.fqn],
            }));
        }
        renderRegionFeed();
    } catch (e) {
        console.error('Failed to load region watch:', e);
    }
}

async function setRegionTicker(enabled) {
    try {
        const resp = await apiRequest('PUT', '/monitor/region-watch', { enabled });
        state.regionTicker = !!(resp && resp.enabled);
        state.regionFeed = [];
        renderRegionFeed();
        if (state.regionTicker) {
            showToast('Region ticker on — polling clamped to ≥3s', 3500);
        }
    } catch (e) {
        showError(e.message);
        const toggle = document.getElementById('region-ticker-toggle');
        if (toggle) toggle.checked = state.regionTicker;
    }
}

function addRegionRestocks(event) {
    // One SSE region_restock event carries up to 50 plans.
    for (const r of (event.restocks || [])) {
        state.regionFeed.unshift({
            time: event.timestamp,
            planCode: r.plan_code,
            fqns: r.fqns || [],
        });
    }
    if (state.regionFeed.length > state.uiPrefs.regionFeedCap) {
        state.regionFeed.length = state.uiPrefs.regionFeedCap;
    }
    renderRegionFeed();
}

function renderRegionFeed() {
    const container = document.getElementById('region-restocks');
    if (!container) return;
    container.innerHTML = '';
    if (!state.regionTicker) {
        container.appendChild(el('p', { class: 'text-gray-500', text: 'Ticker disabled' }));
        return;
    }
    if (state.regionFeed.length === 0) {
        container.appendChild(el('p', { class: 'text-gray-500', text: 'No restocks seen yet' }));
        return;
    }
    state.regionFeed.forEach(item => {
        const time = new Date(item.time).toLocaleTimeString();
        const row = el('div', { class: 'bg-gray-700/50 rounded p-2 flex justify-between items-center gap-2' }, [
            el('div', {}, [
                el('span', { class: 'text-green-400 font-bold', text: item.planCode }),
                el('p', { class: 'text-gray-400 text-xs truncate', text: (item.fqns || []).join(', ') }),
            ]),
            el('span', { class: 'text-gray-500 text-xs whitespace-nowrap', text: time }),
        ]);
        container.appendChild(row);
    });
}

async function addAlert(planCode, fqnPattern) {
    try {
        await apiRequest('POST', '/alerts', {
            plan_code: planCode,
            fqn_pattern: fqnPattern || '*'
        });
        await loadAlerts();
    } catch (e) {
        showError(e.message);
    }
}

async function deleteAlert(alertId) {
    try {
        await apiRequest('DELETE', `/alerts/${encodeURIComponent(alertId)}`);
        await loadAlerts();
        // Deleting an alert disarms its sniper server-side; refresh the
        // armed-sniper panel so it doesn't keep showing a dead entry.
        await loadSniperStatus();
    } catch (e) {
        showError(e.message);
    }
}

async function loadPollInterval() {
    try {
        const status = await apiRequest('GET', '/monitor/status');
        if (status && status.poll_interval) {
            const sel = document.getElementById('poll-interval');
            const val = String(status.poll_interval);
            // A stored interval that isn't one of the preset options (e.g. set
            // via the API) would leave the select blank — inject it so the
            // current value is always shown.
            if (sel && !Array.from(sel.options).some(o => o.value === val)) {
                sel.appendChild(el('option', { value: val, text: `${val}s` }));
            }
            if (sel) sel.value = val;
        }
    } catch (e) {
        // ignore - keep default
    }
}

async function setPollInterval(seconds) {
    try {
        await apiRequest('PUT', '/monitor/poll-interval', { poll_interval: seconds });
    } catch (e) {
        showError(e.message);
    }
}

// SSE monitoring

function startMonitoring() {
    if (state.eventSource) {
        state.eventSource.close();
    }
    if (state.reconnectTimer) {
        clearTimeout(state.reconnectTimer);
        state.reconnectTimer = null;
    }

    state.monitoring = true;
    const btn = document.getElementById('toggle-monitor-btn');
    btn.textContent = 'Stop Monitor';
    btn.className = 'bg-red-600 hover:bg-red-700 px-4 py-2 rounded-lg';

    unlockAudio();

    state.eventSource = new EventSource(`${API_BASE}/monitor/stream`);

    state.eventSource.onopen = () => {
        state.reconnectDelay = RECONNECT_BASE_MS;
        updateConnectionStatus(true);
    };

    state.eventSource.onmessage = async (event) => {
        try {
            const data = JSON.parse(event.data);

            if (data.type === 'stock_update') {
                data.changes.forEach(change => {
                    // Always sync the monitored-list dot to the latest snapshot,
                    // even when nothing newly became available - otherwise a
                    // plan that sells out never reverts from green to red.
                    state.currentStock[change.plan_code] = change.currently_available.map(fqn => ({ fqn }));

                    if (change.newly_available.length > 0) {
                        showStockAlert(change.plan_code, change.newly_available);
                        addToRecentAlerts(change.plan_code, change.newly_available);

                        const soundEnabled = document.getElementById('sound-toggle').checked;
                        if (soundEnabled) {
                            playAlertSound();
                        }

                        showBrowserNotification(change.plan_code, change.newly_available);
                    }
                });

                renderMonitoredList();
            } else if (data.type === 'region_restock') {
                addRegionRestocks(data);
            }
        } catch (e) {
            console.error('Failed to parse SSE message:', e);
        }
    };

    state.eventSource.onerror = () => {
        updateConnectionStatus(false);
        if (state.eventSource) {
            state.eventSource.close();
            state.eventSource = null;
        }
        if (state.monitoring && !state.reconnectTimer) {
            // Exponential backoff so a persistently failing endpoint isn't
            // hammered every few seconds; reset on successful open/stop.
            const delay = state.reconnectDelay;
            state.reconnectDelay = Math.min(delay * 2, RECONNECT_MAX_MS);
            state.reconnectTimer = setTimeout(() => {
                state.reconnectTimer = null;
                if (state.monitoring) {
                    startMonitoring();
                }
            }, delay);
        }
    };
}

function stopMonitoring() {
    state.monitoring = false;
    state.reconnectDelay = RECONNECT_BASE_MS;
    if (state.reconnectTimer) {
        clearTimeout(state.reconnectTimer);
        state.reconnectTimer = null;
    }
    if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
    }
    const btn = document.getElementById('toggle-monitor-btn');
    btn.textContent = 'Start Monitor';
    btn.className = 'bg-green-600 hover:bg-green-700 px-4 py-2 rounded-lg';
    updateConnectionStatus(false);
}

function showStockAlert(planCode, fqns) {
    const panel = document.getElementById('stock-alerts-panel');
    document.getElementById('alert-details').textContent =
        `${fqns.length} config(s) now available for ${planCode}: ${fqns.join(', ')}`;
    panel.classList.remove('hidden');

    // Remember the alert so "Use this config" can apply it explicitly.
    state.lastStockAlert = { planCode, fqn: fqns[0] };
    // Only autofill the rush form when the user hasn't typed into it —
    // values we set are tagged autofilled (the tag is cleared by the
    // fields' input listeners), so consecutive alerts may overwrite each
    // other but never the user's own edits mid-typing.
    const planEl = document.getElementById('rush-plan-code');
    const fqnEl = document.getElementById('rush-fqn');
    const untouched = (el) => !el.value || el.dataset.autofilled === '1';
    if (untouched(planEl) && untouched(fqnEl)) {
        applyAlertConfigToRushForm();
    } else {
        document.getElementById('use-alert-config-btn')?.classList.remove('hidden');
    }

    if (alertPanelTimer) {
        clearTimeout(alertPanelTimer);
        alertPanelTimer = null;
    }
    // 0 = keep the panel open until the user dismisses it.
    if (state.uiPrefs.alertAutohideMs > 0) {
        alertPanelTimer = setTimeout(() => {
            panel.classList.add('hidden');
            alertPanelTimer = null;
        }, state.uiPrefs.alertAutohideMs);
    }
}

function applyAlertConfigToRushForm() {
    if (!state.lastStockAlert) return;
    const planEl = document.getElementById('rush-plan-code');
    const fqnEl = document.getElementById('rush-fqn');
    planEl.value = state.lastStockAlert.planCode;
    fqnEl.value = state.lastStockAlert.fqn;
    updateRushConfigOptionsForPlanCode(state.lastStockAlert.planCode);
    planEl.dataset.autofilled = '1';
    fqnEl.dataset.autofilled = '1';
    document.getElementById('use-alert-config-btn')?.classList.add('hidden');
}

async function requestNotificationPermission() {
    if ('Notification' in window && Notification.permission === 'default') {
        try {
            await Notification.requestPermission();
        } catch (e) {
            // some browsers reject non-gesture requests
        }
    }
}

function showBrowserNotification(planCode, fqns) {
    if ('Notification' in window && Notification.permission === 'granted') {
        try {
            new Notification('OVH Stock Alert!', {
                body: `${planCode} now available: ${fqns[0]}`,
                tag: planCode
            });
        } catch (e) {
            console.warn('Notification failed:', e);
        }
    }
}

// Rush order

function getSelectedDatacenters() {
    return Array.from(document.querySelectorAll('.rush-dc:checked')).map(cb => cb.value);
}

async function rushOrder(e) {
    e.preventDefault();
    showLoading();
    hideError();

    try {
        const planCode = document.getElementById('rush-plan-code').value.trim();
        const fqn = document.getElementById('rush-fqn').value.trim();
        const ramAddon = document.getElementById('rush-ram').value.trim();
        const storageAddon = document.getElementById('rush-storage').value.trim();
        const bandwidthAddon = document.getElementById('rush-bandwidth').value.trim();
        const datacenters = getSelectedDatacenters();
        const region = document.getElementById('rush-region').value;
        const osValue = document.getElementById('rush-os').value;
        const duration = document.getElementById('rush-duration').value;
        const autoPay = document.getElementById('rush-auto-pay').checked;
        const waive = document.getElementById('rush-waive').checked;

        if (!planCode || !fqn) {
            throw new Error('Plan code and FQN are required');
        }

        const result = await apiRequest('POST', '/checkout/rush', {
            plan_code: planCode,
            fqn: fqn,
            ram: ramAddon || null,
            storage: storageAddon || null,
            bandwidth: bandwidthAddon || null,
            datacenters: datacenters,
            region: region,
            os: osValue,
            duration: duration,
            auto_pay: autoPay,
            waive_retractation: waive,
            max_price: state.checkoutDefaults?.max_price ?? null,
            arm_if_oos: true,
        });

        // When the requested config is out of stock the backend arms the
        // sniper instead of firing a doomed order. Refresh the alerts,
        // profiles, and sniper panels so the armed state is visible, and
        // start the monitor so the user sees the stock-alert banner the
        // moment OVH reports it back in stock. The background poller
        // auto-orders regardless of any browser connection.
        if (result.status === 'armed') {
            showToast(result.message || `Sniper armed for ${result.plan_code}. Will auto-order when back in stock.`, 6000);
            await loadAlerts();
            await loadProfiles();
            await loadSniperStatus();
            if (!state.monitoring) {
                startMonitoring();
            }
            return;
        }

        document.getElementById('order-id').textContent = `Order ID: ${result.orderId || 'N/A'}`;
        const orderUrl = document.getElementById('order-url');
        orderUrl.href = result.url || '#';
        showView('order-complete');

        document.getElementById('stock-alerts-panel').classList.add('hidden');
        stopMonitoring();
        await loadOrders();

    } catch (e) {
        showError(e.message);
    } finally {
        hideLoading();
    }
}

// Audio

function initAudio() {
    try {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();

        fetch(ALERT_SOUND_DATA)
            .then(response => response.arrayBuffer())
            .then(arrayBuffer => audioContext.decodeAudioData(arrayBuffer))
            .then(audioBuffer => {
                alertBuffer = audioBuffer;
            })
            .catch(err => {
                console.warn('Could not load alert sound:', err);
            });
    } catch (e) {
        console.warn('Web Audio API not supported:', e);
    }
}

function unlockAudio() {
    if (audioContext && audioContext.state === 'suspended') {
        audioContext.resume().catch(() => {});
    }
}

function playAlertSound() {
    if (!audioContext || !alertBuffer) {
        console.warn('Audio not initialized');
        return;
    }

    try {
        if (audioContext.state === 'suspended') {
            audioContext.resume();
        }

        const source = audioContext.createBufferSource();
        source.buffer = alertBuffer;
        source.connect(audioContext.destination);
        source.start(0);

        setTimeout(() => {
            try { source.stop(); } catch (e) {}
        }, 500);
    } catch (e) {
        console.error('Failed to play sound:', e);
    }
}

// Credentials / accounts

// Point an OVHcloud Manager link at the manager URL for a region.
function updateManagerLink(region, linkEl) {
    if (!linkEl) return;
    const regionInfo = OVH_REGIONS[region] || OVH_REGIONS['ovh-eu'];
    linkEl.href = regionInfo.managerUrl;
    linkEl.textContent = `Open ${regionInfo.name} OVHcloud Manager`;
}

// Setup-wizard region select drives its manager link AND the rush-order
// region (the first account is the one you'll order with).
function updateCredentialsView(region) {
    updateManagerLink(region, document.getElementById('setup-manager-link'));
    const regionInfo = OVH_REGIONS[region] || OVH_REGIONS['ovh-eu'];
    const rushRegion = document.getElementById('rush-region');
    if (rushRegion) {
        rushRegion.value = regionInfo.rushRegion;
    }
}

// Shared save+test core for both the setup wizard and the account editor.
// `fields` = {label, endpoint, applicationKey, applicationSecret, consumerKey}.
// Returns {id, ok} — ok is the credential-test result — or null if
// validation/save failed.
async function submitAccount(fields, editingId, resultElId) {
    const { endpoint, applicationKey, applicationSecret, consumerKey } = fields;
    const label = fields.label || endpoint;
    if (!applicationKey || !consumerKey) {
        showTestResult(resultElId, 'error', 'Application key and consumer key are required.');
        return null;
    }
    if (!editingId && !applicationSecret) {
        showTestResult(resultElId, 'error', 'Application secret is required for a new account.');
        return null;
    }
    showTestResult(resultElId, 'loading', 'Verifying credentials with OVH…');
    const body = {
        label, endpoint,
        application_key: applicationKey,
        application_secret: applicationSecret,
        consumer_key: consumerKey,
    };
    // The backend verifies the credentials against OVH BEFORE saving and
    // rejects with the OVH error otherwise — a throw here means nothing
    // was saved (the callers' catch shows the message in the form).
    let saved;
    if (editingId) {
        saved = await apiRequest('PUT', `/accounts/${editingId}`, body);
    } else {
        saved = await apiRequest('POST', '/accounts', body);
    }
    await loadAccounts();
    showTestResult(resultElId, 'success',
        `Connected as ${saved.nichandle || 'unknown'} — account ${editingId ? 'updated' : 'saved'}.`);
    return { id: saved.id, ok: true };
}

// First-run wizard: save the first account, activate it, go to the monitor.
async function saveSetupAccount() {
    const endpoint = document.getElementById('setup-region').value;
    const fields = {
        endpoint,
        label: document.getElementById('setup-label').value.trim(),
        applicationKey: document.getElementById('setup-app-key').value.trim(),
        applicationSecret: document.getElementById('setup-app-secret').value.trim(),
        consumerKey: document.getElementById('setup-consumer-key').value.trim(),
    };
    try {
        const saved = await submitAccount(fields, null, 'setup-test-result');
        if (!saved) return;
        // Reaching here means the credentials verified AND the account
        // saved (the backend hard-blocks otherwise). Proceed to the monitor.
        state.activeAccountId = saved.id;
        state.endpoint = endpoint;
        state.configured = true;
        setTimeout(async () => {
            document.getElementById('settings-btn').classList.remove('hidden');
            renderAccountSelect();
            await loadAlerts();
            await loadCatalog();
            await loadPollInterval();
            await loadProfiles();
            await loadOrders();
            await loadSniperStatus();
            await loadRegionWatch();
            showView('monitor');
        }, 1200);
    } catch (e) {
        showTestResult('setup-test-result', 'error', e.message);
    }
}

// Accounts page: add or edit an account without changing the active one.
async function saveManagedAccount() {
    const editingId = state.editingAccountId;
    const fields = {
        endpoint: document.getElementById('acct-region').value,
        label: document.getElementById('acct-label').value.trim(),
        applicationKey: document.getElementById('acct-app-key').value.trim(),
        applicationSecret: document.getElementById('acct-app-secret').value.trim(),
        consumerKey: document.getElementById('acct-consumer-key').value.trim(),
    };
    try {
        const saved = await submitAccount(fields, editingId, 'acct-test-result');
        if (!saved) return;
        // Verified + saved (the backend hard-blocks bad credentials, so
        // there is no "saved but broken" half-state anymore).
        renderAccountList();
        renderAccountSelect();
        showToast(editingId ? 'Account updated.' : 'Account added.');
        closeAccountEditor();
    } catch (e) {
        // Verification/save failure: nothing was saved — keep the editor
        // open with OVH's error so the user can fix the keys.
        showTestResult('acct-test-result', 'error', e.message);
    }
}

async function deleteAccount() {
    const editingId = state.editingAccountId;
    if (!editingId) return;
    if (!confirm('Delete this account? Its alerts and profiles remain but become unscoped.')) {
        return;
    }
    const wasActive = editingId === state.activeAccountId;
    try {
        await apiRequest('DELETE', `/accounts/${editingId}`);
        await loadAccounts();
        closeAccountEditor();
        // Active account may have changed (fallback); refresh health-derived state.
        await checkHealth();
        renderAccountSelect();
        if (!state.accounts.length) {
            // No accounts left → back to the first-run setup wizard.
            state.configured = false;
            document.getElementById('settings-btn').classList.add('hidden');
            showView('setup');
            return;
        }
        renderAccountList();
        // If the deleted account was active, the backend fell back to another
        // account (and reloaded its monitor). Re-sync the account-scoped data
        // so the monitor/catalog/orders don't show the deleted account's state.
        if (wasActive) {
            state.endpoint = state.accounts.find(a => a.id === state.activeAccountId)?.endpoint || state.endpoint;
            state._currencyUserSet = false;
            await loadFxRates();
            await loadAccountInfo();
            await loadAlerts();
            await loadCatalog();
            await loadProfiles();
            await loadOrders();
            await loadSniperStatus();
        }
        showToast('Account deleted.');
    } catch (e) {
        showTestResult('acct-test-result', 'error', e.message);
    }
}

function showTestResult(elId, type, message) {
    const div = document.getElementById(elId);
    if (!div) return;
    if (type === 'success') {
        div.className = 'rounded p-3 text-sm bg-green-900/50 border border-green-700 text-green-300';
    } else if (type === 'error') {
        div.className = 'rounded p-3 text-sm bg-red-900/50 border border-red-700 text-red-300';
    } else {
        div.className = 'rounded p-3 text-sm bg-blue-900/50 border border-blue-700 text-blue-300';
    }
    div.textContent = message;
}

// ----- Saved checkout profiles -----

async function loadProfiles() {
    try {
        const profiles = await apiRequest('GET', '/profiles') || [];
        state.profiles = profiles;
        renderProfileSelect();
        renderSniperProfileSelect();
        renderArmedSniper();
    } catch (e) {
        console.error('Failed to load profiles:', e);
        showToast(`Failed to load profiles: ${e.message}`, 4000);
    }
}

function renderProfileSelect() {
    const select = document.getElementById('profile-select');
    if (!select) return;
    select.innerHTML = '';
    select.appendChild(el('option', { value: '', text: 'Select profile...' }));
    (state.profiles || []).forEach(p => {
        select.appendChild(el('option', { value: p.id, text: p.name }));
    });
}

function renderSniperProfileSelect() {
    const select = document.getElementById('sniper-profile-select');
    if (!select) return;
    const current = select.value;
    select.innerHTML = '';
    select.appendChild(el('option', { value: '', text: 'Select profile...' }));
    (state.profiles || []).forEach(p => {
        select.appendChild(el('option', { value: p.id, text: p.name }));
    });
    if (current) select.value = current;
}

// Fallback lists for the rush form when the selected plan (or its catalog
// configurations) is unknown — mirrors the old hardcoded index.html lists.
const DEFAULT_RUSH_DATACENTERS = [
    'gra', 'sbg', 'rbx', 'bhs', 'fra', 'waw', 'lon', 'sgp', 'syd', 'eri', 'hil', 'vin',
];
const DEFAULT_RUSH_OS = [
    'none_64.en', 'debian_64', 'ubuntuserver_64', 'proxmox_64', 'freebsd_64', 'windows_2022_64',
];

function _planConfigValues(plan, name) {
    const cfg = (plan?.configurations || []).find(c => c.name === name);
    return cfg?.values?.length ? cfg.values : null;
}

// Rebuild the rush form's datacenter checkboxes and OS dropdown from the
// selected plan's real catalog configurations, so new OVH DCs/OSes appear
// without a code change. Preserves current selections where still valid.
// MUST run before values are programmatically set (see loadProfileIntoForm).
function updateRushConfigOptions(plan) {
    const dcValues = _planConfigValues(plan, 'dedicated_datacenter') || DEFAULT_RUSH_DATACENTERS;
    const osValues = _planConfigValues(plan, 'dedicated_os') || DEFAULT_RUSH_OS;

    const dcContainer = document.getElementById('rush-datacenters');
    if (dcContainer) {
        const checked = new Set(getSelectedDatacenters());
        dcContainer.innerHTML = '';
        dcValues.forEach(dc => {
            const cb = el('input', { type: 'checkbox', value: dc, class: 'rush-dc' });
            cb.checked = checked.has(dc);
            const label = el('label', { class: 'flex items-center gap-1' }, [cb]);
            label.appendChild(document.createTextNode(` ${dc.toUpperCase()}`));
            dcContainer.appendChild(label);
        });
    }

    const osSel = document.getElementById('rush-os');
    if (osSel) {
        const current = osSel.value;
        osSel.innerHTML = '';
        osValues.forEach(v => {
            osSel.appendChild(el('option', { value: v, text: humanizeOs(v) }));
        });
        if (osValues.includes(current)) osSel.value = current;
        else if (osValues.includes('none_64.en')) osSel.value = 'none_64.en';
    }
}

function updateRushConfigOptionsForPlanCode(planCode) {
    const plan = (state.plans || []).find(p => p.planCode === planCode) || null;
    updateRushConfigOptions(plan);
}

async function loadProfileIntoForm(profileId) {
    if (!profileId) return;
    const profile = state.profiles?.find(p => p.id === profileId);
    if (!profile) return;
    // Loading a profile enters edit mode: saving with the same name
    // updates it in place instead of creating a duplicate.
    state.editingProfileId = profile.id;
    document.getElementById('profile-name').value = profile.name || '';
    // Rebuild DC/OS options for the profile's plan BEFORE assigning the
    // stored values, or they'd be clobbered by the rebuild.
    updateRushConfigOptionsForPlanCode(profile.plan_code);
    document.getElementById('rush-plan-code').value = profile.plan_code || '';
    document.getElementById('rush-fqn').value = profile.fqn || '';
    // Always assign (not just when truthy) so switching to a profile that
    // omits an addon clears the previous profile's value instead of leaving
    // it stuck in the form.
    document.getElementById('rush-ram').value = profile.ram || '';
    document.getElementById('rush-storage').value = profile.storage || '';
    document.getElementById('rush-bandwidth').value = profile.bandwidth || '';
    document.querySelectorAll('.rush-dc').forEach(cb => {
        cb.checked = (profile.datacenters || '').split(',').map(s => s.trim()).includes(cb.value);
    });
    if (profile.region) document.getElementById('rush-region').value = profile.region;
    if (profile.os) document.getElementById('rush-os').value = profile.os;
    if (profile.duration) document.getElementById('rush-duration').value = profile.duration;
    document.getElementById('rush-auto-pay').checked = !!profile.auto_pay;
    document.getElementById('rush-waive').checked = !!profile.waive_retractation;
}

async function saveProfile() {
    const name = document.getElementById('profile-name').value.trim();
    if (!name) {
        showError('Profile name is required');
        return;
    }
    const profile = {
        name,
        plan_code: document.getElementById('rush-plan-code').value.trim(),
        fqn: document.getElementById('rush-fqn').value.trim(),
        ram: document.getElementById('rush-ram').value.trim() || null,
        storage: document.getElementById('rush-storage').value.trim() || null,
        bandwidth: document.getElementById('rush-bandwidth').value.trim() || null,
        datacenters: getSelectedDatacenters().join(','),
        region: document.getElementById('rush-region').value,
        os: document.getElementById('rush-os').value,
        duration: document.getElementById('rush-duration').value,
        auto_pay: document.getElementById('rush-auto-pay').checked,
        waive_retractation: document.getElementById('rush-waive').checked,
    };
    try {
        // Update in place when editing a loaded profile and the name still
        // matches it; a changed name means "save as new" so users can fork
        // a profile by loading it and renaming.
        const editing = state.editingProfileId
            ? state.profiles?.find(p => p.id === state.editingProfileId)
            : null;
        if (editing && editing.name === name) {
            await apiRequest('PUT', `/profiles/${encodeURIComponent(editing.id)}`, profile);
            showToast(`Profile "${name}" updated.`);
        } else {
            await apiRequest('POST', '/profiles', profile);
            showToast(`Profile "${name}" saved.`);
        }
        state.editingProfileId = null;
        document.getElementById('profile-name').value = '';
        await loadProfiles();
    } catch (e) {
        showError(e.message);
    }
}

async function deleteProfile() {
    const id = document.getElementById('profile-select').value;
    if (!id) return;
    try {
        await apiRequest('DELETE', `/profiles/${encodeURIComponent(id)}`);
        if (state.editingProfileId === id) state.editingProfileId = null;
        await loadProfiles();
    } catch (e) {
        showError(e.message);
    }
}

// ----- Sniper mode -----

function renderSniperAlertSelect() {
    const select = document.getElementById('sniper-alert-select');
    if (!select) return;
    const current = select.value;
    select.innerHTML = '';
    select.appendChild(el('option', { value: '', text: 'Select alert...' }));
    (state.alerts || []).forEach(a => {
        select.appendChild(el('option', { value: a.id, text: `${a.plan_code} (${a.fqn_pattern})` }));
    });
    if (current) select.value = current;
}

async function armSniper() {
    const alertId = document.getElementById('sniper-alert-select').value;
    const profileId = document.getElementById('sniper-profile-select').value;
    if (!alertId || !profileId) {
        showError('Select both an alert and a profile');
        return;
    }
    try {
        await apiRequest('POST', '/sniper/arm', { alert_id: alertId, profile_id: profileId });
        await loadSniperStatus();
    } catch (e) {
        showError(e.message);
    }
}

async function disarmSniper() {
    const alertId = document.getElementById('sniper-alert-select').value;
    if (!alertId) return;
    try {
        await apiRequest('POST', `/sniper/disarm/${encodeURIComponent(alertId)}`);
        await loadSniperStatus();
    } catch (e) {
        showError(e.message);
    }
}

function _sniperArmedInfo(entry) {
    const alert = (state.alerts || []).find(a => a.id === entry.alert_id);
    const profile = (state.profiles || []).find(p => p.id === entry.profile_id);
    return {
        planCode: entry.plan_code || alert?.plan_code || entry.alert_id,
        fqn: entry.fqn_pattern || alert?.fqn_pattern || '(any config)',
        profileName: profile?.name || `profile ${entry.profile_id.slice(0, 8)}`,
        profileId: entry.profile_id,
        alertId: entry.alert_id,
    };
}

async function disarmSniperByAlertId(alertId) {
    if (!alertId) return;
    try {
        await apiRequest('POST', `/sniper/disarm/${encodeURIComponent(alertId)}`);
        await loadSniperStatus();
        renderArmedSniper();
    } catch (e) {
        showError(e.message);
    }
}

function renderArmedSniper() {
    const container = document.getElementById('armed-sniper-list');
    if (!container) return;
    const armed = (state.sniperStatus?.armed) || [];
    container.innerHTML = '';
    if (armed.length === 0) {
        container.appendChild(el('p', { class: 'text-gray-500', text: 'No sniper armed' }));
        return;
    }
    armed.forEach(a => {
        const info = _sniperArmedInfo(a);
        const head = el('div', { class: 'flex justify-between items-center' }, [
            el('span', { class: 'text-blue-400 font-bold', text: info.planCode }),
            el('button', {
                class: 'text-xs bg-red-700 hover:bg-red-600 px-2 py-1 rounded',
                text: 'Disarm',
                onclick: () => disarmSniperByAlertId(info.alertId),
            }),
        ]);
        const cfg = el('div', { class: 'text-gray-400 text-xs font-mono break-all', text: info.fqn });
        const prof = el('div', { class: 'text-gray-500 text-xs', text: `Profile: ${info.profileName}` });
        container.appendChild(el('div', { class: 'bg-gray-700 rounded p-2 space-y-1' }, [head, cfg, prof]));
    });
}

async function loadSniperStatus() {
    const container = document.getElementById('sniper-status');
    try {
        const status = await apiRequest('GET', '/sniper/status');
        if (!status) return;
        state.sniperStatus = status;
        renderArmedSniper();
        const armed = status.armed || [];
        const results = status.results || {};
        if (!container) return;
        if (armed.length === 0 && Object.keys(results).length === 0) {
            container.textContent = 'No sniper armed.';
            return;
        }
        container.innerHTML = '';
        armed.forEach(a => {
            const info = _sniperArmedInfo(a);
            const text = `${info.planCode} — ${info.fqn} (profile: ${info.profileName})`;
            container.appendChild(el('div', { class: 'text-yellow-400', text }));
        });
        for (const [aid, r] of Object.entries(results)) {
            const cls = r.status === 'ordered' ? 'text-green-400' : 'text-red-400';
            const alert = (state.alerts || []).find(a => a.id === aid);
            const label = alert?.plan_code || aid.slice(0, 8);
            const text = `Result: ${label} - ${r.status}${r.order_id ? ` (#${r.order_id})` : ''}`;
            container.appendChild(el('div', { class: cls, text }));
        }
    } catch (e) {
        if (container) container.textContent = 'Failed to load sniper status';
        showToast(`Failed to load sniper status: ${e.message}`, 4000);
    }
}

// ----- Orders -----

async function loadOrders() {
    try {
        const data = await apiRequest('GET', '/insights/orders');
        renderOrders(data?.orders || []);
    } catch (e) {
        console.error('Failed to load orders:', e);
        showToast(`Failed to load recent orders: ${e.message}`, 4000);
    }
}

function renderOrders(orders) {
    const container = document.getElementById('orders-list');
    if (!container) return;
    container.innerHTML = '';
    if (!orders.length) {
        container.appendChild(el('p', { class: 'text-gray-500', text: 'No orders placed' }));
        return;
    }
    orders.slice(0, 10).forEach(o => {
        const time = o.placed_at ? new Date(o.placed_at).toLocaleString() : '';
        const id = o.order_id ? `#${o.order_id}` : '(pending)';
        const refreshBtn = o.order_id ? el('button', {
            class: 'text-xs bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded',
            text: 'Refresh',
            onclick: async (ev) => {
                ev.stopPropagation();
                const btn = ev.currentTarget;
                btn.disabled = true;
                btn.textContent = '...';
                try {
                    const r = await apiRequest('GET', `/insights/orders/${encodeURIComponent(o.order_id)}`);
                    showToast(`Order #${o.order_id}: ${r.status}`);
                    await loadOrders();
                } catch (e) {
                    showError(e.message);
                } finally {
                    btn.disabled = false;
                    btn.textContent = 'Refresh';
                }
            },
        }) : null;
        const head = el('div', { class: 'flex justify-between items-center' }, [
            el('div', {}, [
                el('span', { class: 'text-blue-400 font-bold', text: `${o.plan_code} ${id}` }),
                el('span', { class: 'text-gray-400 ml-2 text-xs', text: time }),
            ]),
            refreshBtn,
        ]);
        const card = el('div', {
            class: `bg-gray-700 rounded p-2 space-y-1 ${o.order_id ? 'cursor-pointer hover:bg-gray-600 transition-colors' : ''}`,
        }, [head, orderStatusBadge(o.status || 'unknown')]);
        if (o.order_id) {
            card.addEventListener('click', () => openOrderInTab(o.order_id));
        }
        container.appendChild(card);
    });
}

// ----- Orders tab -----

const ORDER_STATUS_STYLES = {
    delivered:    'bg-green-900/50 text-green-400 border-green-700',
    delivering:   'bg-blue-900/50 text-blue-400 border-blue-700',
    checking:     'bg-blue-900/50 text-blue-300 border-blue-700',
    notPaid:      'bg-red-900/50 text-red-400 border-red-700',
    cancelled:    'bg-gray-700 text-gray-400 border-gray-600',
    cancelling:   'bg-gray-700 text-gray-400 border-gray-600',
    documentsRequested: 'bg-yellow-900/50 text-yellow-400 border-yellow-700',
    unknown:      'bg-gray-700 text-gray-400 border-gray-600',
};

function orderStatusBadge(status) {
    const cls = ORDER_STATUS_STYLES[status] || ORDER_STATUS_STYLES.unknown;
    return el('span', { class: `text-xs px-2 py-0.5 rounded border ${cls}`, text: status || 'unknown' });
}

function _ordersMatchFilter(order, filter) {
    if (filter === 'all') return true;
    const st = (order.status || '').toLowerCase();
    if (filter === 'pending') return ['checking', 'delivering', 'notpaid', 'documentsrequested', 'unknown'].includes(st);
    if (filter === 'delivered') return st === 'delivered';
    if (filter === 'cancelled') return ['cancelled', 'cancelling'].includes(st);
    return true;
}

async function openOrderInTab(orderId) {
    if (!orderId) return;
    await switchTab('orders-tab');
    state.selectedOrderId = orderId;
    renderOrdersList();
    loadOrderDetail(orderId);
}

// Owned servers tab

async function loadServersTab() {
    const container = document.getElementById('servers-list');
    if (!container) return;
    container.innerHTML = '';
    container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'Loading servers from OVH...' }));
    const gen = state._switchGen;
    try {
        const data = await apiRequest('GET', '/servers');
        if (gen !== state._switchGen) return;
        renderServersList(data?.servers || []);
    } catch (e) {
        if (gen !== state._switchGen) return;
        container.innerHTML = '';
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

function renderServersList(servers) {
    const container = document.getElementById('servers-list');
    if (!container) return;
    container.innerHTML = '';
    if (!servers.length) {
        container.appendChild(el('p', { class: 'text-gray-500', text: 'No dedicated servers on this account.' }));
        return;
    }
    servers.forEach(s => {
        const stateColor = s.state === 'ok' ? 'text-green-400' : 'text-yellow-400';
        const row = el('div', {
            class: 'bg-gray-700/50 hover:bg-gray-700 rounded p-2 cursor-pointer flex justify-between items-center gap-2',
            role: 'button', tabindex: '0',
        }, [
            el('div', {}, [
                el('span', { class: 'text-blue-400 font-bold', text: s.display_name || s.service_name }),
                el('p', { class: 'text-gray-400 text-xs', text: [
                    s.commercial_range, s.datacenter, s.os,
                ].filter(Boolean).join(' · ') || s.service_name }),
            ]),
            el('div', { class: 'text-right' }, [
                s.state ? el('p', { class: `text-xs font-bold ${stateColor}`, text: s.state }) : null,
                s.expiration ? el('p', { class: 'text-gray-500 text-xs', text: `exp. ${s.expiration}` }) : null,
            ].filter(Boolean)),
        ]);
        const open = () => loadServerDetail(s.service_name);
        row.addEventListener('click', open);
        row.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });
        container.appendChild(row);
    });
}

async function loadServerDetail(serviceName) {
    const container = document.getElementById('server-detail');
    if (!container) return;
    container.innerHTML = '';
    container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'Loading server details...' }));
    const gen = state._switchGen;
    try {
        const data = await apiRequest('GET', `/servers/${encodeURIComponent(serviceName)}`);
        if (gen !== state._switchGen) return;
        container.innerHTML = '';
        const summary = data.summary || {};
        container.appendChild(el('h3', { class: 'text-lg font-bold mb-3', text: summary.display_name || serviceName }));
        const grid = el('div', { class: 'grid grid-cols-1 sm:grid-cols-2 gap-2' });
        const detail = data.detail || {};
        const info = data.service_info || {};
        const fields = [
            ['Service', serviceName],
            ['State', detail.state],
            ['Datacenter', detail.datacenter],
            ['Range', detail.commercialRange],
            ['OS', detail.os],
            ['IP', detail.ip],
            ['Reverse', detail.reverse],
            ['Rack', detail.rack],
            ['Monitoring', detail.monitoring != null ? String(detail.monitoring) : null],
            ['Expiration', info.expiration],
            ['Renewal', info.renewalType],
            ['Creation', info.creation],
        ];
        for (const [label, value] of fields) {
            if (value == null || value === '') continue;
            grid.appendChild(el('div', { class: 'bg-gray-700 rounded p-2' }, [
                el('p', { class: 'text-gray-500 text-xs', text: label }),
                el('p', { class: 'text-gray-200 text-sm break-all', text: String(value) }),
            ]));
        }
        container.appendChild(grid);
    } catch (e) {
        if (gen !== state._switchGen) return;
        container.innerHTML = '';
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

async function loadOrdersTab(refresh = false) {
    const container = document.getElementById('orders-full-list');
    if (!container) return;
    container.innerHTML = '';
    container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'Loading orders from OVH...' }));
    try {
        const data = await apiRequest('GET', `/orders?limit=${state.uiPrefs.ordersLimit}&days=${state.uiPrefs.ordersDays}${refresh ? '&refresh=true' : ''}`);
        state.allOrders = data?.orders || [];
        renderOrdersList();
    } catch (e) {
        container.innerHTML = '';
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

function renderOrdersList() {
    const container = document.getElementById('orders-full-list');
    if (!container) return;
    container.innerHTML = '';
    const filtered = state.allOrders.filter(o => _ordersMatchFilter(o, state.ordersFilter));
    if (!filtered.length) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No orders found.' }));
        return;
    }
    filtered.forEach(o => {
        const dateStr = o.date || o.placed_at || '';
        const date = dateStr ? new Date(dateStr).toLocaleDateString() : '';
        const priceStr = (o.price_with_tax != null && o.currency_code)
            ? displayPrice(o.price_with_tax, null, o.currency_code)
            : '';
        const isSelected = o.order_id === state.selectedOrderId;
        const card = el('div', {
            class: `rounded p-3 cursor-pointer transition-colors border ${isSelected ? 'bg-blue-600/30 border-blue-500' : 'bg-gray-700 border-gray-600 hover:bg-gray-600'}`,
            role: 'button',
            tabindex: '0',
        });
        card.addEventListener('click', () => {
            state.selectedOrderId = o.order_id;
            renderOrdersList();
            loadOrderDetail(o.order_id);
        });
        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); card.click(); }
        });
        const topRow = el('div', { class: 'flex justify-between items-center gap-2' }, [
            el('div', { class: 'min-w-0 flex-1' }, [
                el('span', { class: 'text-blue-400 font-bold text-sm', text: o.server_name || o.plan_code || 'OVH order' }),
                el('span', { class: 'text-gray-400 ml-2 text-xs', text: `#${o.order_id || '?'}` }),
            ]),
            orderStatusBadge(o.status),
        ]);
        const bottomRow = el('div', { class: 'flex justify-between items-center mt-1' }, [
            el('span', { class: 'text-gray-500 text-xs', text: date }),
            el('span', { class: 'text-yellow-400 text-xs font-bold', text: priceStr }),
        ]);
        card.appendChild(topRow);
        card.appendChild(bottomRow);
        container.appendChild(card);
    });
}

// Logs tab

const LOG_LEVEL_ORDER = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
const LOG_LEVEL_CLASS = {
    DEBUG: 'text-gray-500',
    INFO: 'text-gray-300',
    WARNING: 'text-yellow-400',
    ERROR: 'text-red-400',
    CRITICAL: 'text-red-400',
};

async function loadLogsTab() {
    const container = document.getElementById('logs-list');
    if (!container) return;
    container.innerHTML = '';
    container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'Loading logs…' }));
    try {
        const data = await apiRequest('GET', `/logs?limit=${state.uiPrefs.logsLimit}`);
        state.logsBuffer = data?.logs || [];
        populateLogsSourceSelect(data?.sources || []);
        renderLogsList();
        startLogStream();
    } catch (e) {
        container.innerHTML = '';
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

function populateLogsSourceSelect(sources) {
    const sel = document.getElementById('logs-source');
    if (!sel) return;
    const current = state.logsFilter.source;
    sel.innerHTML = '';
    sel.appendChild(el('option', { value: 'all', text: 'All sources' }));
    sources.forEach(s => sel.appendChild(el('option', { value: s, text: s })));
    // Preserve the selection if it still exists.
    sel.value = sources.includes(current) ? current : 'all';
    state.logsFilter.source = sel.value;
}

function _logMatchesFilter(entry, f) {
    if (f.level && f.level !== 'all') {
        const min = LOG_LEVEL_ORDER[f.level] || 0;
        if ((LOG_LEVEL_ORDER[entry.level] || 0) < min) return false;
    }
    if (f.source && f.source !== 'all' && entry.source !== f.source) return false;
    if (f.search && !entry.message.toLowerCase().includes(f.search.toLowerCase())) return false;
    return true;
}

function renderLogsList() {
    const container = document.getElementById('logs-list');
    if (!container) return;
    container.innerHTML = '';
    const filtered = state.logsBuffer.filter(e => _logMatchesFilter(e, state.logsFilter));
    if (!filtered.length) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No log entries match the current filters.' }));
        return;
    }
    filtered.forEach(e => container.appendChild(renderLogRow(e)));
    // Auto-scroll to the newest line unless the tail is paused.
    if (!state.logsPaused) container.scrollTop = container.scrollHeight;
}

function renderLogRow(entry) {
    const time = entry.ts ? new Date(entry.ts).toLocaleTimeString() : '';
    return el('div', { class: 'flex gap-2 whitespace-pre-wrap break-words' }, [
        el('span', { class: 'text-gray-500 shrink-0', text: time }),
        el('span', { class: `shrink-0 font-bold ${LOG_LEVEL_CLASS[entry.level] || 'text-gray-300'}`, text: entry.level.padEnd(7) }),
        el('span', { class: 'text-blue-400 shrink-0', text: entry.source }),
        el('span', { class: LOG_LEVEL_CLASS[entry.level] || 'text-gray-300', text: entry.message }),
    ]);
}

function appendLogEntry(entry) {
    state.logsBuffer.push(entry);
    // Cap the client buffer so a long-running tab can't grow without bound.
    const MAX = 5000;
    if (state.logsBuffer.length > MAX) {
        state.logsBuffer.splice(0, state.logsBuffer.length - MAX);
    }
    // Ensure a new source shows up in the dropdown.
    const sel = document.getElementById('logs-source');
    if (sel && !Array.from(sel.options).some(o => o.value === entry.source)) {
        sel.appendChild(el('option', { value: entry.source, text: entry.source }));
    }
    if (state.logsPaused) return;
    if (!_logMatchesFilter(entry, state.logsFilter)) return;
    const container = document.getElementById('logs-list');
    if (!container) return;
    // Drop the empty-state placeholder on the first live line.
    const placeholder = container.querySelector('p');
    if (placeholder) container.innerHTML = '';
    container.appendChild(renderLogRow(entry));
    container.scrollTop = container.scrollHeight;
}

function startLogStream() {
    if (state.logsEventSource) state.logsEventSource.close();
    if (state.logsReconnectTimer) {
        clearTimeout(state.logsReconnectTimer);
        state.logsReconnectTimer = null;
    }
    state.logsEventSource = new EventSource(`${API_BASE}/logs/stream`);
    state.logsEventSource.onopen = () => {
        state.logsReconnectDelay = RECONNECT_BASE_MS;
    };
    state.logsEventSource.onmessage = (event) => {
        try {
            appendLogEntry(JSON.parse(event.data));
        } catch (e) {
            console.error('Failed to parse log SSE message:', e);
        }
    };
    state.logsEventSource.onerror = () => {
        if (state.logsEventSource) {
            state.logsEventSource.close();
            state.logsEventSource = null;
        }
        // Reconnect only while the logs tab is still the active view.
        const logsTab = document.getElementById('logs-tab');
        const visible = logsTab && !logsTab.classList.contains('hidden');
        if (visible && !state.logsReconnectTimer) {
            const delay = state.logsReconnectDelay;
            state.logsReconnectDelay = Math.min(delay * 2, RECONNECT_MAX_MS);
            state.logsReconnectTimer = setTimeout(() => {
                state.logsReconnectTimer = null;
                startLogStream();
            }, delay);
        }
    };
}

function stopLogStream() {
    state.logsReconnectDelay = RECONNECT_BASE_MS;
    if (state.logsReconnectTimer) {
        clearTimeout(state.logsReconnectTimer);
        state.logsReconnectTimer = null;
    }
    if (state.logsEventSource) {
        state.logsEventSource.close();
        state.logsEventSource = null;
    }
}

async function loadOrderDetail(orderId) {
    const container = document.getElementById('order-detail');
    if (!container) return;
    container.innerHTML = '';
    container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'Loading order details...' }));
    try {
        const data = await apiRequest('GET', `/orders/${encodeURIComponent(orderId)}`);
        renderOrderDetail(data);
    } catch (e) {
        container.innerHTML = '';
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

function renderOrderDetail(data) {
    const container = document.getElementById('order-detail');
    if (!container) return;
    container.innerHTML = '';
    const order = data.order || {};
    const status = data.status || 'unknown';
    const details = data.details || [];
    const followup = data.followup || [];
    const orderId = order.orderId || state.selectedOrderId;

    // Header
    const header = el('div', { class: 'flex justify-between items-center mb-4' }, [
        el('div', {}, [
            el('h3', { class: 'text-lg font-bold text-blue-400', text: `Order #${orderId}` }),
            el('p', { class: 'text-gray-400 text-xs', text: order.date ? new Date(order.date).toLocaleString() : '' }),
        ]),
        orderStatusBadge(status),
    ]);
    container.appendChild(header);

    // Price breakdown
    const priceSection = el('div', { class: 'space-y-1 mb-4' });
    const pwt = order.priceWithTax || {};
    const pwot = order.priceWithoutTax || {};
    const tax = order.tax || {};
    if (pwt.text) {
        priceSection.appendChild(el('p', {}, [
            el('span', { class: 'text-gray-400 text-sm', text: 'Total (with tax): ' }),
            el('span', { class: 'text-yellow-400 font-bold text-sm', text: pwt.text }),
        ]));
    }
    if (pwot.text) {
        priceSection.appendChild(el('p', { class: 'text-gray-400 text-xs', text: `Subtotal: ${pwot.text}` }));
    }
    if (tax.text) {
        priceSection.appendChild(el('p', { class: 'text-gray-400 text-xs', text: `Tax: ${tax.text}` }));
    }
    container.appendChild(priceSection);

    // Dates (retraction, expiration)
    if (order.retractionDate) {
        const retDate = new Date(order.retractionDate);
        const isFuture = retDate > new Date();
        priceSection.appendChild(el('p', { class: `text-xs ${isFuture ? 'text-yellow-400' : 'text-gray-500'}`, text: `Retraction period: ${retDate.toLocaleString()}${isFuture ? ' (active)' : ' (expired)'}` }));
    }
    if (order.expirationDate) {
        priceSection.appendChild(el('p', { class: 'text-gray-400 text-xs', text: `Expires: ${new Date(order.expirationDate).toLocaleString()}` }));
    }

    // Actions
    const actionsRow = el('div', { class: 'flex gap-2 mb-4 flex-wrap' });
    if (order.pdfUrl) {
        actionsRow.appendChild(el('a', {
            href: order.pdfUrl, target: '_blank', rel: 'noopener',
            class: 'bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded text-sm',
            text: 'View Invoice PDF',
        }));
    }
    if (order.url) {
        actionsRow.appendChild(el('a', {
            href: order.url, target: '_blank', rel: 'noopener',
            class: 'bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded text-sm',
            text: 'Open in OVH Manager',
        }));
    }
    // Waive retraction + cancel buttons (only if retraction period is active
    // and the order is paid and not already cancelled/delivered — OVH
    // rejects retraction on unpaid orders with 403 "Order is not paid").
    const cancellableStatus = !['cancelled', 'cancelling', 'delivered', 'notpaid', 'unknown'].includes((status || '').toLowerCase());
    if (order.retractionDate && new Date(order.retractionDate) > new Date() && cancellableStatus) {
        const waiveBtn = el('button', {
            class: 'bg-yellow-600 hover:bg-yellow-700 px-3 py-1 rounded text-sm',
            text: 'Waive Retraction',
        });
        waiveBtn.addEventListener('click', async () => {
            if (!confirm('Waive the retraction period? This speeds up delivery but you forfeit your right of withdrawal.')) return;
            waiveBtn.disabled = true;
            waiveBtn.textContent = 'Waiving...';
            try {
                await apiRequest('POST', `/orders/${encodeURIComponent(orderId)}/waive-retraction`);
                showToast('Retraction waived.');
                await loadOrderDetail(orderId);
            } catch (e) {
                showError(e.message);
            } finally {
                waiveBtn.disabled = false;
                waiveBtn.textContent = 'Waive Retraction';
            }
        });
        actionsRow.appendChild(waiveBtn);

        const cancelBtn = el('button', {
            class: 'bg-red-600 hover:bg-red-700 px-3 py-1 rounded text-sm',
            text: 'Cancel Order',
        });
        cancelBtn.addEventListener('click', async () => {
            if (!confirm('Cancel this order? This exercises your right of retraction (withdrawal) and cannot be undone.')) return;
            cancelBtn.disabled = true;
            cancelBtn.textContent = 'Cancelling...';
            try {
                const r = await apiRequest('POST', `/orders/${encodeURIComponent(orderId)}/cancel`);
                showToast(`Order cancelled: ${r.status}`);
                await loadOrderDetail(orderId);
                await loadOrdersTab();
            } catch (e) {
                showError(e.message);
            } finally {
                cancelBtn.disabled = false;
                cancelBtn.textContent = 'Cancel Order';
            }
        });
        actionsRow.appendChild(cancelBtn);
    }
    if (actionsRow.children.length) container.appendChild(actionsRow);

    // Line items. Prefer the backend's grouped `line_items` (one row per
    // physical item, setup + monthly merged); fall back to raw `details`.
    const lineItems = data.line_items || [];
    if (lineItems.length) {
        const itemsSection = el('div', { class: 'mb-4' });
        itemsSection.appendChild(el('h4', { class: 'font-bold text-gray-400 text-sm uppercase mb-2', text: 'Line Items' }));
        for (const item of lineItems) {
            const parts = [];
            if (item.setup_price && item.setup_price.value) parts.push(`${item.setup_price.text} setup`);
            if (item.recurring_price && item.recurring_price.value) parts.push(`${item.recurring_price.text}/mo`);
            const priceText = parts.length ? parts.join(' + ') : 'Included';
            const labelCls = 'text-gray-200 text-sm' + (item.cancelled ? ' line-through text-gray-500' : '');
            itemsSection.appendChild(el('div', { class: 'flex justify-between items-center bg-gray-700 rounded px-3 py-2 mb-1' }, [
                el('div', { class: 'min-w-0 flex-1' }, [
                    el('span', { class: labelCls, text: item.label || '(line item)' }),
                ]),
                el('span', { class: 'text-gray-400 text-xs whitespace-nowrap', text: priceText }),
            ]));
        }
        container.appendChild(itemsSection);
    }

    // Follow-up timeline
    if (followup.length) {
        const followSection = el('div', { class: 'mb-4' });
        followSection.appendChild(el('h4', { class: 'font-bold text-gray-400 text-sm uppercase mb-2', text: 'Delivery Timeline' }));
        for (const f of followup) {
            const stepClass = f.status === 'DONE' ? 'text-green-400' :
                              f.status === 'DOING' ? 'text-blue-400' :
                              f.status === 'ERROR' ? 'text-red-400' :
                              'text-gray-400';
            followSection.appendChild(el('div', { class: 'bg-gray-700 rounded px-3 py-2 mb-1' }, [
                el('div', { class: 'flex justify-between' }, [
                    el('span', { class: 'text-sm font-bold', text: f.step || '' }),
                    el('span', { class: `text-xs font-bold ${stepClass}`, text: f.status || '' }),
                ]),
            ]));
            for (const h of (f.history || [])) {
                followSection.appendChild(el('div', { class: 'text-gray-500 text-xs ml-4 pl-2 border-l border-gray-600' }, [
                    el('span', { text: h.date ? new Date(h.date).toLocaleString() : '' }),
                    el('span', { class: 'ml-2', text: h.label || h.description || '' }),
                ]));
            }
        }
        container.appendChild(followSection);
    }
}

// ----- Insights tab -----

function populateInsightsPlanSelect() {
    const select = document.getElementById('insights-plan-select');
    if (!select) return;
    const current = select.value;
    select.innerHTML = '';
    select.appendChild(el('option', { value: '', text: 'Select a monitored plan…' }));
    const codes = sortedMonitoredPlanCodes();
    codes.forEach(code => {
        select.appendChild(el('option', { value: code, text: insightsPlanLabel(code) }));
    });
    if (current && codes.includes(current)) {
        select.value = current;
    }
}

function sortedMonitoredPlanCodes() {
    const fromAlerts = (state.alerts || []).map(a => a.plan_code);
    const fromCatalog = state.plans.map(p => p.planCode);
    const merged = Array.from(new Set([...fromAlerts, ...fromCatalog]));
    return merged.sort();
}

function insightsPlanLabel(code) {
    const p = (state.plans || []).find(x => x.planCode === code);
    return p && p.invoiceName ? `${p.invoiceName} (${code})` : code;
}

// Split an FQN ("plan.ram-….softraid-…") into readable addon labels.
function humanizeFqn(fqn) {
    if (!fqn) return 'Unknown config';
    const parts = fqn.split('.').slice(1);  // drop the leading plan code
    if (!parts.length) return fqn;
    return parts.map(humanizeAddon).join(' · ');
}

function formatDuration(sec) {
    if (sec == null) return '—';
    sec = Math.round(sec);
    if (sec < 60) return `${sec}s`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    if (h < 24) return rm ? `${h}h ${rm}m` : `${h}h`;
    const d = Math.floor(h / 24);
    const rh = h % 24;
    return rh ? `${d}d ${rh}h` : `${d}d`;
}

function relativeTime(ts) {
    if (!ts) return '—';
    const then = new Date(ts).getTime();
    if (isNaN(then)) return '—';
    const diff = Math.round((Date.now() - then) / 1000);
    if (diff < 45) return 'just now';
    if (diff < 5400) return `${formatDuration(diff)} ago`;
    const h = Math.round(diff / 3600);
    if (h < 36) return `${h}h ago`;
    return `${Math.round(h / 24)}d ago`;
}

function median(nums) {
    if (!nums.length) return null;
    const s = [...nums].sort((a, b) => a - b);
    const mid = Math.floor(s.length / 2);
    return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

async function loadInsightsData() {
    const days = insightsDays();
    loadInsightsOverview(days);
    loadInsightsPromos();
    const planCode = document.getElementById('insights-plan-select').value;
    const detail = document.getElementById('insights-detail');
    if (!planCode) {
        if (detail) detail.classList.add('hidden');
        return;
    }
    if (detail) detail.classList.remove('hidden');
    loadInsightsDetail(planCode, days);
    loadPriceWatchPanel(planCode);
}

function insightsDays() {
    return parseInt(document.getElementById('insights-days').value, 10) || 30;
}

// Price watches: notify when a plan's monthly price drops to a cap.

async function loadPriceWatchPanel(planCode) {
    const gen = state._switchGen;
    const current = document.getElementById('price-watch-current');
    const currencyEl = document.getElementById('price-watch-currency');
    if (currencyEl) currencyEl.textContent = state.catalogCurrency || '';
    if (!current) return;
    try {
        const data = await apiRequest('GET', '/price-watches');
        if (gen !== state._switchGen) return;
        const watch = (data.watches || []).find(w => w.plan_code === planCode);
        current.innerHTML = '';
        if (!watch) {
            current.appendChild(el('span', { class: 'text-gray-500', text: 'No price watch on this plan.' }));
            return;
        }
        const thresholdUnits = (watch.threshold_ucents / 100000000).toFixed(2);
        current.appendChild(el('span', {
            text: `Watching: alert below ${thresholdUnits} ${watch.currency_code || ''} `,
        }));
        const del = el('button', { class: 'text-red-400 hover:text-red-300 ml-1 text-xs', text: 'remove' });
        del.addEventListener('click', async () => {
            try {
                await apiRequest('DELETE', `/price-watches/${encodeURIComponent(watch.id)}`);
                showToast('Price watch removed.');
                await loadPriceWatchPanel(planCode);
            } catch (e) {
                showError(e.message);
            }
        });
        current.appendChild(del);
        if (watch.notified_at) {
            current.appendChild(el('div', {
                class: 'text-xs text-gray-500',
                text: `Last alert: ${new Date(watch.notified_at).toLocaleString()}`,
            }));
        }
    } catch (e) {
        console.error('Failed to load price watches:', e);
    }
}

async function savePriceWatch() {
    const planCode = document.getElementById('insights-plan-select').value;
    if (!planCode) {
        showToast('Select a plan first.', 3000);
        return;
    }
    const raw = document.getElementById('price-watch-threshold').value.trim();
    const units = parseFloat(raw);
    if (!raw || isNaN(units) || units <= 0) {
        showToast('Enter a valid threshold price.', 3000);
        return;
    }
    try {
        await apiRequest('POST', '/price-watches', {
            plan_code: planCode,
            threshold_ucents: Math.round(units * 100000000),
        });
        showToast(`Price watch saved: ${planCode} below ${units.toFixed(2)}`);
        await loadPriceWatchPanel(planCode);
    } catch (e) {
        showError(e.message);
    }
}

async function loadInsightsPromos() {
    const gen = state._switchGen;
    const container = document.getElementById('insights-promos');
    if (!container) return;
    try {
        const data = await apiRequest('GET', '/insights/promos');
        if (gen !== state._switchGen) return;
        const promos = data.promos || [];
        container.innerHTML = '';
        if (promos.length === 0) {
            container.appendChild(el('p', { class: 'text-gray-500', text: 'No promotions seen' }));
            return;
        }
        promos.forEach(p => {
            let desc = p.payload;
            try {
                const parsed = JSON.parse(p.payload);
                desc = parsed.description || parsed.name || p.payload;
            } catch { /* raw payload fallback */ }
            container.appendChild(el('div', { class: 'bg-gray-700/50 rounded p-2 flex justify-between items-center gap-2' }, [
                el('div', {}, [
                    el('span', { class: 'text-yellow-400 font-bold font-mono', text: p.plan_code }),
                    el('p', { class: 'text-gray-300 text-xs truncate', text: String(desc).slice(0, 160) }),
                ]),
                el('span', { class: 'text-gray-500 text-xs whitespace-nowrap', text: relativeTime(p.first_seen) }),
            ]));
        });
    } catch (e) {
        console.error('Failed to load promos:', e);
    }
}

function selectInsightsPlan(code) {
    const select = document.getElementById('insights-plan-select');
    if (!select) return;
    if (!Array.from(select.options).some(o => o.value === code)) {
        select.appendChild(el('option', { value: code, text: insightsPlanLabel(code) }));
    }
    select.value = code;
    loadInsightsData();
    document.getElementById('insights-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function loadInsightsOverview(days) {
    const container = document.getElementById('insights-overview');
    if (!container) return;
    try {
        const data = await apiRequest('GET', `/insights/summary?days=${days}`);
        const plans = data?.plans || [];
        container.innerHTML = '';
        if (!plans.length) {
            container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No restock history logged yet. Add alerts and start the monitor to build history over time.' }));
            return;
        }
        const headCell = (t, extra = '') => el('th', { class: `py-2 pr-4 font-medium ${extra}`, text: t });
        const table = el('table', { class: 'w-full text-sm border-collapse' }, [
            el('thead', {}, [
                el('tr', { class: 'text-left text-gray-400 border-b border-gray-700' }, [
                    headCell('Plan'),
                    headCell('Status'),
                    headCell('Restocks', 'text-right'),
                    headCell('Typical window', 'text-right'),
                    headCell('Last restock', 'text-right'),
                ]),
            ]),
        ]);
        const tbody = el('tbody');
        plans.forEach(p => {
            const nameCell = el('td', { class: 'py-2 pr-4' }, [
                el('div', { class: 'font-mono text-gray-200', text: p.plan_code }),
            ]);
            const label = insightsPlanLabel(p.plan_code);
            if (label !== p.plan_code) {
                nameCell.appendChild(el('div', { class: 'text-xs text-gray-500 truncate max-w-[16rem]', text: label.replace(` (${p.plan_code})`, '') }));
            }
            const row = el('tr', {
                class: 'border-b border-gray-700/40 hover:bg-gray-700/40 cursor-pointer',
                onclick: () => selectInsightsPlan(p.plan_code),
            }, [
                nameCell,
                el('td', { class: 'py-2 pr-4' }, [stockBadge(p.in_stock_now)]),
                el('td', { class: 'py-2 pr-4 text-right tabular-nums', text: String(p.restocks) }),
                el('td', { class: 'py-2 pr-4 text-right tabular-nums text-gray-300', text: p.median_window_seconds != null ? formatDuration(p.median_window_seconds) : '—' }),
                el('td', { class: 'py-2 pr-4 text-right text-gray-400', text: p.last_restock ? relativeTime(p.last_restock) : '—' }),
            ]);
            tbody.appendChild(row);
        });
        table.appendChild(tbody);
        container.appendChild(table);
    } catch (e) {
        container.innerHTML = '';
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

function stockBadge(inStock) {
    return el('span', {
        class: inStock
            ? 'inline-flex items-center gap-1 text-xs font-semibold text-green-400'
            : 'inline-flex items-center gap-1 text-xs text-gray-500',
    }, [
        el('span', { class: `w-2 h-2 rounded-full ${inStock ? 'bg-green-500' : 'bg-gray-600'}` }),
        inStock ? 'In stock' : 'Out',
    ]);
}

async function loadInsightsDetail(planCode, days) {
    const [histRes, priceRes, patternsRes] = await Promise.allSettled([
        apiRequest('GET', `/insights/history/${encodeURIComponent(planCode)}?days=${days}`),
        apiRequest('GET', `/insights/price/${encodeURIComponent(planCode)}`),
        apiRequest('GET', `/insights/patterns/${encodeURIComponent(planCode)}?days=${days}`),
    ]);
    const eventsDesc = (histRes.status === 'fulfilled' ? histRes.value?.events : []) || [];
    const priceHistory = (priceRes.status === 'fulfilled' ? priceRes.value?.history : []) || [];
    const hourlyCounts = (patternsRes.status === 'fulfilled' ? patternsRes.value?.hourly_counts : []) || [];
    const eventsAsc = [...eventsDesc].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
    const windows = computeAvailabilityWindows(eventsAsc);
    renderInsightsSummary(eventsAsc, windows, priceHistory);
    renderRestockHeatmap(eventsAsc);
    renderHourlyPatterns(hourlyCounts);
    renderInsightsWindows(windows);
    renderInsightsActivity(eventsDesc);
    renderPriceTrend(planCode, priceHistory);
}

// Hour-of-day restock bars from the server-side aggregate
// (GET /insights/patterns). Unlike the heatmap, which is derived from the
// (capped) event history payload, this aggregation runs over ALL events in
// SQLite. Server hours are UTC; shift into the viewer's local time.
function renderHourlyPatterns(hourlyCounts) {
    const container = document.getElementById('insights-hourly');
    if (!container) return;
    container.innerHTML = '';
    if (!hourlyCounts.length) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No restocks logged yet for this range.' }));
        return;
    }
    const tzShift = -new Date().getTimezoneOffset() / 60;  // UTC → local
    const local = new Array(24).fill(0);
    hourlyCounts.forEach(({ hour, count }) => {
        local[(((hour + tzShift) % 24) + 24) % 24] += count;
    });
    const max = Math.max(...local, 1);
    const bars = el('div', { class: 'flex items-end gap-px h-16' });
    for (let h = 0; h < 24; h++) {
        const pct = Math.round((local[h] / max) * 100);
        bars.appendChild(el('div', {
            class: 'flex-1 rounded-t bg-blue-500/70 min-h-[2px]',
            style: `height: ${Math.max(pct, 3)}%`,
            title: `${h}:00 — ${local[h]} restock${local[h] === 1 ? '' : 's'}`,
        }));
    }
    container.appendChild(bars);
    const axis = el('div', { class: 'flex gap-px mt-1' });
    for (let h = 0; h < 24; h++) {
        axis.appendChild(el('div', { class: 'flex-1 text-center text-[10px] text-gray-500', text: h % 3 === 0 ? String(h) : '' }));
    }
    container.appendChild(axis);
}

// Pair each `available` event with the following `unavailable` for the same
// config to measure how long it stayed orderable. Unclosed pairs are still
// in stock now.
function computeAvailabilityWindows(eventsAsc) {
    const open = {};
    const windows = [];
    eventsAsc.forEach(e => {
        const ts = new Date(e.timestamp);
        if (e.event_type === 'available') {
            if (!(e.fqn in open)) open[e.fqn] = ts;
        } else if (e.fqn in open) {
            const start = open[e.fqn];
            delete open[e.fqn];
            windows.push({ fqn: e.fqn, start, end: ts, durationSec: (ts - start) / 1000, open: false });
        }
    });
    Object.entries(open).forEach(([fqn, start]) => {
        windows.push({ fqn, start, end: null, durationSec: (Date.now() - start) / 1000, open: true });
    });
    windows.sort((a, b) => b.start - a.start);
    return windows;
}

function statTile(label, value, sub, accent = 'text-gray-100') {
    return el('div', { class: 'bg-gray-800 rounded-lg p-3' }, [
        el('div', { class: 'text-xs text-gray-400 mb-1', text: label }),
        el('div', { class: `text-lg font-bold leading-tight ${accent}`, text: value }),
        sub ? el('div', { class: 'text-xs text-gray-500 mt-0.5', text: sub }) : null,
    ]);
}

function renderInsightsSummary(eventsAsc, windows, priceHistory) {
    const container = document.getElementById('insights-summary');
    if (!container) return;
    container.innerHTML = '';
    const restocks = eventsAsc.filter(e => e.event_type === 'available').length;
    const inStock = windows.some(w => w.open);
    const closed = windows.filter(w => !w.open).map(w => w.durationSec);
    const med = median(closed);
    const lastAvail = eventsAsc.filter(e => e.event_type === 'available').slice(-1)[0];
    let priceNow = '—', priceSub = '';
    if (priceHistory.length) {
        const latest = priceHistory[0];
        const units = priceHistory.map(h => convertMicrocents(h.price_in_ucents, h.currency_code || state.catalogCurrency));
        priceNow = formatCurrency(convertMicrocents(latest.price_in_ucents, latest.currency_code || state.catalogCurrency));
        priceSub = `min ${formatCurrency(Math.min(...units))} · max ${formatCurrency(Math.max(...units))}`;
    }
    container.appendChild(statTile('Availability', inStock ? 'In stock' : 'Out of stock', inStock ? 'orderable now' : 'not orderable', inStock ? 'text-green-400' : 'text-gray-400'));
    container.appendChild(statTile('Restocks', String(restocks), 'in selected period'));
    container.appendChild(statTile('Typical time in stock', med != null ? formatDuration(med) : '—', med != null ? 'median window' : 'no closed windows'));
    container.appendChild(statTile('Last restock', lastAvail ? relativeTime(lastAvail.timestamp) : '—', lastAvail ? new Date(lastAvail.timestamp).toLocaleString() : 'none yet'));
    container.appendChild(statTile('Price', priceNow, priceSub || 'no price logged', 'text-blue-300'));
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

// Sequential single-hue (blue) heatmap of restock frequency by weekday × hour,
// in the viewer's local time.
function renderRestockHeatmap(eventsAsc) {
    const container = document.getElementById('insights-heatmap');
    if (!container) return;
    container.innerHTML = '';
    const avail = eventsAsc.filter(e => e.event_type === 'available');
    if (!avail.length) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No restocks logged yet for this range.' }));
        return;
    }
    // grid[dayMon0][hour]
    const grid = Array.from({ length: 7 }, () => new Array(24).fill(0));
    avail.forEach(e => {
        const d = new Date(e.timestamp);
        const day = (d.getDay() + 6) % 7;  // JS Sun=0 → Mon=0
        grid[day][d.getHours()]++;
    });
    let max = 0, peak = { day: 0, hour: 0, count: 0 };
    for (let dy = 0; dy < 7; dy++) {
        for (let h = 0; h < 24; h++) {
            if (grid[dy][h] > max) max = grid[dy][h];
            if (grid[dy][h] > peak.count) peak = { day: dy, hour: h, count: grid[dy][h] };
        }
    }
    const cellColor = (c) => {
        if (!c) return 'rgba(148,163,184,0.10)';  // faint slate for zero
        const a = 0.25 + 0.75 * (c / max);        // sequential blue-500 ramp
        return `rgba(59,130,246,${a.toFixed(3)})`;
    };
    const scroll = el('div', { class: 'overflow-x-auto' });
    const table = el('div', { class: 'inline-block min-w-full' });
    // Hour axis (label every 3h)
    const axis = el('div', { class: 'flex mb-1' }, [el('div', { class: 'w-9 shrink-0' })]);
    for (let h = 0; h < 24; h++) {
        axis.appendChild(el('div', { class: 'flex-1 text-center text-[10px] text-gray-500', text: h % 3 === 0 ? String(h) : '' }));
    }
    table.appendChild(axis);
    for (let dy = 0; dy < 7; dy++) {
        const row = el('div', { class: 'flex items-center mb-0.5' }, [
            el('div', { class: 'w-9 shrink-0 text-[11px] text-gray-400', text: WEEKDAYS[dy] }),
        ]);
        for (let h = 0; h < 24; h++) {
            const c = grid[dy][h];
            row.appendChild(el('div', {
                class: 'flex-1 h-5 rounded-sm mx-px',
                style: `background:${cellColor(c)}`,
                title: `${WEEKDAYS[dy]} ${String(h).padStart(2, '0')}:00 — ${c} restock${c === 1 ? '' : 's'}`,
            }));
        }
        table.appendChild(row);
    }
    scroll.appendChild(table);
    container.appendChild(scroll);
    if (peak.count) {
        container.appendChild(el('p', { class: 'text-xs text-gray-300 mt-3' }, [
            'Most restocks land on ',
            el('span', { class: 'font-semibold text-blue-300', text: `${WEEKDAYS[peak.day]} around ${String(peak.hour).padStart(2, '0')}:00` }),
            ` (${peak.count} in this range).`,
        ]));
    }
}

function renderInsightsWindows(windows) {
    const container = document.getElementById('insights-windows');
    if (!container) return;
    container.innerHTML = '';
    if (!windows.length) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No in-stock windows recorded yet.' }));
        return;
    }
    windows.slice(0, 60).forEach(w => {
        container.appendChild(el('div', {
            class: `flex justify-between items-center gap-3 rounded px-3 py-2 ${w.open ? 'bg-green-900/25 border border-green-700/60' : 'bg-gray-700/60'}`,
        }, [
            el('div', { class: 'min-w-0' }, [
                el('div', { class: 'text-sm text-gray-200 truncate', text: humanizeFqn(w.fqn) }),
                el('div', { class: 'text-xs text-gray-500', text: relativeTime(w.start) }),
            ]),
            el('div', { class: `text-sm font-semibold shrink-0 ${w.open ? 'text-green-400' : 'text-gray-300'}`, text: w.open ? `in stock ${formatDuration(w.durationSec)}` : formatDuration(w.durationSec) }),
        ]));
    });
}

function renderInsightsActivity(eventsDesc) {
    const container = document.getElementById('insights-activity');
    if (!container) return;
    container.innerHTML = '';
    if (!eventsDesc.length) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No stock changes logged yet.' }));
        return;
    }
    eventsDesc.slice(0, 100).forEach(e => {
        const available = e.event_type === 'available';
        container.appendChild(el('div', { class: 'flex justify-between items-center gap-3 px-2 py-1.5 rounded hover:bg-gray-700/40' }, [
            el('div', { class: 'flex items-center gap-2 min-w-0' }, [
                el('span', { class: `w-2 h-2 rounded-full shrink-0 ${available ? 'bg-green-500' : 'bg-gray-600'}` }),
                el('span', { class: 'text-sm text-gray-300 truncate', text: humanizeFqn(e.fqn) }),
            ]),
            el('div', { class: 'flex items-center gap-2 shrink-0' }, [
                el('span', { class: `text-xs ${available ? 'text-green-400' : 'text-gray-500'}`, text: available ? 'restocked' : 'sold out' }),
                el('span', { class: 'text-xs text-gray-500', text: relativeTime(e.timestamp) }),
            ]),
        ]));
    });
}

function renderPriceTrend(planCode, history) {
    const container = document.getElementById('insights-price');
    if (!container) return;
    container.innerHTML = '';
    const refreshBtn = el('button', {
        class: 'bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded text-sm mb-3',
        text: 'Refresh price now',
        onclick: async () => {
            try {
                await apiRequest('POST', `/insights/price/${encodeURIComponent(planCode)}/refresh`);
                showToast('Price refreshed.');
                loadInsightsDetail(planCode, insightsDays());
            } catch (e) {
                showError(e.message);
            }
        },
    });
    container.appendChild(refreshBtn);
    if (!history.length) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No price history yet. Click "Refresh price now" to log the current price.' }));
        return;
    }
    // API returns newest first; chart oldest→newest.
    const rows = [...history].reverse();
    const pts = rows.map(h => ({
        t: new Date(h.timestamp).getTime(),
        v: convertMicrocents(h.price_in_ucents, h.currency_code || state.catalogCurrency),
        label: new Date(h.timestamp).toLocaleString(),
    }));
    const vals = pts.map(p => p.v);
    const min = Math.min(...vals), max = Math.max(...vals);
    if (pts.length >= 2) {
        container.appendChild(buildSparkline(pts, min, max));
    }
    const latest = pts[pts.length - 1].v;
    const stat = el('div', { class: 'flex gap-4 mt-3 text-sm' }, [
        el('div', {}, [el('span', { class: 'text-gray-500', text: 'now ' }), el('span', { class: 'text-blue-300 font-bold', text: formatCurrency(latest) })]),
        el('div', {}, [el('span', { class: 'text-gray-500', text: 'min ' }), el('span', { class: 'text-gray-200', text: formatCurrency(min) })]),
        el('div', {}, [el('span', { class: 'text-gray-500', text: 'max ' }), el('span', { class: 'text-gray-200', text: formatCurrency(max) })]),
    ]);
    container.appendChild(stat);
}

// Inline SVG line chart (no chart library). 2px line, min/max gridlines,
// per-point hover via <title>.
function buildSparkline(pts, min, max) {
    const W = 400, H = 96, padX = 6, padY = 10;
    const span = (max - min) || 1;
    const n = pts.length;
    const x = i => padX + (i / (n - 1)) * (W - 2 * padX);
    const y = v => padY + (1 - (v - min) / span) * (H - 2 * padY);
    const line = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ');
    const area = `${line} L${x(n - 1).toFixed(1)},${(H - padY).toFixed(1)} L${x(0).toFixed(1)},${(H - padY).toFixed(1)} Z`;
    const dots = pts.map((p, i) =>
        `<circle cx="${x(i).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="3.5" fill="#60a5fa"><title>${p.label} — ${formatCurrency(p.v)}</title></circle>`
    ).join('');
    const wrap = el('div', { class: 'w-full' });
    wrap.innerHTML =
        `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none" role="img" aria-label="Price over time">
            <line x1="${padX}" y1="${padY}" x2="${W - padX}" y2="${padY}" stroke="#374151" stroke-width="1" stroke-dasharray="3 3"/>
            <line x1="${padX}" y1="${H - padY}" x2="${W - padX}" y2="${H - padY}" stroke="#374151" stroke-width="1" stroke-dasharray="3 3"/>
            <path d="${area}" fill="rgba(59,130,246,0.12)"/>
            <path d="${line}" fill="none" stroke="#3b82f6" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
            ${dots}
        </svg>`;
    return wrap;
}

// Init

async function init() {
    showView('loading');
    hideError();
    initAudio();

    const configured = await checkHealth();
    state.configured = configured;
    await loadAccounts();

    // Setup wizard: region select drives its manager link + rush region.
    const setupRegion = document.getElementById('setup-region');
    if (setupRegion) {
        setupRegion.addEventListener('change', (e) => updateCredentialsView(e.target.value));
        updateCredentialsView(setupRegion.value);
    }
    // Account editor: region select drives only its own manager link.
    document.getElementById('acct-region')?.addEventListener('change', (e) => {
        updateManagerLink(e.target.value, document.getElementById('acct-manager-link'));
    });

    const accountSelect = document.getElementById('account-select');
    if (accountSelect) {
        accountSelect.addEventListener('change', (e) => {
            if (e.target.value) switchAccount(e.target.value);
        });
    }

    if (!configured) {
        // First run: show the setup wizard only.
        showView('setup');
    } else {
        document.getElementById('settings-btn').classList.remove('hidden');
        renderAccountSelect();
        await loadFxRates();
        await loadAccountInfo();
        await loadAlerts();
        await loadUiPrefs();
        await loadCatalog();
        await loadPollInterval();
        await loadProfiles();
        await loadOrders();
        await loadSniperStatus();
        await loadRegionWatch();
        showView('monitor');
    }

    // Setup wizard
    document.getElementById('setup-save-btn')?.addEventListener('click', saveSetupAccount);

    // Accounts settings page
    document.getElementById('accounts-add-btn')?.addEventListener('click', () => openAccountEditor(null));
    document.getElementById('acct-save-btn')?.addEventListener('click', saveManagedAccount);
    document.getElementById('acct-cancel-btn')?.addEventListener('click', closeAccountEditor);
    document.getElementById('acct-delete-btn')?.addEventListener('click', deleteAccount);

    // Header gear → Accounts page; settings sub-nav + back buttons.
    document.getElementById('settings-btn').addEventListener('click', () => showSettings('accounts'));
    document.querySelectorAll('[data-settings-nav]').forEach(btn => {
        btn.addEventListener('click', () => showSettings(btn.dataset.settingsNav));
    });
    document.querySelectorAll('.settings-back-btn').forEach(btn => {
        btn.addEventListener('click', () => showView('monitor'));
    });

    // Tab switching
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            switchTab(btn.dataset.tab);
        });
    });

    document.getElementById('toggle-monitor-btn').addEventListener('click', () => {
        if (state.monitoring) {
            stopMonitoring();
        } else {
            requestNotificationPermission();
            startMonitoring();
        }
    });

    document.getElementById('poll-interval').addEventListener('change', (e) => {
        const interval = parseInt(e.target.value, 10);
        if (interval) {
            setPollInterval(interval);
        }
    });

    document.getElementById('add-alert-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const planCode = document.getElementById('plan-select').value;
        const fqnPattern = document.getElementById('fqn-pattern').value;
        if (planCode) {
            await addAlert(planCode, fqnPattern);
            document.getElementById('fqn-pattern').value = '';
        }
    });

    document.getElementById('currency-select')?.addEventListener('change', (e) => {
        state.displayCurrency = e.target.value;
        state._currencyUserSet = true;
        updateCurrencyStatus();
        // In 'ovh' mode the fetched subsidiary depends on the selected
        // currency (when the endpoint accepts it), so a currency change may
        // need a re-fetch. In 'fx' mode the catalog is always the endpoint
        // default, so only a re-render is needed (plus rates if converting).
        if (state.priceMode === 'ovh') {
            loadCatalog();
        } else if (!state.fxRates) {
            loadFxRates().then(() => {
                renderCatalogList();
                if (state.selectedPlanCode) {
                    const p = state.plans.find(x => x.planCode === state.selectedPlanCode);
                    if (p) renderCatalogDetail(p);
                }
            });
        } else {
            renderCatalogList();
            if (state.selectedPlanCode) {
                const p = state.plans.find(x => x.planCode === state.selectedPlanCode);
                if (p) renderCatalogDetail(p);
            }
        }
    });

    document.getElementById('price-mode-ovh')?.addEventListener('change', (e) => {
        // Checked = convert pricing to the selected currency (fx); unchecked
        // = show OVH's native catalog currency (ovh).
        state.priceMode = e.target.checked ? 'fx' : 'ovh';
        updateCurrencyStatus();
        // Re-fetch only if the source subsidiary actually changes with the
        // new mode; otherwise just re-render in the new effective currency
        // (avoids a needless OVH round-trip on the common CA/USD toggle).
        const newSub = subsidiaryForMode(state.priceMode);
        if (newSub !== state.catalogCountry) {
            if (state.priceMode === 'fx' && !state.fxRates) {
                loadFxRates().then(() => loadCatalog());
            } else {
                loadCatalog();
            }
        } else {
            renderCatalogList();
            if (state.selectedPlanCode) {
                const p = state.plans.find(x => x.planCode === state.selectedPlanCode);
                if (p) renderCatalogDetail(p);
            }
        }
    });

    document.getElementById('catalog-search')?.addEventListener('input', renderCatalogList);
    document.getElementById('catalog-sort')?.addEventListener('change', renderCatalogList);
    document.getElementById('catalog-location-filter')?.addEventListener('change', renderCatalogList);
    document.getElementById('catalog-orderable-filter')?.addEventListener('change', renderCatalogList);
    document.getElementById('catalog-stock-first')?.addEventListener('change', renderCatalogList);

    document.getElementById('catalog-refresh-btn')?.addEventListener('click', () => {
        loadCatalog(null, true);
    });

    // Orders tab listeners
    document.getElementById('orders-filter')?.addEventListener('change', (e) => {
        state.ordersFilter = e.target.value;
        renderOrdersList();
    });
    document.getElementById('orders-refresh-btn')?.addEventListener('click', () => {
        loadOrdersTab(true);
    });

    // Logs tab filters + controls (client-side; no re-fetch needed).
    document.getElementById('logs-level')?.addEventListener('change', (e) => {
        state.logsFilter.level = e.target.value;
        renderLogsList();
    });
    document.getElementById('logs-source')?.addEventListener('change', (e) => {
        state.logsFilter.source = e.target.value;
        renderLogsList();
    });
    document.getElementById('logs-search')?.addEventListener('input', (e) => {
        state.logsFilter.search = e.target.value;
        renderLogsList();
    });
    document.getElementById('logs-pause-btn')?.addEventListener('click', (e) => {
        state.logsPaused = !state.logsPaused;
        e.target.textContent = state.logsPaused ? '▶ Resume' : '⏸ Pause';
        if (!state.logsPaused) renderLogsList();
    });
    document.getElementById('logs-clear-btn')?.addEventListener('click', () => {
        state.logsBuffer = [];
        renderLogsList();
    });

    document.getElementById('catalog-autorefresh')?.addEventListener('change', (e) => {
        if (e.target.checked) {
            const interval = parseInt(document.getElementById('catalog-refresh-interval').value, 10);
            startCatalogAutoRefresh(interval);
        } else {
            stopCatalogAutoRefresh();
        }
    });
    document.getElementById('catalog-refresh-interval')?.addEventListener('change', () => {
        if (document.getElementById('catalog-autorefresh').checked) {
            const interval = parseInt(document.getElementById('catalog-refresh-interval').value, 10);
            stopCatalogAutoRefresh();
            startCatalogAutoRefresh(interval);
        }
    });

    // Checkout defaults form
    document.getElementById('checkout-defaults-form')?.addEventListener('submit', saveCheckoutDefaults);

    document.getElementById('notif-save-btn')?.addEventListener('click', saveNotificationSettings);
    document.getElementById('notif-test-email-btn')?.addEventListener('click', sendTestEmail);
    document.getElementById('app-settings-save-btn')?.addEventListener('click', saveAppSettings);

    document.getElementById('rush-order-btn').addEventListener('click', () => {
        document.getElementById('rush-submit-btn').click();
    });

    // User input into the rush target fields clears the autofill tag so
    // later stock alerts stop overwriting them (see showStockAlert).
    for (const id of ['rush-plan-code', 'rush-fqn']) {
        document.getElementById(id).addEventListener('input', (e) => {
            delete e.target.dataset.autofilled;
        });
    }
    // Keep the DC/OS options in sync with the typed plan code.
    document.getElementById('rush-plan-code').addEventListener('change', (e) => {
        updateRushConfigOptionsForPlanCode(e.target.value.trim());
    });
    // Build the fallback DC/OS lists before any plan is known.
    updateRushConfigOptions(null);
    document.getElementById('use-alert-config-btn').addEventListener('click', applyAlertConfigToRushForm);

    document.getElementById('region-ticker-toggle').addEventListener('change', (e) => {
        setRegionTicker(e.target.checked);
    });

    document.getElementById('servers-refresh-btn')?.addEventListener('click', () => loadServersTab());

    document.getElementById('rush-order-form').addEventListener('submit', rushOrder);

    document.getElementById('back-to-monitor-btn').addEventListener('click', () => {
        showView('monitor');
        // Only (re)start monitoring if it was already active - don't force
        // it on for a user who placed an order via the catalog's inline
        // "Order Now" flow without ever enabling it.
        if (state.monitoring) startMonitoring();
    });

    document.getElementById('sound-toggle').addEventListener('change', (e) => {
        if (e.target.checked) {
            unlockAudio();
        }
    });

    // Saved profiles
    document.getElementById('load-profile-btn')?.addEventListener('click', () => {
        loadProfileIntoForm(document.getElementById('profile-select').value);
    });
    document.getElementById('save-profile-btn')?.addEventListener('click', saveProfile);
    document.getElementById('delete-profile-btn')?.addEventListener('click', deleteProfile);

    // Sniper mode
    document.getElementById('sniper-arm-btn')?.addEventListener('click', armSniper);
    document.getElementById('sniper-disarm-btn')?.addEventListener('click', disarmSniper);

    // Insights tab
    document.getElementById('insights-plan-select')?.addEventListener('change', loadInsightsData);
    document.getElementById('insights-days')?.addEventListener('change', loadInsightsData);
    document.getElementById('price-watch-save-btn')?.addEventListener('click', savePriceWatch);
}

document.addEventListener('DOMContentLoaded', init);
