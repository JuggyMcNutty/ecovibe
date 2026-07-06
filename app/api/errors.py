"""Shared HTTP error mapping for OVH service errors.

Translates `OVHServiceError` (which carries OVH's upstream status code) into
the appropriate FastAPI `HTTPException`. The mapping is designed to preserve
semantics: a 404 from OVH (e.g. cart not found) becomes a 404 to the client,
not a generic 500.

OVH status 460 (resource expired) maps to HTTP 410 Gone. Any 5xx from OVH
becomes 502 Bad Gateway (we are a gateway, not the source of the failure).
The full error message is logged server-side; only a safe detail string is
returned to the client (the message is preserved for 4xx since it usually
contains actionable context like "cart not found").
"""
import logging
from typing import NoReturn

from fastapi import HTTPException

from app.services.ovh_service import OVHServiceError

logger = logging.getLogger(__name__)

# OVH status -> HTTP status. Values not in this map fall through to the
# 5xx / default branches below.
_OVH_STATUS_MAP = {
    400: 400,  # Bad parameters
    403: 403,  # Forbidden / not granted
    404: 404,  # Resource not found
    409: 409,  # Conflict
    460: 410,  # Resource expired (OVH-specific) -> Gone
}


def raise_ovh_http_error(e: OVHServiceError) -> NoReturn:
    """Translate an OVHServiceError into an HTTPException and raise it.

    Always logs the full error (including OVH query ID) at WARNING level
    before raising, so server-side logs retain full context even when the
    client only sees a sanitised detail string.
    """
    status = e.status_code
    if status in _OVH_STATUS_MAP:
        code = _OVH_STATUS_MAP[status]
        detail = e.message
    elif status is not None and 500 <= status < 600:
        # OVH is having a bad day — report it as a gateway error.
        code = 502
        detail = "OVH upstream error"
    else:
        # Unknown status (e.g. network error with no response) — generic 502.
        code = 502
        detail = e.message or "OVH API unavailable"
    logger.warning(
        "OVH service error: %s (ovh_status=%s query_id=%s) -> http %s",
        e.message, e.status_code, e.query_id, code,
    )
    raise HTTPException(status_code=code, detail=detail)
