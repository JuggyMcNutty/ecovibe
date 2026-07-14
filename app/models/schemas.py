"""Pydantic request/response models for the API layer.

All models are snake_case (the public API contract). OVH's own camelCase
wire shapes are passed through as plain dicts where needed; prices from
OVH are raw integers in microcents (divide by 10^8 for currency units).

All durations use ISO 8601 period strings (e.g. "P1M" = 1 month).
"""
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Request bodies (snake_case, the public API contract)
# ---------------------------------------------------------------------------

class AlertCreate(BaseModel):
    """Body for POST /api/alerts."""
    plan_code: str
    fqn_pattern: str = "*"


class AlertResponse(BaseModel):
    """Response shape for alert endpoints."""
    id: str
    plan_code: str
    fqn_pattern: str
    enabled: bool
    notified_at: str | None = None
    auto_order_profile_id: str | None = None
    # Set by PUT /disable when pausing the alert also disarmed its sniper
    # (a paused alert is never polled, so an armed sniper on it is dead).
    sniper_disarmed: bool | None = None


class AssignProfileRequest(BaseModel):
    """Body for PUT /api/alerts/{id}/profile. Pass null to clear."""
    profile_id: str | None = None


class PollIntervalRequest(BaseModel):
    """Body for PUT /api/monitor/poll-interval. Clamped to [1, 60] seconds."""
    poll_interval: int = Field(..., ge=1, le=60)
