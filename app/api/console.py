"""Browser KVM console: OVH IPMI session brokering + a WebSocket↔BMC relay.

OVH's dedicated servers here expose their console as **ATEN iKVM**, which is
plain RFB 3.8 on TCP 5900 (verified live: the endpoint greets with
``RFB 003.008`` and offers exactly one security type, ``16``). So the console is
a normal VNC client problem, not a Java one — the vendored noVNC fork in
``static/vendor/novnc`` speaks ATEN's security type 16 and its AST2100 video
encoding.

A browser cannot open a raw TCP socket, so this module is the missing piece: a
websockify-equivalent that lives in the existing process. No container, no new
dependency (``uvicorn[standard]`` already ships ``websockets``).

**Why the relay is a dumb byte pipe rather than doing the auth itself.**
Terminating the ATEN handshake server-side and presenting security type "None"
to the browser would keep the credentials here, which is nicer — but noVNC only
switches into ATEN mode inside ``_negotiate_aten_auth`` (``rfb.js`` sets
``_rfb_atenikvm``/``_convert_color`` there, and the ATEN encodings and the
``atenKeyEvent``/``atenPointerEvent`` framing are all gated on that flag).
Faking the handshake would leave the decoder switched off, and switching it on
would mean patching vendored files. So the browser does the real handshake and
receives the credentials.

That is an acceptable trade here: they are per-session, expire with OVH's TTL
(≤15 minutes), grant only console access to a machine the same UI already
controls, and are handed out same-origin. The BMC's **host and port never reach
the browser** — it addresses an opaque session id, and this process resolves it.
"""
import asyncio
import logging
import secrets
import time
from typing import Any
from xml.etree import ElementTree

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.api.servers import IpmiSessionRequest, _configured_service, open_ipmi_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/servers", tags=["console"])

# Relay tuning. The read size is a compromise: large enough that a full-screen
# AST2100 frame does not take dozens of round trips, small enough that one
# console cannot pin a lot of memory.
_READ_SIZE = 65536
_CONNECT_TIMEOUT = 15.0
# Hard ceiling regardless of what OVH reports, so a stuck session cannot hold a
# socket open forever.
_MAX_SESSION_SECONDS = 20 * 60


class ConsoleSession:
    """One brokered console: where to connect, and until when."""

    __slots__ = ("id", "service_name", "host", "port", "username", "password",
                 "expires_at", "websocket")

    def __init__(self, service_name: str, host: str, port: int,
                 username: str, password: str, expires_at: float) -> None:
        self.id = secrets.token_urlsafe(24)
        self.service_name = service_name
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.expires_at = expires_at
        self.websocket: WebSocket | None = None

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at


# service_name -> session. One console per server; opening a second replaces the
# first, because the BMC itself only supports one active KVM session.
_sessions: dict[str, ConsoleSession] = {}


def parse_kvm_jnlp(xml: str) -> dict[str, Any]:
    """Pull the connection details out of an ATEN iKVM JNLP.

    The interesting part is ``<application-desc>``'s positional arguments; for
    ``tw.com.aten.ikvm.KVMMain`` they are, in order: host, username, password,
    (unused), video port, IPMI port, then two numeric flags. Verified against a
    live OVH JNLP.

    Positional, undocumented and vendor-specific — so validate rather than
    trusting the shape, and say clearly what was wrong when it does not match.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as e:
        raise ValueError(f"JNLP is not valid XML: {e}") from e

    desc = root.find("application-desc")
    if desc is None:
        raise ValueError("JNLP has no <application-desc>")
    args = [(a.text or "").strip() for a in desc.findall("argument")]
    if len(args) < 5:
        raise ValueError(f"JNLP has {len(args)} arguments, expected at least 5")

    host, username, password = args[0], args[1], args[2]
    try:
        port = int(args[4])
    except ValueError as e:
        raise ValueError(f"JNLP video port {args[4]!r} is not a number") from e
    if not host or not username or not password:
        raise ValueError("JNLP is missing host/username/password")

    main_class = desc.get("main-class") or ""
    return {
        "host": host, "port": port,
        "username": username, "password": password,
        "main_class": main_class,
        "vendor": (root.findtext("information/vendor") or "").strip(),
    }


class ConsoleSessionResponse(BaseModel):
    session_id: str
    # noVNC takes ATEN credentials as a single "user:password" string.
    password: str
    expires_in: int
    vendor: str


@router.post("/{service_name}/console/session", response_model=ConsoleSessionResponse)
async def create_console_session(service_name: str) -> ConsoleSessionResponse:
    """Broker a console: ask OVH for a JNLP, parse it, and stash the target.

    The JNLP is used purely as a *connection descriptor* — nothing Java runs.
    """
    _configured_service()  # 503 early if no account is configured
    result = await open_ipmi_session(
        service_name, IpmiSessionRequest(type="kvmipJnlp", ttl=15)
    )
    value = (result.get("access") or {}).get("value")
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=502, detail="OVH returned no console descriptor")

    try:
        info = parse_kvm_jnlp(value)
    except ValueError as e:
        logger.warning("could not parse JNLP for %s: %s", service_name, e)
        raise HTTPException(status_code=502, detail=f"Unsupported console descriptor: {e}") from e

    ttl = _MAX_SESSION_SECONDS
    session = ConsoleSession(
        service_name=service_name, host=info["host"], port=info["port"],
        username=info["username"], password=info["password"],
        expires_at=time.monotonic() + ttl,
    )

    previous = _sessions.get(service_name)
    if previous is not None and previous.websocket is not None:
        try:
            await previous.websocket.close(code=1000)
        except Exception:
            pass
    _sessions[service_name] = session

    # Host/port deliberately absent from the response.
    logger.info(
        "console session opened for %s (%s, %s:%s)",
        service_name, info["vendor"] or "unknown vendor", info["host"], info["port"],
    )
    return ConsoleSessionResponse(
        session_id=session.id,
        password=f"{session.username}:{session.password}",
        expires_in=ttl,
        vendor=info["vendor"] or "ATEN",
    )


def _same_origin(websocket: WebSocket) -> bool:
    """Reject cross-origin console sockets.

    Browsers do not preflight WebSocket upgrades, so the app's ``CsrfMiddleware``
    (which only guards ``/api/*`` HTTP verbs) never sees this route. A page on
    another origin could otherwise open a console against a server on the LAN.
    A missing Origin means a non-browser client (curl, tests) and is allowed,
    matching how CsrfMiddleware treats the same case.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    host = websocket.headers.get("host")
    if not host:
        return False
    return origin.split("://", 1)[-1] == host


async def _pump(reader, websocket: WebSocket) -> None:
    """BMC → browser."""
    while True:
        data = await reader.read(_READ_SIZE)
        if not data:
            return
        await websocket.send_bytes(data)


async def _pump_ws(websocket: WebSocket, writer) -> None:
    """Browser → BMC."""
    while True:
        data = await websocket.receive_bytes()
        writer.write(data)
        await writer.drain()


@router.websocket("/{service_name}/console/ws")
async def console_ws(
    websocket: WebSocket, service_name: str, session: str = Query(...),
) -> None:
    """Relay the browser's WebSocket to the BMC's RFB port, byte for byte."""
    if not _same_origin(websocket):
        await websocket.close(code=1008)
        return
    entry = _sessions.get(service_name)
    if entry is None or not secrets.compare_digest(entry.id, session):
        await websocket.close(code=1008)
        return
    if entry.expired:
        _sessions.pop(service_name, None)
        await websocket.close(code=1008)
        return

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(entry.host, entry.port), _CONNECT_TIMEOUT
        )
    except (OSError, asyncio.TimeoutError) as e:
        logger.warning("console connect to %s:%s failed: %s", entry.host, entry.port, e)
        await websocket.close(code=1011)
        return

    # noVNC refuses anything but the 'binary' subprotocol.
    await websocket.accept(subprotocol="binary")
    entry.websocket = websocket
    logger.info("console relay open for %s", service_name)

    remaining = max(1.0, entry.expires_at - time.monotonic())
    tasks = [
        asyncio.create_task(_pump(reader, websocket)),
        asyncio.create_task(_pump_ws(websocket, writer)),
    ]
    try:
        # Either direction ending ends the session, and the OVH TTL bounds it
        # even if both sides stay silent.
        done, pending = await asyncio.wait(
            tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionError)):
                logger.debug("console pump ended: %r", exc)
    finally:
        for t in tasks:
            t.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        if entry.websocket is websocket:
            entry.websocket = None
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("console relay closed for %s", service_name)
