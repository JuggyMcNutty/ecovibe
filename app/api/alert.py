"""Alert CRUD + profile assignment for sniper mode."""
from typing import Any

from fastapi import APIRouter, HTTPException

from app.models.schemas import AlertCreate, AlertResponse, AssignProfileRequest
from app.services.monitor import DuplicateAlertError, get_monitor_service
from app.services.storage import get_storage

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _to_response(alert) -> AlertResponse:
    """Convert a StockAlert dataclass to an AlertResponse (ISO timestamp)."""
    return AlertResponse(
        id=alert.id,
        plan_code=alert.plan_code,
        fqn_pattern=alert.fqn_pattern,
        enabled=alert.enabled,
        notified_at=alert.notified_at.isoformat() if alert.notified_at else None,
        auto_order_profile_id=alert.auto_order_profile_id,
    )


@router.post("", status_code=201)
async def create_alert(request: AlertCreate) -> AlertResponse:
    """Create a new alert. Returns 409 if (plan_code, fqn_pattern) already exists."""
    monitor = get_monitor_service()
    try:
        alert = await monitor.add_alert(request.plan_code, request.fqn_pattern)
    except DuplicateAlertError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return _to_response(alert)


@router.get("")
async def list_alerts() -> list[AlertResponse]:
    """List all alerts (enabled and disabled)."""
    monitor = get_monitor_service()
    return [_to_response(a) for a in monitor.get_alerts()]


@router.delete("/{alert_id}")
async def delete_alert(alert_id: str) -> dict[str, Any]:
    """Delete an alert. 404 if not found."""
    monitor = get_monitor_service()
    success = await monitor.remove_alert(alert_id)
    if not success:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"status": "deleted", "alert_id": alert_id}


@router.get("/{alert_id}")
async def get_alert(alert_id: str) -> AlertResponse:
    """Fetch a single alert by ID."""
    monitor = get_monitor_service()
    alert = monitor.get_alert(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _to_response(alert)


@router.put("/{alert_id}/enable")
async def enable_alert(alert_id: str) -> AlertResponse:
    """Re-enable a disabled alert."""
    monitor = get_monitor_service()
    alert = await monitor.set_alert_enabled(alert_id, True)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _to_response(alert)


@router.put("/{alert_id}/disable")
async def disable_alert(alert_id: str) -> AlertResponse:
    """Pause an alert without deleting it."""
    monitor = get_monitor_service()
    alert = await monitor.set_alert_enabled(alert_id, False)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _to_response(alert)


@router.put("/{alert_id}/profile")
async def assign_profile(alert_id: str, request: AssignProfileRequest) -> AlertResponse:
    """Assign (or clear) a checkout profile to an alert for sniper-mode auto-ordering.

    Pass `profile_id: null` to clear the assignment.
    """
    monitor = get_monitor_service()
    alert = monitor.get_alert(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    storage = get_storage()
    if request.profile_id is not None:
        profile = storage.load_profile(request.profile_id, account_id=alert.account_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")
    alert.auto_order_profile_id = request.profile_id
    try:
        storage.set_alert_profile(alert_id, request.profile_id)
    except Exception:
        # Best-effort persistence - the in-memory assignment still works.
        pass
    return _to_response(alert)
