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
    alerts: [],
    profiles: [],
    recentAlerts: [],
    currentStock: {},
    cart: null,
    cartCreatedAt: null,
    orderResult: null
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
        } else if (key === 'role') {
            node.setAttribute('role', value);
        } else if (key === 'tabindex') {
            node.setAttribute('tabindex', value);
        } else {
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
    try {
        const subsidiary = country || defaultSubsidiaryForEndpoint(state.endpoint);
        const url = subsidiary
            ? `/catalog/plans?country=${encodeURIComponent(subsidiary)}`
            : '/catalog/plans';
        const plans = await apiRequest('GET', url);
        state.catalog = { plans };
        state.plans = plans || [];
        renderPlanSelect();
        renderCatalogList();
    } catch (e) {
        showError(e.message);
    } finally {
        hideLoading();
    }
}

function formatPrice(priceInUcents) {
    if (typeof priceInUcents !== 'number' || !isFinite(priceInUcents)) {
        return 'On request';
    }
    if (priceInUcents === 0) {
        return '\u20AC0.00';
    }
    return `\u20AC${(priceInUcents / 1000000).toFixed(2)}`;
}

function renderPlanSelect() {
    const select = document.getElementById('plan-select');
    select.innerHTML = '';
    select.appendChild(el('option', { value: '', text: 'Select a plan...' }));
    state.plans.forEach(plan => {
        const opt = el('option', { value: plan.planCode, text: plan.invoiceName || plan.planCode });
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
        const mp = p.prices?.find(x => x.label === 'default')?.price;
        return mp?.priceInUcents ?? Infinity;
    };
    if (sort === 'price-asc') plans.sort((a, b) => priceOf(a) - priceOf(b));
    else if (sort === 'price-desc') plans.sort((a, b) => priceOf(b) - priceOf(a));
    else if (sort === 'name') plans.sort((a, b) => (a.invoiceName || '').localeCompare(b.invoiceName || ''));
    return plans;
}

function renderCatalogList() {
    const container = document.getElementById('catalog-plans');
    container.innerHTML = '';
    const plans = getFilteredPlans().slice(0, 50);
    if (plans.length === 0) {
        container.appendChild(el('p', { class: 'text-gray-500 text-sm', text: 'No plans match your search.' }));
        return;
    }
    plans.forEach(plan => {
        const mainPrice = plan.prices?.find(p => p.label === 'default')?.price;
        const priceText = mainPrice ? formatPrice(mainPrice.priceInUcents) : 'On request';

        const name = el('span', { class: 'font-bold text-blue-400', text: plan.invoiceName || plan.planCode });
        const code = el('span', { class: 'text-gray-400 ml-2', text: plan.planCode });
        const left = el('div', {}, [name, code]);
        const price = el('span', { class: 'text-green-400', text: priceText });

        const div = el('div', {
            class: 'bg-gray-700 rounded p-2 text-sm flex justify-between items-center cursor-pointer hover:bg-gray-600',
            role: 'button',
            tabindex: '0'
        }, [left, price]);

        const selectPlan = () => {
            document.getElementById('plan-select').value = plan.planCode;
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
    document.getElementById('env-vars-template').textContent =
        `export OVH_APPLICATION_KEY="your_key"\nexport OVH_APPLICATION_SECRET="your_secret"\nexport OVH_CONSUMER_KEY="your_consumer_key"\nexport OVH_ENDPOINT="${region}"`;

    const rushRegion = document.getElementById('rush-region');
    if (rushRegion) {
        rushRegion.value = regionInfo.rushRegion;
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
        showView('credentials');
    } else {
        populateCatalogCountries();
        await loadAlerts();
        await loadCatalog();
        await loadPollInterval();
        await loadProfiles();
        await loadOrders();
        await loadSniperStatus();
        showView('monitor');
    }

    document.getElementById('check-credentials-btn').addEventListener('click', async () => {
        const configured = await checkHealth();
        if (configured) {
            state.configured = true;
            populateCatalogCountries();
            await loadAlerts();
            await loadCatalog();
            await loadPollInterval();
            await loadProfiles();
            await loadOrders();
            await loadSniperStatus();
            showView('monitor');
        } else {
            showError('Credentials not configured. Please follow the setup instructions.');
        }
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
