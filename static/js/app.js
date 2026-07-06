// ===========================================================================
// OVH Flash Sale Monitor — frontend SPA logic (vanilla JS, no framework)
// ===========================================================================
// Structure:
//   1. Constants & state
//   2. DOM helpers (el, showView, loading, error banner, connection dot)
//   3. API client (apiRequest, checkHealth)
//   4. Catalog (load, search, filter, render)
//   5. Alerts (CRUD, render lists)
//   6. SSE monitoring (start/stop, stock alerts, reconnect)
//   7. Browser notifications & audio
//   8. Rush order (one-shot POST /api/checkout/rush)
//   9. Credentials view
//  10. Saved checkout profiles
//  11. Sniper mode (arm/disarm/status)
//  12. Orders list
//  13. Init (DOMContentLoaded)
// ===========================================================================

// --- 1. Constants & state ---------------------------------------------------

const API_BASE = '/api';

const OVH_REGIONS = {
    'ovh-eu': {
        name: 'Europe',
        createAppUrl: 'https://eu.api.ovh.com/createApp/',
        createTokenUrl: 'https://eu.api.ovh.com/createToken/',
        apiEndpoint: 'https://eu.api.ovh.com/v1',
        rushRegion: 'europe'
    },
    'ovh-us': {
        name: 'United States',
        createAppUrl: 'https://api.us.ovhcloud.com/createApp/',
        createTokenUrl: 'https://api.us.ovhcloud.com/createToken/',
        apiEndpoint: 'https://api.us.ovhcloud.com/v1',
        rushRegion: 'us'
    },
    'ovh-ca': {
        name: 'Canada',
        createAppUrl: 'https://ca.api.ovh.com/createApp/',
        createTokenUrl: 'https://ca.api.ovh.com/createToken/',
        apiEndpoint: 'https://ca.api.ovh.com/v1',
        rushRegion: 'canada'
    }
};

const ALERT_SOUND_DATA = 'data:audio/wav;base64,UklGRnoGAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQoGAACBhYqFbF1fdH2Onp6dn5yXl5aXmJmam5ydn56dn5+goaKjpKWlp6eorK2tr7GxsrKys7S0tbW2tra3t7e4uLm5uru7u7y8vL29vr6/v8DAwMHBwsLCwsPDw8TExMXFxcbGxsfHx8jIyMnJysrKy8vLzMzMzc3Ozs/Pz9DQ0NHR0tLS09PT1NTU1dXW1tbX19fY2NjZ2dra29vb3Nzc3d3e3t/f3+Dg4OHh4uLi4+Pj5OTk5eXm5ubn5+fo6Ojp6erq6+vr7Ozs7e3u7u/v7/Dw8PHx8vLy8/Pz9PT09fX29vb39/f4+Pj5+fr6+vv7+/z8/P39/v7///8=';

let state = {
    view: 'loading',
    configured: false,
    endpoint: 'ovh-eu',
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
    checkoutDefaults: null
};

let audioContext = null;
let alertBuffer = null;
let alertPanelTimer = null;

// --- 2. DOM helpers --------------------------------------------------------

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
            // Don't set the attribute if false — presence/absence is what matters
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
    document.getElementById('loading-view').classList.remove('hidden');
}

function hideLoading() {
    document.getElementById('loading-view').classList.add('hidden');
}

function showError(message) {
    const errorView = document.getElementById('error-view');
    document.getElementById('error-message').textContent = message;
    errorView.classList.remove('hidden');
}

function hideError() {
    document.getElementById('error-view').classList.add('hidden');
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

// --- 3. API client ---------------------------------------------------------

async function apiRequest(method, path, body = null) {
    const options = {
        method,
        headers: { 'Content-Type': 'application/json' }
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
        return health.configured;
    } catch (e) {
        return false;
    }
}

// --- 4. Catalog (load, search, filter, render) -----------------------------

const SUBSIDIARIES_BY_ENDPOINT = {
    'ovh-eu': ['IE', 'FR', 'DE', 'GB', 'ES', 'PL', 'IT', 'PT', 'CZ', 'FI'],
    'ovh-us': ['US'],
    'ovh-ca': ['CA'],
};

function defaultSubsidiaryForEndpoint(endpoint) {
    const list = SUBSIDIARIES_BY_ENDPOINT[endpoint] || SUBSIDIARIES_BY_ENDPOINT['ovh-eu'];
    return list[0];
}

function populateCatalogCountries() {
    const select = document.getElementById('catalog-country');
    if (!select) return;
    const list = SUBSIDIARIES_BY_ENDPOINT[state.endpoint] || SUBSIDIARIES_BY_ENDPOINT['ovh-eu'];
    select.innerHTML = '';
    list.forEach(code => {
        select.appendChild(el('option', { value: code, text: code }));
    });
}

async function loadCatalog(country) {
    showLoading();
    const subsidiary = country || defaultSubsidiaryForEndpoint(state.endpoint);
    state.catalogCountry = subsidiary;
    try {
        const url = subsidiary
            ? `/catalog/plans?country=${encodeURIComponent(subsidiary)}`
            : '/catalog/plans';
        const plans = await apiRequest('GET', url);
        state.catalog = { plans };
        state.plans = plans || [];
        renderPlanSelect();
        renderCatalogList();
        // Re-render detail if a plan was selected
        if (state.selectedPlanCode) {
            const p = state.plans.find(x => x.planCode === state.selectedPlanCode);
            if (p) renderCatalogDetail(p);
        }
    } catch (e) {
        showError(e.message);
    } finally {
        hideLoading();
    }
}

async function refreshCatalogSilent() {
    if (!state.configured || !state.catalogCountry) return;
    try {
        const url = `/catalog/plans?country=${encodeURIComponent(state.catalogCountry)}`;
        const plans = await apiRequest('GET', url);
        const oldCount = state.plans.length;
        state.catalog = { plans };
        state.plans = plans || [];
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
    } catch (e) {
        // Silent fail — don't disrupt the user with error banners on background polls
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
//   formattedPrice: "$90.00 USD" (pre-formatted by OVH — use for display)
function getPlanMonthlyPrice(plan) {
    const pricings = plan.pricings || [];
    const monthly = pricings.find(p => p.mode === 'default' && p.interval === 1 && p.intervalUnit === 'month');
    if (monthly) return monthly;
    // Fallback: any default-mode pricing with a renew capacity
    return pricings.find(p => p.mode === 'default' && (p.capacities || []).includes('renew'));
}

function formatPrice(priceValue) {
    if (typeof priceValue !== 'number' || !isFinite(priceValue)) {
        return 'On request';
    }
    if (priceValue === 0) {
        return '$0.00';
    }
    // OVH stores prices in microcents: divide by 10^8 to get currency units.
    return `$${(priceValue / 100000000).toFixed(2)}`;
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
        const opt = el('option', { value: plan.planCode, text: planLabel(plan) });
        select.appendChild(opt);
    });
}

function getFilteredPlans() {
    const q = (document.getElementById('catalog-search')?.value || '').trim().toLowerCase();
    const sort = document.getElementById('catalog-sort')?.value || 'default';
    let plans = state.plans.slice();
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
        const priceText = monthly?.formattedPrice || (monthly?.price != null ? formatPrice(monthly.price) : 'On request');

        const name = el('span', { class: 'font-bold text-blue-400', text: plan.invoiceName || plan.planCode });
        const region = planRegion(plan.planCode);
        const regionSpan = region ? el('span', { class: 'text-yellow-400 ml-1 text-xs', text: `[${region}]` }) : null;
        const code = el('span', { class: 'text-gray-400 ml-2 text-xs', text: plan.planCode });
        const left = el('div', {}, [name, regionSpan, code]);
        const price = el('span', { class: 'text-green-400 text-sm', text: priceText });

        const div = el('div', {
            class: 'bg-gray-700 rounded p-2 text-sm flex justify-between items-center cursor-pointer hover:bg-gray-600',
            role: 'button',
            tabindex: '0'
        }, [left, price]);

        const selectPlan = () => {
            document.getElementById('plan-select').value = plan.planCode;
            renderCatalogDetail(plan);
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

// --- Human-readable addon label parsers ---

function humanizeAddon(code) {
    if (!code) return 'Unknown';
    const lower = code.toLowerCase();
    if (lower.startsWith('ram-')) return humanizeRam(code);
    if (lower.startsWith('softraid-') || lower.startsWith('noraid-')) return humanizeStorage(code);
    if (lower.startsWith('bandwidth-')) return humanizeBandwidth(code);
    return code;
}

function humanizeRam(code) {
    // ram-{size}g-{type}-{speed}-{product}-{region}
    // e.g. ram-32g-ecc-2400-24risegame01-eu → "32 GB ECC @ 2400 MHz"
    //      ram-64g-noecc-2133-25skle04-us → "64 GB non-ECC @ 2133 MHz"
    const m = code.match(/^ram-(\d+)g-(ecc|noecc)-(\d+)-/i);
    if (m) {
        const size = m[1];
        const type = m[2].toLowerCase() === 'noecc' ? 'non-ECC' : 'ECC';
        const speed = m[3];
        return `${size} GB ${type} @ ${speed} MHz`;
    }
    return code;
}

function humanizeStorage(code) {
    // softraid-{count}x{size}{type}-{product}-{region}
    // noraid-{count}x{size}{type}-{product}-{region}
    // e.g. softraid-2x480ssd-24sk60b-eu → "2× 480 GB SSD (SoftRAID)"
    //      noraid-1x120ssd-25skb01-eu → "1× 120 GB SSD (No RAID)"
    //      softraid-2x512nvme-... → "2× 512 GB NVMe"
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

function humanizeBandwidth(code) {
    // bandwidth-{speed}-{optional: unguaranteed}-{product}-{region}
    // e.g. bandwidth-500-25sk-eu → "500 Mbps"
    //      bandwidth-1000-rise-game-eu → "1 Gbps"
    //      bandwidth-300-unguaranteed-25skle-us → "300 Mbps (unguaranteed)"
    const m = code.match(/^bandwidth-(\d+)(?:-(unguaranteed))?-/i);
    if (m) {
        const speed = parseInt(m[1], 10);
        const speedLabel = speed >= 1000 ? `${speed / 1000} Gbps` : `${speed} Mbps`;
        const unguaranteed = m[2] ? ' (unguaranteed)' : '';
        return `${speedLabel}${unguaranteed}`;
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

function renderCatalogDetail(plan) {
    state.selectedPlanCode = plan.planCode;
    const container = document.getElementById('catalog-detail');
    container.innerHTML = '';

    const monthly = getPlanMonthlyPrice(plan);
    const priceText = monthly?.formattedPrice || (monthly?.price != null ? formatPrice(monthly.price) : 'On request');
    const region = planRegion(plan.planCode);

    // Parse server name + CPU from invoiceName (format: "MODEL | CPU")
    const parts = (plan.invoiceName || plan.planCode).split('|');
    const serverModel = parts[0].trim();
    const cpu = parts.length > 1 ? parts[1].trim() : null;

    // Commercial info from blobs
    const blobs = plan.blobs || {};
    const commercial = blobs.commercial || {};
    const useCase = (commercial.features || []).find(f => f.name === 'baremetal-server-usecases')?.value;

    // Header
    container.appendChild(el('h2', { class: 'text-2xl font-bold text-blue-400 mb-1', text: serverModel }));
    if (cpu) {
        container.appendChild(el('p', { class: 'text-gray-300 mb-1', text: cpu }));
    }
    if (region) {
        container.appendChild(el('span', { class: 'inline-block bg-yellow-600/30 text-yellow-400 text-xs px-2 py-1 rounded mb-2', text: region }));
    }
    container.appendChild(el('p', { class: 'text-gray-500 text-xs font-mono mb-4', text: plan.planCode }));

    // Price
    const priceSection = el('div', { class: 'bg-gray-700 rounded p-3 mb-4' }, [
        el('div', { class: 'flex justify-between items-center' }, [
            el('span', { class: 'text-gray-400 text-sm', text: 'Monthly price' }),
            el('span', { class: 'text-green-400 font-bold text-lg', text: priceText }),
        ]),
    ]);
    if (monthly?.promotions?.length) {
        const promo = monthly.promotions[0];
        priceSection.appendChild(el('p', { class: 'text-yellow-400 text-xs mt-1', text: `Promo: ${promo.name} (${promo.formattedValue || promo.value + '%'} off)` }));
    }
    container.appendChild(priceSection);

    // Hardware specs from addonFamilies — selectable cards
    const families = plan.addonFamilies || [];
    const specsSection = el('div', { class: 'space-y-3 mb-4' });
    specsSection.appendChild(el('h3', { class: 'font-bold text-gray-400 text-sm uppercase mb-2', text: 'Configuration Options' }));

    // Track selected addon per family (defaults to the plan's default)
    const selectedAddons = {};
    for (const fam of families) {
        selectedAddons[fam.name] = fam.default || (fam.addons || [])[0] || null;
    }

    // Build the FQN string from the plan base + selected addon short codes.
    // OVH FQN format: {planBase}.{memory}.{storage}.{bandwidth} — order matters!
    function buildFqn() {
        const planBase = plan.planCode.split('-').slice(0, -1).join('-') || plan.planCode;
        const parts = [planBase];
        // Canonical order: memory → storage → bandwidth
        for (const famName of ['memory', 'storage', 'bandwidth']) {
            const addon = selectedAddons[famName];
            if (!addon) continue;
            // Strip the last 2 segments (product code + region) from the addon code
            // e.g. ram-32g-ecc-2400-24risegame01-eu → ram-32g-ecc-2400
            const segs = addon.split('-');
            const short = segs.length > 2 ? segs.slice(0, -2).join('-') : addon;
            parts.push(short);
        }
        return parts.join('.');
    }

    // FQN preview line
    const fqnPreview = el('div', { class: 'bg-gray-700 rounded p-2 mb-3' }, [
        el('span', { class: 'text-gray-500 text-xs', text: 'FQN: ' }),
        el('code', { id: 'fqn-preview', class: 'text-blue-300 text-xs font-mono', text: buildFqn() }),
    ]);

    function updateFqnPreview() {
        const el2 = document.getElementById('fqn-preview');
        if (el2) el2.textContent = buildFqn();
    }

    function syncOrderForm(famName, addon) {
        const selectId = `order-${famName}`;
        const sel = document.getElementById(selectId);
        if (sel) sel.value = addon;
    }

    for (const fam of families) {
        const famName = fam.name.charAt(0).toUpperCase() + fam.name.slice(1);
        const itemsContainer = el('div', { class: 'space-y-1' });

        for (const addon of (fam.addons || [])) {
            const isDefault = addon === fam.default;
            const isSelected = addon === selectedAddons[fam.name];
            const card = el('div', {
                class: `flex items-center justify-between rounded px-3 py-2 cursor-pointer transition-colors ${isSelected ? 'bg-blue-600/30 border border-blue-500' : 'bg-gray-700 border border-gray-600 hover:bg-gray-600'}`,
                role: 'button',
                tabindex: '0',
                onclick: () => {
                    selectedAddons[fam.name] = addon;
                    // Re-render just the cards for this family
                    itemsContainer.querySelectorAll('[data-addon]').forEach(c => {
                        const cAddon = c.dataset.addon;
                        const selected = cAddon === addon;
                        c.className = `flex items-center justify-between rounded px-3 py-2 cursor-pointer transition-colors ${selected ? 'bg-blue-600/30 border border-blue-500' : 'bg-gray-700 border border-gray-600 hover:bg-gray-600'}`;
                        const label = c.querySelector('[data-label]');
                        if (label) label.className = selected ? 'text-blue-300' : 'text-gray-300';
                        const badge = c.querySelector('[data-badge]');
                        if (badge) badge.textContent = selected ? 'SELECTED' : (isDefault ? 'DEFAULT' : '');
                    });
                    updateFqnPreview();
                    syncOrderForm(fam.name, addon);
                },
            });
            card.dataset.addon = addon;

            const labelSpan = el('span', { class: isSelected ? 'text-blue-300' : 'text-gray-300', text: humanizeAddon(addon) });
            labelSpan.dataset.label = '1';
            const badgeSpan = el('span', {
                class: isSelected ? 'text-blue-400 text-xs font-bold' : (isDefault ? 'text-green-500 text-xs' : 'text-gray-600 text-xs'),
                text: isSelected ? 'SELECTED' : (isDefault ? 'DEFAULT' : ''),
            });
            badgeSpan.dataset.badge = '1';
            card.appendChild(labelSpan);
            card.appendChild(badgeSpan);

            card.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    card.click();
                }
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

    // Available datacenters
    const configs = plan.configurations || [];
    const dcConfig = configs.find(c => c.name === 'dedicated_datacenter');
    if (dcConfig && dcConfig.values && dcConfig.values.length) {
        const dcList = dcConfig.values.map(dc => el('span', { class: 'inline-block bg-gray-700 text-gray-300 text-xs px-2 py-1 rounded mr-1 mb-1', text: humanizeDatacenter(dc) }));
        container.appendChild(el('div', { class: 'mb-4' }, [
            el('p', { class: 'text-gray-400 text-sm font-bold mb-1', text: 'Available Datacenters' }),
            el('div', {}, dcList),
        ]));
    }

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
            const maxPrice = state.checkoutDefaults?.max_price || null;

            if (!confirm(`Place order for ${serverModel}?\n${monthly?.formattedPrice || priceText}/mo\nDC: ${(dc||'default').toUpperCase()}\nDuration: ${duration}`)) {
                return;
            }

            try {
                showLoading();
                const result = await apiRequest('POST', '/checkout/rush', {
                    plan_code: plan.planCode,
                    fqn: plan.planCode,
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
    // Lazy-load billing data when switching to that tab
    if (tabId === 'billing-tab' && !state.billingLoaded) {
        loadBillingInfo();
    }
}

// --- 4b. Billing & account info -------------------------------------------

async function loadBillingInfo() {
    await Promise.all([loadAccountInfo(), loadPaymentMethods(), loadCheckoutDefaults()]);
    state.billingLoaded = true;
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
        showError('Checkout defaults saved.');
        setTimeout(() => hideError(), 2000);
    } catch (e) {
        showError(e.message);
    }
}

// --- 5. Alerts (CRUD, render lists) ----------------------------------------

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
        const name = el('span', { class: 'font-bold text-blue-400', text: alert.plan_code });
        const pattern = el('span', { class: 'text-gray-400 ml-2 text-sm', text: alert.fqn_pattern });
        const left = el('div', {}, [name, pattern]);
        const delBtn = el('button', {
            class: 'text-red-400 hover:text-red-300 delete-alert-btn',
            'data-id': alert.id,
            text: '\u00D7',
            'aria-label': `Delete alert for ${alert.plan_code}`
        });
        delBtn.addEventListener('click', async () => {
            await deleteAlert(alert.id);
        });
        const row = el('div', { class: 'bg-gray-700 rounded p-2 flex justify-between items-center' }, [left, delBtn]);
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

// --- 6. SSE monitoring (start/stop, stock alerts, reconnect) --------------

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

// --- 8. Rush order (one-shot POST /api/checkout/rush) ---------------------

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
            waive_retractation: waive
        });

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

// --- 7. Audio (init, unlock on gesture, play alert sound) ------------------

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

// --- 9. Credentials view ---------------------------------------------------

function updateCredentialsView(region) {
    const regionInfo = OVH_REGIONS[region] || OVH_REGIONS['ovh-eu'];
    document.getElementById('create-app-link').href = regionInfo.createAppUrl;
    document.getElementById('create-app-link').textContent = regionInfo.createAppUrl;
    document.getElementById('create-token-link').href = regionInfo.createTokenUrl;
    document.getElementById('create-token-link').textContent = regionInfo.createTokenUrl;

    const rushRegion = document.getElementById('rush-region');
    if (rushRegion) {
        rushRegion.value = regionInfo.rushRegion;
    }
}

async function saveCredentials() {
    const endpoint = document.getElementById('ovh-region-select').value;
    const applicationKey = document.getElementById('cred-app-key').value.trim();
    const applicationSecret = document.getElementById('cred-app-secret').value.trim();
    const consumerKey = document.getElementById('cred-consumer-key').value.trim();

    if (!applicationKey || !applicationSecret || !consumerKey) {
        showCredentialTestResult('error', 'All three credential fields are required.');
        return;
    }

    showCredentialTestResult('loading', 'Saving and testing credentials...');

    try {
        await apiRequest('POST', '/setup/credentials', {
            endpoint,
            application_key: applicationKey,
            application_secret: applicationSecret,
            consumer_key: consumerKey,
        });

        // Test the credentials
        try {
            const result = await apiRequest('POST', '/setup/test');
            showCredentialTestResult('success',
                `Connected as ${result.firstname || ''} ${result.name || ''} (${result.nichandle || 'unknown'})`);

            // After 1.5s, proceed to the monitor
            setTimeout(async () => {
                state.configured = true;
                state.endpoint = endpoint;
                populateCatalogCountries();
                await loadAlerts();
                await loadCatalog();
                await loadPollInterval();
                await loadProfiles();
                await loadOrders();
                await loadSniperStatus();
                document.getElementById('settings-btn').classList.remove('hidden');
                showView('monitor');
            }, 1500);
        } catch (e) {
            showCredentialTestResult('error', `Credentials saved but test failed: ${e.message}`);
        }
    } catch (e) {
        showCredentialTestResult('error', e.message);
    }
}

async function deleteCredentials() {
    if (!confirm('Delete stored credentials? You will need to re-enter them to use the monitor.')) {
        return;
    }
    try {
        await apiRequest('DELETE', '/setup/credentials');
        showCredentialTestResult('success', 'Credentials deleted. Restart the server to reconfigure.');
        document.getElementById('cred-app-key').value = '';
        document.getElementById('cred-app-secret').value = '';
        document.getElementById('cred-consumer-key').value = '';
        document.getElementById('delete-credentials-btn').classList.add('hidden');
        document.getElementById('setup-title').textContent = 'Setup Required';
        document.getElementById('settings-btn').classList.add('hidden');
        state.configured = false;
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
    try {
        const result = await apiRequest('GET', '/setup/credentials');
        if (result.configured) {
            document.getElementById('setup-title').textContent = 'Credentials Configured';
            document.getElementById('setup-description').textContent =
                `Credentials are stored for endpoint ${result.endpoint}. App key: ${result.application_key_masked || '****'}, Consumer key: ${result.consumer_key_masked || '****'}.`;
            document.getElementById('delete-credentials-btn').classList.remove('hidden');
            // Pre-select the right region
            if (result.endpoint) {
                document.getElementById('ovh-region-select').value = result.endpoint;
                updateCredentialsView(result.endpoint);
            }
        }
    } catch (e) {
        // ignore — fresh install
    }
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
        const head = el('div', {}, [
            el('span', { class: 'text-blue-400 font-bold', text: `${o.plan_code} ${id}` }),
            el('span', { class: 'text-gray-400 ml-2 text-xs', text: time }),
        ]);
        const st = el('span', { class: 'text-xs text-gray-400', text: `status: ${status}` });
        container.appendChild(el('div', { class: 'bg-gray-700 rounded p-2' }, [head, st]));
    });
}

// --- 13. Init (DOMContentLoaded) -------------------------------------------

async function init() {
    showView('loading');
    hideError();
    initAudio();

    const configured = await checkHealth();
    state.configured = configured;

    const regionSelect = document.getElementById('ovh-region-select');
    if (regionSelect) {
        regionSelect.addEventListener('change', (e) => {
            updateCredentialsView(e.target.value);
        });
        updateCredentialsView(regionSelect.value);
    }

    if (!configured) {
        await loadExistingCredentials();
        showView('credentials');
    } else {
        document.getElementById('settings-btn').classList.remove('hidden');
        populateCatalogCountries();
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

    document.getElementById('settings-btn').addEventListener('click', () => {
        showView('credentials');
        loadExistingCredentials();
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

    document.getElementById('load-catalog-btn').addEventListener('click', () => {
        const country = document.getElementById('catalog-country').value;
        loadCatalog(country);
    });

    document.getElementById('catalog-country').addEventListener('change', (e) => {
        loadCatalog(e.target.value);
    });

    document.getElementById('catalog-search')?.addEventListener('input', renderCatalogList);
    document.getElementById('catalog-sort')?.addEventListener('change', renderCatalogList);

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
}

document.addEventListener('DOMContentLoaded', init);
