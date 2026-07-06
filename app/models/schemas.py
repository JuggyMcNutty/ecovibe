"""Pydantic request/response models for the API layer.

Two naming conventions coexist intentionally:
  - camelCase models (Price, Cart, CartItem, ...) mirror OVH's wire format
    and are used to document response shapes (and may be attached as
    `response_model` in future).
  - snake_case models (AddServerRequest, CheckoutRequest, ...) are the
    request bodies the API accepts — Python-idiomatic for the public API.

All durations use ISO 8601 period strings (e.g. "P1M" = 1 month).
"""
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# OVH response-shape models (camelCase, mirroring OVH's wire format)
# ---------------------------------------------------------------------------

class Price(BaseModel):
    """OVH price object. `priceInUcents` is in microcents of euro (1€ = 1_000_000)."""
    currencyCode: str
    priceInUcents: int
    text: str
    value: float


class CartItem(BaseModel):
    """A line item in a cart."""
    itemId: int
    planCode: str
    duration: str
    quantity: int
    prices: list[dict[str, Any]]


class Cart(BaseModel):
    """An OVH shopping cart."""
    cartId: str
    description: str | None = ""
    expire: str
    readOnly: bool
    items: list[dict[str, Any]] = []


class ServerOption(BaseModel):
    """An addon option (RAM/storage/bandwidth upgrade) for an ECO server."""
    planCode: str
    label: str
    invoiceName: str
    price: Price


class PlanInfo(BaseModel):
    """A catalog plan entry."""
    planCode: str
    invoiceName: str
    durations: list[str]
    price: Price


class ServerAvailability(BaseModel):
    """One orderable FQN configuration for a plan."""
    planCode: str
    fqn: str
    referencePrice: Price | None = None


class OrderSummary(BaseModel):
    """Checkout summary returned by OVH."""
    details: list[dict[str, Any]]
    prices: dict[str, Any]
    orderId: int | None = None
    url: str | None = None


# ---------------------------------------------------------------------------
# Request bodies (snake_case, the public API contract)
# ---------------------------------------------------------------------------

class CreateCartRequest(BaseModel):
    """Body for POST /api/cart."""
    description: str = ""


class AddServerRequest(BaseModel):
    """Body for POST /api/cart/{id}/server."""
    plan_code: str
    duration: str = "P1M"
    quantity: int = 1


class AddOptionRequest(BaseModel):
    """Body for POST /api/cart/{id}/options."""
    item_id: int
    plan_code: str
    duration: str = "P1M"


class AddConfigRequest(BaseModel):
    """Body for POST /api/cart/{id}/config."""
    item_id: int
    label: str
    value: str


class CheckoutRequest(BaseModel):
    """Body for POST /api/checkout/{id}."""
    auto_pay: bool = False
    waive_retractation: bool = False


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


class AssignProfileRequest(BaseModel):
    """Body for PUT /api/alerts/{id}/profile. Pass null to clear."""
    profile_id: str | None = None


class PollIntervalRequest(BaseModel):
    """Body for PUT /api/monitor/poll-interval. Clamped to [1, 10] seconds."""
    poll_interval: int = Field(..., ge=1, le=10)
