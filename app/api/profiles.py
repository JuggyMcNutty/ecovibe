"""Saved checkout profile CRUD."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.storage import get_storage

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


class CheckoutProfile(BaseModel):
    name: str
    plan_code: str
    fqn: str
    ram: str | None = None
    storage: str | None = None
    bandwidth: str | None = None
    datacenters: str | None = None
    region: str = "europe"
    os: str = "none_64.en"
    duration: str = "P1M"
    auto_pay: bool = False
    waive_retractation: bool = False
    max_price: int | None = None


class CheckoutProfileResponse(CheckoutProfile):
    id: str
    created_at: str


@router.get("")
async def list_profiles() -> list[dict]:
    storage = get_storage()
    return storage.load_profiles()


@router.post("", status_code=201)
async def create_profile(profile: CheckoutProfile) -> dict:
    storage = get_storage()
    profile_id = str(uuid.uuid4())
    record = profile.model_dump()
    record["id"] = profile_id
    record["created_at"] = datetime.now(timezone.utc).isoformat()
    storage.upsert_profile(record)
    return record


@router.get("/{profile_id}")
async def get_profile(profile_id: str) -> dict:
    storage = get_storage()
    profile = storage.load_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.put("/{profile_id}")
async def update_profile(profile_id: str, profile: CheckoutProfile) -> dict:
    storage = get_storage()
    existing = storage.load_profile(profile_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Profile not found")
    record = profile.model_dump()
    record["id"] = profile_id
    record["created_at"] = existing["created_at"]
    storage.upsert_profile(record)
    return record


@router.delete("/{profile_id}")
async def delete_profile(profile_id: str) -> dict:
    storage = get_storage()
    storage.delete_profile(profile_id)
    return {"status": "deleted", "id": profile_id}
