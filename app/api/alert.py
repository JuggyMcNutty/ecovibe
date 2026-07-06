from typing import Any

from fastapi import APIRouter, HTTPException

from app.models.schemas import AlertCreate, AlertResponse
from app.services.monitor import DuplicateAlertError, get_monitor_service

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _to_response(alert) -> AlertResponse:
    return AlertResponse(
        id=alert.id,
        plan_code=alert.plan_code,
        fqn_pattern=alert.fqn_pattern,
        enabled=alert.enabled,
        notified_at=alert.notified_at.isoformat() if alert.notified_at else None,
    )


@router.post("", status_code=201)
async def create_alert(request: AlertCreate) -> AlertResponse:
    monitor = get_monitor_service()
    try:
        alert = await monitor.add_alert(request.plan_code, request.fqn_pattern)
    except DuplicateAlertError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return _to_response(alert)


@router.get("")
async def list_alerts() -> list[AlertResponse]:
    monitor = get_monitor_service()
    return [_to_response(a) for a in monitor.get_alerts()]


@router.delete("/{alert_id}")
async def delete_alert(alert_id: str) -> dict[str, Any]:
    monitor = get_monitor_service()
    success = await monitor.remove_alert(alert_id)
    if not success:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"status": "deleted", "alert_id": alert_id}


@router.get("/{alert_id}")
async def get_alert(alert_id: str) -> AlertResponse:
    monitor = get_monitor_service()
    alert = monitor.get_alert(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _to_response(alert)


@router.put("/{alert_id}/enable")
async def enable_alert(alert_id: str) -> AlertResponse:
    monitor = get_monitor_service()
    alert = await monitor.set_alert_enabled(alert_id, True)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _to_response(alert)


@router.put("/{alert_id}/disable")
async def disable_alert(alert_id: str) -> AlertResponse:
    monitor = get_monitor_service()
    alert = await monitor.set_alert_enabled(alert_id, False)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _to_response(alert)
