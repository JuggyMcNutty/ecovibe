"""Sniper mode - arm/disarm per-alert auto-ordering."""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.monitor import get_monitor_service, get_sniper_service
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sniper", tags=["sniper"])


class SniperArmRequest(BaseModel):
    alert_id: str
    profile_id: str


@router.get("/status")
async def status() -> dict:
    sniper = get_sniper_service()
    monitor = get_monitor_service()
    alerts = {a.id: a for a in monitor.get_alerts()}
    snapshot = sniper.status()
    armed = snapshot["armed"]
    for a in armed:
        aid = a["alert_id"]
        if aid in alerts:
            a["plan_code"] = alerts[aid].plan_code
    return {
        "armed": armed,
        "results": snapshot["results"],
    }


@router.post("/arm")
async def arm(req: SniperArmRequest) -> dict:
    monitor = get_monitor_service()
    alert = monitor.get_alert(req.alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    storage = get_storage()
    profile = storage.load_profile(req.profile_id, account_id=alert.account_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    sniper = get_sniper_service()
    sniper.arm(req.alert_id, req.profile_id)
    return {"status": "armed", "alert_id": req.alert_id, "profile_id": req.profile_id}


@router.post("/disarm/{alert_id}")
async def disarm(alert_id: str) -> dict:
    sniper = get_sniper_service()
    sniper.disarm(alert_id)
    return {"status": "disarmed", "alert_id": alert_id}
