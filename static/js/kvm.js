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
            setStatus(state);
            const connected = state === 'connected';
            cadBtn.disabled = !connected;
            if (connected) clearError();
        },
        onDisconnected: (_rfb, reason) => {
            setStatus('disconnected');
            cadBtn.disabled = true;
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

// Keep the canvas from being scaled by the flex parent; noVNC sizes it itself
// from the framebuffer dimensions.
canvas.style.maxWidth = '100%';
canvas.style.objectFit = 'contain';

start();
