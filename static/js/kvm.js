/*
 * Full-page KVM console.
 *
 * The heavy lifting is the vendored noVNC ATEN fork (see
 * static/vendor/novnc/PROVENANCE.md): OVH's servers here expose an ATEN iKVM,
 * which is plain RFB 3.8 with a vendor security type (16) and a vendor video
 * encoding (AST2100). This file only brokers a session and points noVNC at the
 * relay in app/api/console.py, which bridges WebSocket to the BMC's TCP port —
 * a browser cannot open a raw socket itself.
 *
 * noVNC builds its URI as ws(s)://<host>:<port>/<path>, so `path` carries the
 * whole API route including the session id.
 */
'use strict';

const SERVICE_NAME = window.KVM_SERVICE_NAME;

const statusEl = document.getElementById('kvm-status');
const errorEl = document.getElementById('kvm-error');
const canvas = document.getElementById('kvm-canvas');
const cadBtn = document.getElementById('kvm-ctrlaltdel');
const reconnectBtn = document.getElementById('kvm-reconnect');

let rfb = null;
// Set by the first onFBResize *after* the connection is up. The one during
// ServerInit carries a placeholder, because ATEN only reveals the real
// resolution in its first framebuffer update — so a resize after that point is
// the signal that video is actually flowing.
let videoSeen = false;
let connected = false;
let noVideoTimer = null;

// How long to wait before telling the user a blank console is the BMC's
// answer, not a stuck client. The BMC's first update lands well inside this.
const NO_VIDEO_HINT_MS = 8000;

function setStatus(text) {
    statusEl.textContent = text;
}

function showError(text) {
    errorEl.textContent = text;
    errorEl.classList.remove('hidden');
}

function clearError() {
    errorEl.textContent = '';
    errorEl.classList.add('hidden');
}

async function openSession() {
    const r = await fetch(`/api/servers/${encodeURIComponent(SERVICE_NAME)}/console/session`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        },
    });
    if (!r.ok) {
        let detail = `HTTP ${r.status}`;
        try {
            const body = await r.json();
            if (body && body.detail) detail = body.detail;
        } catch (e) { /* keep the status code */ }
        throw new Error(detail);
    }
    return r.json();
}

function connect(session) {
    // Tear down any previous client so a reconnect doesn't leave two attached.
    if (rfb) {
        try { rfb.disconnect(); } catch (e) { /* already gone */ }
        rfb = null;
    }

    rfb = new RFB({
        target: canvas,
        encrypt: window.location.protocol === 'https:',
        local_cursor: true,
        shared: true,
        view_only: false,
        // -1 keeps the BMC's own defaults; the ATEN decoder documents quality
        // loss at low settings, so don't override them without reason.
        ast2100_quality: -1,
        ast2100_subsamplingMode: -1,
        onUpdateState: (_rfb, state) => {
            connected = state === 'connected';
            cadBtn.disabled = !connected;
            if (!connected) {
                setStatus(state);
                return;
            }
            clearError();
            setStatus('connected — waiting for video…');
            clearTimeout(noVideoTimer);
            noVideoTimer = setTimeout(() => {
                if (videoSeen) return;
                setStatus('connected — no video signal');
                showError(
                    'The console is connected but the BMC is reporting no video. '
                    + 'That is what it sends when the host is powered off or its '
                    + 'screen is blanked — click the console and press a key to '
                    + 'wake it, or check power and boot state on the Servers tab.'
                );
            }, NO_VIDEO_HINT_MS);
        },
        onFBResize: (_rfb, width, height) => {
            // The ServerInit resize fires before the state reaches 'connected'.
            if (!connected) return;
            videoSeen = true;
            clearTimeout(noVideoTimer);
            clearError();
            setStatus(`connected — ${width}×${height}`);
        },
        onDisconnected: (_rfb, reason) => {
            setStatus('disconnected');
            cadBtn.disabled = true;
            clearTimeout(noVideoTimer);
            if (reason) showError(`Disconnected: ${reason}`);
        },
        onPasswordRequired: () => {
            // Should not happen: the session response already carries
            // "user:password" for the ATEN handshake.
            showError('The console rejected the brokered credentials.');
        },
        onDesktopName: (_rfb, name) => {
            if (name) document.title = `Console — ${name}`;
        },
    });

    const port = window.location.port
        || (window.location.protocol === 'https:' ? '443' : '80');
    const path = `api/servers/${encodeURIComponent(SERVICE_NAME)}`
        + `/console/ws?session=${encodeURIComponent(session.session_id)}`;

    rfb.connect(window.location.hostname, port, session.password, path);
}

async function start() {
    clearError();
    setStatus('Requesting console session…');
    cadBtn.disabled = true;
    videoSeen = false;
    connected = false;
    clearTimeout(noVideoTimer);
    try {
        const session = await openSession();
        setStatus(`Session ready (${session.vendor}) — connecting…`);
        connect(session);
    } catch (e) {
        setStatus('failed');
        showError(
            `Could not open a console session: ${e.message}. `
            + 'OVH takes ~15s to authorise one; if this persists, check that the '
            + 'server reports IPMI support and that this host\'s public IP is the '
            + 'one allow-listed.'
        );
    }
}

cadBtn.addEventListener('click', () => {
    if (rfb) rfb.sendCtrlAltDel();
});
reconnectBtn.addEventListener('click', start);

// noVNC sizes the canvas itself from the framebuffer dimensions, so the page
// has to cope with whatever resolution the host is in. Both maxima are needed:
// with max-width alone the element keeps its intrinsic height, so an
// over-tall framebuffer scrolls the page instead of fitting it — which is what
// a 10000px ATEN placeholder looked like. `contain` keeps the aspect ratio,
// and because these are maxima a console smaller than the viewport still
// renders 1:1 rather than being blown up.
canvas.style.maxWidth = '100%';
canvas.style.maxHeight = '100%';
canvas.style.objectFit = 'contain';

start();
