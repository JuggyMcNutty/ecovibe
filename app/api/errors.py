"""Shared HTTP error mapping for OVH service errors."""
import logging
from typing import NoReturn

from fastapi import HTTPException

from app.services.ovh_service import OVHServiceError

logger = logging.getLogger(__name__)

_OVH_STATUS_MAP = {
    400: 400,
    403: 403,
    404: 404,
    409: 409,
    460: 410,
}


def raise_ovh_http_error(e: OVHServiceError) -> NoReturn:
    status = e.status_code
    if status in _OVH_STATUS_MAP:
        code = _OVH_STATUS_MAP[status]
        detail = e.message
    elif status is not None and 500 <= status < 600:
        code = 502
        detail = "OVH upstream error"
    else:
        code = 502
        detail = e.message or "OVH API unavailable"
    logger.warning(
        "OVH service error: %s (ovh_status=%s query_id=%s) -> http %s",
        e.message, e.status_code, e.query_id, code,
    )
    raise HTTPException(status_code=code, detail=detail)
