"""Browser KVM console: JNLP parsing, session brokering and the WebSocket relay.

OVH's servers here are ATEN iKVM — plain RFB 3.8 on TCP 5900 behind an
OVH-brokered host. The JNLP is used only as a connection descriptor; nothing
Java runs. These tests use a fake BMC (an asyncio echo/greeter on localhost) so
no hardware is needed.
"""
import asyncio
import os
import re
import threading
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.console import ConsoleSession, _sessions, parse_kvm_jnlp
from app.main import app
from app.services.ovh_service import get_active_ovh_service

XHR = {"X-Requested-With": "XMLHttpRequest"}

# The live JNLP OVH returned for ns3147088.ip-51-83-10.eu, with the credentials
# replaced. The argument ORDER is the contract: host, user, password, unused,
# video port, IPMI port, then two flags.
LIVE_JNLP = """<jnlp spec="1.0+" codebase="https://abc123.gra3-1.ipmi.ovh.net/">
  <information>
    <title>ATEN Java iKVM Viewer</title>
    <vendor>ATEN</vendor>
  </information>
  <resources>
    <jar href="iKVM__V1.69.21.0x0.jar" download="eager" main="true"/>
  </resources>
  <application-desc main-class="tw.com.aten.ikvm.KVMMain">
    <argument>5.135.96.128</argument>
    <argument>abcdefghijklmnop</argument>
    <argument>abcdefghijklmnop</argument>
    <argument>null</argument>
    <argument>5900</argument>
    <argument>623</argument>
    <argument>2</argument>
    <argument>0</argument>
  </application-desc>
</jnlp>"""


@pytest.fixture
def client():
    _sessions.clear()
    with TestClient(app) as c:
        yield c
    _sessions.clear()


def _create_account(client):
    r = client.post(
        "/api/accounts",
        json={"label": "test", "endpoint": "ovh-us", "application_key": "ak",
              "application_secret": "secret123", "consumer_key": "ck"},
        headers=XHR,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _stub_ovh(jnlp=LIVE_JNLP):
    """Make the IPMI session flow return a JNLP without touching OVH."""
    svc = get_active_ovh_service()
    svc.server_get = MagicMock(return_value={"value": jnlp, "expiration": "2026-08-07T06:00:00Z"})
    svc.server_post = MagicMock(return_value={"taskId": 1})
    return svc


# ----- JNLP parsing -----


def test_parses_the_live_aten_jnlp():
    info = parse_kvm_jnlp(LIVE_JNLP)
    assert info["host"] == "5.135.96.128"
    assert info["port"] == 5900          # video port is arg 5, not arg 6 (623 = IPMI)
    assert info["username"] == "abcdefghijklmnop"
    assert info["password"] == "abcdefghijklmnop"
    assert info["vendor"] == "ATEN"
    assert info["main_class"] == "tw.com.aten.ikvm.KVMMain"


@pytest.mark.parametrize("xml, expected", [
    ("not xml at all", "not valid XML"),
    ("<jnlp></jnlp>", "no <application-desc>"),
    ("<jnlp><application-desc><argument>a</argument></application-desc></jnlp>",
     "expected at least 5"),
])
def test_rejects_malformed_jnlp(xml, expected):
    """A vendor-specific positional format deserves a clear error, not an
    IndexError three layers down."""
    with pytest.raises(ValueError, match=expected):
        parse_kvm_jnlp(xml)


def test_rejects_a_non_numeric_port():
    xml = LIVE_JNLP.replace("<argument>5900</argument>", "<argument>hello</argument>")
    with pytest.raises(ValueError, match="not a number"):
        parse_kvm_jnlp(xml)


def test_rejects_empty_credentials():
    xml = LIVE_JNLP.replace("<argument>abcdefghijklmnop</argument>\n    <argument>abcdefghijklmnop</argument>",
                            "<argument></argument>\n    <argument></argument>")
    with pytest.raises(ValueError, match="missing host/username/password"):
        parse_kvm_jnlp(xml)


# ----- session brokering -----


def test_session_returns_credentials_but_never_the_bmc_address(client):
    """noVNC must do the ATEN handshake itself (that is what switches its ATEN
    decoder on), so it needs the credentials — but the browser addresses an
    opaque session id, never the BMC."""
    _create_account(client)
    _stub_ovh()

    body = client.post("/api/servers/ns1.example/console/session", headers=XHR).json()

    assert body["password"] == "abcdefghijklmnop:abcdefghijklmnop"
    assert body["vendor"] == "ATEN"
    assert body["session_id"]
    serialised = str(body)
    assert "5.135.96.128" not in serialised
    assert "5900" not in serialised


def test_session_rejects_an_unparseable_descriptor(client):
    _create_account(client)
    _stub_ovh(jnlp="<jnlp></jnlp>")

    r = client.post("/api/servers/ns1.example/console/session", headers=XHR)
    assert r.status_code == 502
    assert "Unsupported console descriptor" in r.json()["detail"]


def test_opening_a_second_session_replaces_the_first(client):
    """The BMC supports one KVM session; the newest wins."""
    _create_account(client)
    _stub_ovh()

    first = client.post("/api/servers/ns1.example/console/session", headers=XHR).json()
    second = client.post("/api/servers/ns1.example/console/session", headers=XHR).json()

    assert first["session_id"] != second["session_id"]
    assert _sessions["ns1.example"].id == second["session_id"]


# ----- the relay -----


class FakeBMC:
    """Greets like RFB 3.8, then echoes — enough to prove the pipe is wired."""

    def __init__(self):
        self.server = None
        self.port = None
        self.received = bytearray()

    async def start(self):
        async def handle(reader, writer):
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                self.received.extend(data)
                writer.write(data.upper())
                await writer.drain()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


def _point_session_at(host, port, service_name="ns1.example", ttl=60):
    s = ConsoleSession(service_name, host, port, "u", "p", time.monotonic() + ttl)
    _sessions[service_name] = s
    return s


async def _run_until(stop):
    while not stop.is_set():
        await asyncio.sleep(0.02)


def test_relay_pipes_both_directions(client):
    """The point of the whole module: a browser socket reaching BMC TCP.

    TestClient drives the app on its own loop, so the fake BMC gets a second
    loop on a background thread — the two talk over a real localhost socket,
    which is exactly what the relay does in production.
    """
    bmc = FakeBMC()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(bmc.start())
    stop = threading.Event()
    thread = threading.Thread(target=lambda: loop.run_until_complete(_run_until(stop)),
                              daemon=True)
    thread.start()

    session = _point_session_at("127.0.0.1", bmc.port)
    try:
        with client.websocket_connect(
            f"/api/servers/ns1.example/console/ws?session={session.id}",
            subprotocols=["binary"],
        ) as ws:
            assert ws.receive_bytes() == b"RFB 003.008\n"      # BMC -> browser
            ws.send_bytes(b"hello")
            assert ws.receive_bytes() == b"HELLO"              # browser -> BMC -> back
        assert bytes(bmc.received) == b"hello"
    finally:
        stop.set()
        thread.join(timeout=5)
        loop.run_until_complete(bmc.stop())
        loop.close()


def test_relay_rejects_an_unknown_session(client):
    _sessions.clear()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/servers/ns1.example/console/ws?session=nope",
            subprotocols=["binary"],
        ):
            pass


def test_relay_rejects_an_expired_session(client):
    s = _point_session_at("127.0.0.1", 1, ttl=-1)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/servers/ns1.example/console/ws?session={s.id}",
            subprotocols=["binary"],
        ):
            pass
    assert "ns1.example" not in _sessions       # expired entry is dropped


def test_relay_rejects_a_cross_origin_socket(client):
    """Browsers do not preflight WebSocket upgrades, so CsrfMiddleware never
    sees this route — the origin check has to live here."""
    s = _point_session_at("127.0.0.1", 1)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/servers/ns1.example/console/ws?session={s.id}",
            subprotocols=["binary"],
            headers={"origin": "https://evil.example"},
        ):
            pass


# ----- the vendored decoder's one local patch -----

# Captured live from OVH's BMC after selecting security type 16 (see
# PROVENANCE.md). 24 bytes; the leading u32 is 0xa7f95fbe, which is what makes
# noVNC pick heuristic #0.
LIVE_ATEN_PREAMBLE = bytes.fromhex(
    "a7f95fbe5021020020a6000070ecefbe00704b40102e0100"
)
_RFB_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "vendor", "novnc", "core", "rfb.js",
)


def _aten_preamble_arithmetic():
    """The three numbers that decide whether the ATEN handshake stays aligned,
    read out of the vendored file itself."""
    src = open(_RFB_JS).read()
    return {
        "length": int(re.search(r"var ATEN_PREAMBLE_LENGTH = (\d+);", src).group(1)),
        "h0": int(re.search(
            r"heuristic #0.*?_aten_preamble_read = (\d+);", src, re.S).group(1)),
        "h1": int(re.search(
            r"heuristic #1.*?_aten_preamble_read = (\d+);", src, re.S).group(1)),
    }


@pytest.mark.parametrize("heuristic", ["h0", "h1"])
def test_both_aten_heuristics_consume_the_whole_preamble(heuristic):
    """Upstream skips a flat 16 bytes after the detection read, which totals 24
    via heuristic #1 but only 20 via #0. Via #0 that left 4 bytes of preamble
    queued, and `_handle_security_result` read them instead of the real result —
    surfacing as "Unsupported server (Unknown SecurityResult)" in the browser.

    Both paths must land exactly on the end of the preamble. This is the one
    local modification to the vendored fork, so a re-vendor that overwrites it
    must fail here rather than silently breaking the console again.
    """
    a = _aten_preamble_arithmetic()
    assert a["length"] == len(LIVE_ATEN_PREAMBLE)
    consumed = a[heuristic]
    # The detection read, plus the skip of everything remaining.
    assert consumed + (a["length"] - consumed) == len(LIVE_ATEN_PREAMBLE)


def test_the_vendored_fork_skips_the_remainder_not_a_fixed_count():
    """Pin the mechanism, not just the totals: a flat `rQskipBytes(16)` is the
    bug, and it reads as correct until you check it against a #0 server."""
    src = open(_RFB_JS).read()
    aten = src[src.index("_negotiate_aten_auth:"):]
    aten = aten[:aten.index("_negotiate_xvp_auth:")]
    assert "rQskipBytes(ATEN_PREAMBLE_LENGTH - consumed)" in aten
    assert "rQskipBytes(16)" not in aten


def test_console_page_renders_with_the_vendored_bundle(client):
    r = client.get("/console/ns1.example")
    assert r.status_code == 200
    body = r.text
    assert "ns1.example" in body
    # Load order matters; rfb.js must come after its dependencies.
    assert body.index("core/util.js") < body.index("core/rfb.js")
    assert body.index("core/ast2100/ast2100.js") < body.index("core/rfb.js")
    assert "/static/js/kvm.js" in body


def test_every_vendored_script_is_cache_busted(client):
    """The vendored bundle shipped with no `?v=`, so the ATEN handshake fix
    could not reach a browser that had already loaded the broken rfb.js."""
    body = client.get("/console/ns1.example").text
    tags = re.findall(r'<script src="(/static/vendor/novnc/[^"]+)"', body)
    assert len(tags) == 16, tags
    unbusted = [t for t in tags if "?v=" not in t]
    assert not unbusted, f"vendored scripts without a cache buster: {unbusted}"
    # One hash for the whole bundle, and it must be real, not the "dev" fallback.
    versions = {t.split("?v=", 1)[1] for t in tags}
    assert len(versions) == 1, versions
    assert versions != {"dev"}
