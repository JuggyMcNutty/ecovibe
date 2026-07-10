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
    cart: null,
    cartCreatedAt: null,
    orderResult: null,
    billingLoaded: false,
    checkoutDefaults: null,
    addonPrices: {},
    productSpecs: {},
    stockByPlan: {},
    notifSettingsLoaded: false,
    // Orders tab state.
    allOrders: [],
    selectedOrderId: null,
    ordersFilter: 'all',
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

function displayPriceUnits(microcents, fromCode = state.catalogCurrency) {
    // Returns just the converted numeric units (for totals arithmetic display).
    if (microcents == null) return 0;
    return convertMicrocents(microcents, fromCode, effectiveDisplayCurrency());
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
    state.selectedPlanCode = null;
    state.stockByPlan = {};
    state.allOrders = [];
    state.selectedOrderId = null;
    state.cart = null;
    state.cartCreatedAt = null;
    state.orderResult = null;
    state.plans = [];
    state.addonPrices = {};
    state.productSpecs = {};
    state.catalogCountry = null;
    // Clear detail panels so stale content doesn't linger.
    const catDetail = document.getElementById('catalog-detail');
    if (catDetail) catDetail.innerHTML = '<p class="text-gray-500 text-sm">Select a plan to see details.</p>';
    const ordDetail = document.getElementById('order-detail');
    if (ordDetail) ordDetail.innerHTML = '<p class="text-gray-500 text-sm">Select an order to see details.</p>';

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
        populateCatalogCountries();
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
        await loadSniperStatus();
        if (gen !== state._switchGen) return;
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
    const container = document.getElementById('account-list');
    if (!container) return;
    container.innerHTML = '';
    if (!state.accounts.length) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No accounts yet. Add one below.' }));
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
                    onclick: () => editAccount(a.id),
                }),
            ]),
        ]);
        container.appendChild(card);
    });
}

function editAccount(accountId) {
    const a = state.accounts.find(x => x.id === accountId);
    if (!a) return;
    state.editingAccountId = accountId;
    document.getElementById('setup-title').textContent = 'Edit Account';
    document.getElementById('setup-description').textContent = `Editing "${a.label}". Leave the secret blank to keep the stored one.`;
    document.getElementById('cred-label').value = a.label;
    document.getElementById('ovh-region-select').value = a.endpoint;
    document.getElementById('cred-app-key').value = '';
    document.getElementById('cred-app-secret').value = '';
    document.getElementById('cred-consumer-key').value = '';
    document.getElementById('delete-credentials-btn').classList.remove('hidden');
    document.getElementById('cred-test-result').classList.add('hidden');
    updateCredentialsView(a.endpoint);
}

function resetAccountForm() {
    state.editingAccountId = null;
    document.getElementById('setup-title').textContent = 'Add OVH Account';
    document.getElementById('setup-description').textContent = 'Add an OVH API account to monitor flash sales and place orders. Credentials are stored in the local database.';
    document.getElementById('cred-label').value = '';
    document.getElementById('ovh-region-select').value = 'ovh-eu';
    document.getElementById('cred-app-key').value = '';
    document.getElementById('cred-app-secret').value = '';
    document.getElementById('cred-consumer-key').value = '';
    document.getElementById('delete-credentials-btn').classList.add('hidden');
    document.getElementById('cred-test-result').classList.add('hidden');
    updateCredentialsView('ovh-eu');
}

// Catalog

const SUBSIDIARIES_BY_ENDPOINT = {
    'ovh-eu': ['IE', 'FR', 'DE', 'GB', 'ES', 'PL', 'IT', 'PT', 'CZ', 'FI'],
    'ovh-us': ['US'],
    'ovh-ca': ['CA'],
};

function populateCatalogCountries() {
    // The per-country dropdown was replaced by the currency selector.
    // Kept as a no-op so existing call sites don't need updating.
}

function catalogSubsidiaryForCurrency() {
    // Delegate to subsidiaryForMode() so the fetch and the display mode
    // agree on which subsidiary's catalog is the source of truth.
    return subsidiaryForMode(state.priceMode);
}

async function loadCatalog(country, force = false) {
    showLoading();
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
        renderCatalogList();
        if (state.selectedPlanCode) {
            const p = state.plans.find(x => x.planCode === state.selectedPlanCode);
            if (p) renderCatalogDetail(p);
        }
        // Fetch stock levels for all plans and re-render with badges.
        // Awaited so the loading overlay stays visible until stock data
        // is ready — otherwise the list shows without OOS badges briefly.
        await refreshStockForAllPlans();
        renderCatalogList();
    } catch (e) {
        showError(e.message);
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
    try {
        const url = `/catalog/plans?country=${encodeURIComponent(state.catalogCountry)}`;
        const resp = await apiRequest('GET', url);
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
        refreshStockForAllPlans().then(() => renderCatalogList()).catch(() => {});
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
                stockByPlan[pc] = hasAvailable;
            } catch (e) {
                console.warn(`Stock fetch failed for ${pc}, assuming in-stock:`, e);
                stockByPlan[pc] = true;
            }
        }
    }
    await Promise.all(Array.from({ length: CONCURRENCY }, fetchOne));
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
const REGION_LABELS = {
    'eu': 'Europe',
    'us': 'US',
    'ca': 'Canada',
    'sgp': 'Singapore',
    'syd': 'Sydney',
    'lon': 'London',
};

function planRegion(planCode) {
    // planCode looks like "24sk102-ca" → extract "ca" → "Canada"
    const parts = (planCode || '').split('-');
    if (parts.length > 1) {
        const suffix = parts[parts.length - 1].toLowerCase();
        if (REGION_LABELS[suffix]) return REGION_LABELS[suffix];
        return suffix.toUpperCase();
    }
    return '';
}

function planLabel(plan) {
    const name = plan.invoiceName || plan.planCode;
    const region = planRegion(plan.planCode);
    return region ? `${name} [${region}]` : name;
}

function renderPlanSelect() {
    const select = document.getElementById('plan-select');
    select.innerHTML = '';
    select.appendChild(el('option', { value: '', text: 'Select a plan...' }));
    state.plans.forEach(plan => {
        if (!planMatchesEndpoint(plan.planCode, state.endpoint)) return;
        const opt = el('option', { value: plan.planCode, text: planLabel(plan) });
        select.appendChild(opt);
    });
}

// Map OVH endpoint -> region suffixes that can be ordered on that endpoint.
// Plans with no region suffix are orderable on any endpoint.
const ENDPOINT_REGION_SUFFIXES = {
    'ovh-eu': ['eu', 'fr', 'de', 'gb', 'es', 'pl', 'it', 'pt', 'cz', 'fi', 'ie'],
    'ovh-us': ['us'],
    'ovh-ca': ['ca'],
};

function planMatchesEndpoint(planCode, endpoint) {
    const suffixes = ENDPOINT_REGION_SUFFIXES[endpoint] || ENDPOINT_REGION_SUFFIXES['ovh-eu'];
    const parts = (planCode || '').split('-');
    if (parts.length <= 1) return true;
    const suffix = parts[parts.length - 1].toLowerCase();
    return suffixes.includes(suffix);
}

function getFilteredPlans() {
    const q = (document.getElementById('catalog-search')?.value || '').trim().toLowerCase();
    const sort = document.getElementById('catalog-sort')?.value || 'default';
    const regionFilter = document.getElementById('catalog-region-filter')?.checked;
    const stockFirst = document.getElementById('catalog-stock-first')?.checked;
    let plans = state.plans.slice();
    if (regionFilter) {
        plans = plans.filter(p => planMatchesEndpoint(p.planCode, state.endpoint));
    }
    if (q) {
        plans = plans.filter(p =>
            (p.invoiceName || '').toLowerCase().includes(q) ||
            (p.planCode || '').toLowerCase().includes(q)
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
            return ps.cpu?.score ?? 0;
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
    const plans = getFilteredPlans().slice(0, 100);
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
        const region = planRegion(plan.planCode);
        const regionSpan = region ? el('span', { class: 'text-yellow-400 ml-1 text-xs', text: `[${region}]` }) : null;
        const code = el('span', { class: 'text-gray-400 ml-2 text-xs', text: plan.planCode });
        const left = el('div', {}, [name, stockBadge, regionSpan, code].filter(Boolean));
        const price = el('span', {
            class: inStock ? 'text-green-400 text-sm' : 'text-gray-600 text-sm line-through',
            text: priceText,
        });

        const isSelected = state.selectedPlanCode === plan.planCode;
        const div = el('div', {
            class: [
                'rounded p-2 text-sm flex justify-between items-center cursor-pointer transition-colors',
                inStock ? 'hover:bg-gray-600' : 'hover:bg-gray-600 opacity-60',
                isSelected
                    ? 'bg-blue-600/40 border border-blue-500'
                    : inStock
                        ? 'bg-gray-700'
                        : 'bg-gray-700/50',
            ].join(' '),
            role: 'button',
            tabindex: '0'
        }, [left, price]);

        const selectPlan = () => {
            document.getElementById('plan-select').value = plan.planCode;
            renderCatalogDetail(plan);
            // Re-render the list to update the highlight on the newly
            // selected row. This is cheap (100 rows max) and keeps the
            // highlight in sync without a full catalog refetch.
            const prev = container.querySelector('.bg-blue-600\\/40');
            if (prev) {
                prev.classList.remove('bg-blue-600/40', 'border', 'border-blue-500');
                prev.classList.add(inStock ? 'bg-gray-700' : 'bg-gray-700/50');
            }
            div.classList.remove(inStock ? 'bg-gray-700' : 'bg-gray-700/50');
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
    return code.replace(/(\d+)(nvme|sa|sas|hdd)/gi, (m, num, unit) => {
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

function renderCatalogDetail(plan) {
    state.selectedPlanCode = plan.planCode;
    const container = document.getElementById('catalog-detail');
    container.innerHTML = '';

    const monthly = getPlanMonthlyPrice(plan);
    const setup = getPlanSetupFee(plan);
    const priceText = displayPrice(monthly?.price, monthly?.formattedPrice, monthly?.currencyCode || state.catalogCurrency);
    const setupText = displayPrice(setup?.price, setup?.formattedPrice, setup?.currencyCode || state.catalogCurrency);
    const region = planRegion(plan.planCode);

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
    if (cpu && cpu.score) {
        specBadges.push(`CPU score: ${cpu.score.toLocaleString()}`);
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
    if (region) {
        container.appendChild(el('span', { class: 'inline-block bg-yellow-600/30 text-yellow-400 text-xs px-2 py-1 rounded mb-2', text: region }));
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
    const families = (plan.addonFamilies || []).filter(f => HARDWARE_FAMILIES.has(f.name));
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

    // Fetch stock data asynchronously (don't block rendering)
    apiRequest('GET', `/catalog/stock?plan_code=${encodeURIComponent(plan.planCode)}`)
        .then(data => {
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
                state.orderResult = result;
                state.cart = null;
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
    // Lazy-load billing data when switching to that tab
    if (tabId === 'billing-tab' && !state.billingLoaded) {
        loadBillingInfo();
    }
    // Lazy-load orders data when switching to the orders tab
    if (tabId === 'orders-tab') {
        loadOrdersTab();
    }
    // Refresh insights plan dropdown when switching to that tab
    if (tabId === 'insights-tab') {
        populateInsightsPlanSelect();
    }
}

// Billing & account info

async function loadBillingInfo() {
    await Promise.all([loadAccountInfo(), loadPaymentMethods(), loadCheckoutDefaults()]);
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
        state.notifSettingsLoaded = true;
    } catch (e) {
        console.error('Failed to load notification settings:', e);
    }
}

async function saveNotificationSettings() {
    try {
        const body = {
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
        await apiRequest('PUT', '/settings/notifications', body);
        showToast('Notification settings saved.');
        await loadNotificationSettings();
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

async function loadPaymentMethods() {
    const container = document.getElementById('payment-methods');
    if (!container) return;
    container.innerHTML = '';
    try {
        const data = await apiRequest('GET', '/account/payment-methods');
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
    try {
        const defaults = await apiRequest('GET', '/account/checkout-defaults');
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
    } catch (e) {
        console.error('Failed to load alerts:', e);
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
                await apiRequest('PUT', `/alerts/${encodeURIComponent(alert.id)}/${alert.enabled ? 'disable' : 'enable'}`);
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

function renderMonitoredList() {
    const container = document.getElementById('monitored-list');
    container.innerHTML = '';
    if (state.alerts.length === 0) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No servers being monitored' }));
        return;
    }
    state.alerts.forEach(alert => {
        const stock = state.currentStock[alert.plan_code];
        const available = stock && stock.length > 0;
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
    state.recentAlerts.slice(0, 5).forEach(alert => {
        const time = new Date(alert.timestamp).toLocaleTimeString();
        const code = el('span', { class: 'text-red-400 font-bold', text: alert.planCode });
        const t = el('span', { class: 'text-gray-400 ml-2 text-xs', text: time });
        const fqns = el('p', { class: 'text-sm', text: (alert.fqns || []).join(', ') });
        const row = el('div', { class: 'bg-red-900/30 border border-red-700 rounded p-2' }, [code, t, fqns]);
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
    } catch (e) {
        showError(e.message);
    }
}

async function loadPollInterval() {
    try {
        const status = await apiRequest('GET', '/monitor/status');
        if (status && status.poll_interval) {
            document.getElementById('poll-interval').value = String(status.poll_interval);
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
        updateConnectionStatus(true);
    };

    state.eventSource.onmessage = async (event) => {
        try {
            const data = JSON.parse(event.data);

            if (data.type === 'stock_update') {
                data.changes.forEach(change => {
                    if (change.newly_available.length > 0) {
                        state.currentStock[change.plan_code] = change.currently_available.map(fqn => ({ fqn }));

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
            state.reconnectTimer = setTimeout(() => {
                state.reconnectTimer = null;
                if (state.monitoring) {
                    startMonitoring();
                }
            }, 3000);
        }
    };
}

function stopMonitoring() {
    state.monitoring = false;
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

    document.getElementById('rush-plan-code').value = planCode;
    document.getElementById('rush-fqn').value = fqns[0];

    if (alertPanelTimer) {
        clearTimeout(alertPanelTimer);
    }
    alertPanelTimer = setTimeout(() => {
        panel.classList.add('hidden');
        alertPanelTimer = null;
    }, 30000);
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

        state.orderResult = result;
        state.cart = null;
        state.cartCreatedAt = null;

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

// Credentials

function updateCredentialsView(region) {
    const regionInfo = OVH_REGIONS[region] || OVH_REGIONS['ovh-eu'];
    const link = document.getElementById('create-api-key-link');
    if (link) {
        link.href = regionInfo.managerUrl;
        link.textContent = `Open ${regionInfo.name} OVHcloud Manager`;
    }

    const rushRegion = document.getElementById('rush-region');
    if (rushRegion) {
        rushRegion.value = regionInfo.rushRegion;
    }
}

async function saveCredentials() {
    const endpoint = document.getElementById('ovh-region-select').value;
    const label = document.getElementById('cred-label').value.trim() || endpoint;
    const applicationKey = document.getElementById('cred-app-key').value.trim();
    const applicationSecret = document.getElementById('cred-app-secret').value.trim();
    const consumerKey = document.getElementById('cred-consumer-key').value.trim();
    const editingId = state.editingAccountId;

    if (!applicationKey || !consumerKey) {
        showCredentialTestResult('error', 'Application key and consumer key are required.');
        return;
    }
    if (!editingId && !applicationSecret) {
        showCredentialTestResult('error', 'Application secret is required for a new account.');
        return;
    }

    showCredentialTestResult('loading', 'Saving account...');
    try {
        const body = { label, endpoint, application_key: applicationKey, application_secret: applicationSecret, consumer_key: consumerKey };
        let savedId;
        if (editingId) {
            await apiRequest('PUT', `/accounts/${editingId}`, body);
            savedId = editingId;
        } else {
            const created = await apiRequest('POST', '/accounts', body);
            savedId = created.id;
        }
        await loadAccounts();
        state.activeAccountId = savedId;
        state.endpoint = endpoint;
        state.configured = true;

        // Test the saved account.
        try {
            const result = await apiRequest('POST', `/accounts/${savedId}/test`);
            showCredentialTestResult('success',
                `Connected as ${result.firstname || ''} ${result.name || ''} (${result.nichandle || 'unknown'})`);
        } catch (e) {
            showCredentialTestResult('error', `Account saved but test failed: ${e.message}`);
        }

        // After 1.2s, proceed to the monitor (or back to account list in manage mode).
        setTimeout(async () => {
            document.getElementById('settings-btn').classList.remove('hidden');
            renderAccountSelect();
            populateCatalogCountries();
            await loadAlerts();
            await loadCatalog();
            await loadPollInterval();
            await loadProfiles();
            await loadOrders();
            await loadSniperStatus();
            showView('monitor');
        }, 1200);
    } catch (e) {
        showCredentialTestResult('error', e.message);
    }
}

async function deleteCredentials() {
    const editingId = state.editingAccountId;
    if (!editingId) return;
    if (!confirm('Delete this account? Its alerts and profiles remain but become unscoped.')) {
        return;
    }
    try {
        await apiRequest('DELETE', `/accounts/${editingId}`);
        await loadAccounts();
        state.editingAccountId = null;
        // Active account may have changed (fallback); refresh health-derived state.
        await checkHealth();
        renderAccountList();
        renderAccountSelect();
        resetAccountForm();
        showCredentialTestResult('success', 'Account deleted.');
    } catch (e) {
        showCredentialTestResult('error', e.message);
    }
}

function showCredentialTestResult(type, message) {
    const div = document.getElementById('cred-test-result');
    div.classList.remove('hidden', 'bg-green-900/50', 'border-green-700', 'text-green-300',
                         'bg-red-900/50', 'border-red-700', 'text-red-300',
                         'bg-blue-900/50', 'border-blue-700', 'text-blue-300');
    if (type === 'success') {
        div.className = 'rounded p-3 text-sm bg-green-900/50 border border-green-700 text-green-300';
    } else if (type === 'error') {
        div.className = 'rounded p-3 text-sm bg-red-900/50 border border-red-700 text-red-300';
    } else {
        div.className = 'rounded p-3 text-sm bg-blue-900/50 border border-blue-700 text-blue-300';
    }
    div.textContent = message;
}

async function loadExistingCredentials() {
    // Manage-mode: render the account list and reset the form for adding.
    await loadAccounts();
    renderAccountList();
    resetAccountForm();
}

// ----- Saved checkout profiles -----

async function loadProfiles() {
    try {
        const profiles = await apiRequest('GET', '/profiles') || [];
        state.profiles = profiles;
        renderProfileSelect();
        renderSniperProfileSelect();
    } catch (e) {
        console.error('Failed to load profiles:', e);
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

async function loadProfileIntoForm(profileId) {
    if (!profileId) return;
    const profile = state.profiles?.find(p => p.id === profileId);
    if (!profile) return;
    document.getElementById('rush-plan-code').value = profile.plan_code || '';
    document.getElementById('rush-fqn').value = profile.fqn || '';
    if (profile.ram) document.getElementById('rush-ram').value = profile.ram;
    if (profile.storage) document.getElementById('rush-storage').value = profile.storage;
    if (profile.bandwidth) document.getElementById('rush-bandwidth').value = profile.bandwidth;
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
        await apiRequest('POST', '/profiles', profile);
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

async function loadSniperStatus() {
    const container = document.getElementById('sniper-status');
    if (!container) return;
    try {
        const status = await apiRequest('GET', '/sniper/status');
        if (!status) return;
        const armed = status.armed || [];
        const results = status.results || {};
        if (armed.length === 0 && Object.keys(results).length === 0) {
            container.textContent = 'No sniper armed.';
            return;
        }
        container.innerHTML = '';
        armed.forEach(a => {
            const text = `Armed: ${a.plan_code || a.alert_id} -> profile ${a.profile_id.slice(0, 8)}`;
            container.appendChild(el('div', { class: 'text-yellow-400', text }));
        });
        for (const [aid, r] of Object.entries(results)) {
            const cls = r.status === 'ordered' ? 'text-green-400' : 'text-red-400';
            const text = `Result: ${aid.slice(0, 8)} - ${r.status}${r.order_id ? ` (#${r.order_id})` : ''}`;
            container.appendChild(el('div', { class: cls, text }));
        }
    } catch (e) {
        container.textContent = 'Failed to load sniper status';
    }
}

// ----- Orders -----

async function loadOrders() {
    try {
        const data = await apiRequest('GET', '/insights/orders');
        renderOrders(data?.orders || []);
    } catch (e) {
        console.error('Failed to load orders:', e);
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
        const status = o.status || 'unknown';
        const head = el('div', { class: 'flex justify-between items-center' }, [
            el('div', {}, [
                el('span', { class: 'text-blue-400 font-bold', text: `${o.plan_code} ${id}` }),
                el('span', { class: 'text-gray-400 ml-2 text-xs', text: time }),
            ]),
            o.order_id ? el('button', {
                class: 'text-xs bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded',
                text: 'Refresh',
                onclick: async (ev) => {
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
            }) : null,
        ]);
        const st = el('span', { class: 'text-xs text-gray-400', text: `status: ${status}` });
        container.appendChild(el('div', { class: 'bg-gray-700 rounded p-2' }, [head, st]));
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

async function loadOrdersTab() {
    const container = document.getElementById('orders-full-list');
    if (!container) return;
    container.innerHTML = '';
    container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'Loading orders from OVH...' }));
    try {
        const data = await apiRequest('GET', '/orders?limit=50&days=90');
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
                el('span', { class: 'text-blue-400 font-bold text-sm', text: o.server_name || o.plan_code || '(unknown)' }),
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

    // Line items
    if (details.length) {
        const itemsSection = el('div', { class: 'mb-4' });
        itemsSection.appendChild(el('h4', { class: 'font-bold text-gray-400 text-sm uppercase mb-2', text: 'Line Items' }));
        for (const d of details) {
            const dTotal = d.totalPrice?.text || '';
            itemsSection.appendChild(el('div', { class: 'flex justify-between items-center bg-gray-700 rounded px-3 py-2 mb-1' }, [
                el('div', { class: 'min-w-0 flex-1' }, [
                    el('span', { class: 'text-gray-200 text-sm', text: d.description || d.domain || '(line item)' }),
                    d.detailType ? el('span', { class: 'text-gray-500 ml-2 text-xs', text: d.detailType }) : null,
                ]),
                el('span', { class: 'text-gray-400 text-xs whitespace-nowrap', text: dTotal }),
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
    select.appendChild(el('option', { value: '', text: 'Select a monitored plan...' }));
    const codes = sortedMonitoredPlanCodes();
    codes.forEach(code => {
        select.appendChild(el('option', { value: code, text: code }));
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

async function loadInsightsData() {
    const planCode = document.getElementById('insights-plan-select').value;
    const days = parseInt(document.getElementById('insights-days').value, 10) || 30;
    if (!planCode) {
        document.getElementById('restock-pattern').innerHTML = '';
        document.getElementById('restock-pattern').appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'Select a plan to see restock patterns by hour.' }));
        document.getElementById('price-history').innerHTML = '';
        document.getElementById('price-history').appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'Select a plan to see price history.' }));
        document.getElementById('stock-events').innerHTML = '';
        document.getElementById('stock-events').appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'Select a plan to see recent stock events.' }));
        return;
    }
    await Promise.all([
        loadRestockPatterns(planCode, days),
        loadPriceHistoryView(planCode),
        loadStockEvents(planCode, days),
    ]);
}

async function loadRestockPatterns(planCode, days) {
    const container = document.getElementById('restock-pattern');
    if (!container) return;
    container.innerHTML = '';
    try {
        const data = await apiRequest('GET', `/insights/patterns/${encodeURIComponent(planCode)}?days=${days}`);
        const counts = data?.hourly_counts || [];
        if (!counts.length) {
            container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No restock events logged for this plan yet.' }));
            return;
        }
        const max = Math.max(...counts.map(c => c.count), 1);
        const byHour = Object.fromEntries(counts.map(c => [c.hour, c.count]));
        const grid = el('div', { class: 'grid grid-cols-12 gap-1' });
        for (let h = 0; h < 24; h++) {
            const c = byHour[h] || 0;
            const heightPct = Math.round((c / max) * 100);
            const bar = el('div', {
                class: 'bg-blue-600 rounded-t',
                style: `height: ${Math.max(heightPct, 4)}%; min-height: 4px;`,
                title: `${h}:00 - ${c} events`,
            });
            const cell = el('div', { class: 'flex flex-col items-center justify-end h-16' }, [
                bar,
                el('span', { class: 'text-gray-500 text-xs mt-1', text: `${h}` }),
            ]);
            grid.appendChild(cell);
        }
        container.appendChild(grid);
        container.appendChild(el('p', { class: 'text-gray-400 text-xs mt-2', text: `Bars show restock count per hour over the last ${days} days (UTC). Peak hour: ${peakHour(counts)}:00 with ${max} events.` }));
    } catch (e) {
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

function peakHour(counts) {
    if (!counts.length) return '—';
    return counts.reduce((a, b) => (b.count > a.count ? b : a)).hour;
}

async function loadPriceHistoryView(planCode) {
    const container = document.getElementById('price-history');
    if (!container) return;
    container.innerHTML = '';
    try {
        const data = await apiRequest('GET', `/insights/price/${encodeURIComponent(planCode)}`);
        const history = data?.history || [];
        const refreshBtn = el('button', {
            class: 'bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded text-sm mb-3',
            text: 'Refresh price now',
            onclick: async () => {
                try {
                    await apiRequest('POST', `/insights/price/${encodeURIComponent(planCode)}/refresh`);
                    showToast('Price refreshed.');
                    await loadPriceHistoryView(planCode);
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
        // Simple text table; no chart library in the project.
        const list = el('div', { class: 'space-y-1' });
        history.slice(0, 20).forEach(h => {
            const time = new Date(h.timestamp).toLocaleString();
            const price = formatCurrency(convertMicrocents(h.price_in_ucents));
            list.appendChild(el('div', { class: 'flex justify-between bg-gray-700 rounded px-2 py-1 text-sm' }, [
                el('span', { class: 'text-gray-400', text: time }),
                el('span', { class: 'text-green-400 font-bold', text: price }),
            ]));
        });
        container.appendChild(list);
    } catch (e) {
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

async function loadStockEvents(planCode, days) {
    const container = document.getElementById('stock-events');
    if (!container) return;
    container.innerHTML = '';
    try {
        const data = await apiRequest('GET', `/insights/history/${encodeURIComponent(planCode)}?days=${days}`);
        const events = data?.events || [];
        if (!events.length) {
            container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No stock events logged for this plan yet.' }));
            return;
        }
        events.slice(0, 200).forEach(e => {
            const time = new Date(e.timestamp).toLocaleString();
            const available = e.event_type === 'available';
            container.appendChild(el('div', {
                class: `flex justify-between rounded px-2 py-1 text-sm ${available ? 'bg-green-900/30 border border-green-700' : 'bg-gray-700'}`,
            }, [
                el('span', { class: 'text-gray-300 font-mono text-xs', text: e.fqn }),
                el('span', { class: `text-xs ${available ? 'text-green-400' : 'text-gray-500'}`, text: `${e.event_type} · ${time}` }),
            ]));
        });
    } catch (e) {
        container.appendChild(el('p', { class: 'text-red-400 text-sm', text: `Error: ${e.message}` }));
    }
}

// Init

async function init() {
    showView('loading');
    hideError();
    initAudio();

    const configured = await checkHealth();
    state.configured = configured;
    await loadAccounts();

    const regionSelect = document.getElementById('ovh-region-select');
    if (regionSelect) {
        regionSelect.addEventListener('change', (e) => {
            updateCredentialsView(e.target.value);
        });
        updateCredentialsView(regionSelect.value);
    }

    const accountSelect = document.getElementById('account-select');
    if (accountSelect) {
        accountSelect.addEventListener('change', (e) => {
            if (e.target.value) switchAccount(e.target.value);
        });
    }

    if (!configured) {
        // First-start: only show the add-account wizard,
        // hide account list, notification settings + back button.
        resetAccountForm();
        document.getElementById('account-list-block')?.classList.add('hidden');
        document.getElementById('notif-settings-block')?.classList.add('hidden');
        document.getElementById('credentials-back-block')?.classList.add('hidden');
        document.getElementById('add-account-btn')?.classList.add('hidden');
        showView('credentials');
    } else {
        document.getElementById('settings-btn').classList.remove('hidden');
        renderAccountSelect();
        populateCatalogCountries();
        await loadFxRates();
        await loadAccountInfo();
        await loadAlerts();
        await loadCatalog();
        await loadPollInterval();
        await loadProfiles();
        await loadOrders();
        await loadSniperStatus();
        showView('monitor');
    }

    document.getElementById('save-credentials-btn').addEventListener('click', saveCredentials);
    document.getElementById('delete-credentials-btn').addEventListener('click', deleteCredentials);
    document.getElementById('skip-credentials-btn').addEventListener('click', () => {
        // Skip to monitor (will show 503s for OVH calls until configured)
        showView('monitor');
    });
    document.getElementById('add-account-btn')?.addEventListener('click', () => {
        resetAccountForm();
    });

    document.getElementById('settings-btn').addEventListener('click', () => {
        // Manage-mode: show account list, notification settings + back button.
        document.getElementById('account-list-block')?.classList.remove('hidden');
        document.getElementById('notif-settings-block')?.classList.remove('hidden');
        document.getElementById('credentials-back-block')?.classList.remove('hidden');
        document.getElementById('add-account-btn')?.classList.remove('hidden');
        showView('credentials');
        loadExistingCredentials();
        loadNotificationSettings();
    });

    document.getElementById('back-from-settings-btn')?.addEventListener('click', () => {
        showView('monitor');
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
    document.getElementById('catalog-region-filter')?.addEventListener('change', renderCatalogList);
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
        loadOrdersTab();
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

    document.getElementById('rush-order-btn').addEventListener('click', () => {
        document.getElementById('rush-submit-btn').click();
    });

    document.getElementById('rush-order-form').addEventListener('submit', rushOrder);

    document.getElementById('back-to-monitor-btn').addEventListener('click', () => {
        state.cart = null;
        state.cartCreatedAt = null;
        showView('monitor');
        startMonitoring();
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
}

document.addEventListener('DOMContentLoaded', init);
