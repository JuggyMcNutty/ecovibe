from typing import Any

from pydantic import BaseModel, Field


class Price(BaseModel):
    currencyCode: str
    priceInUcents: int
    text: str
    value: float


class CartItem(BaseModel):
    itemId: int
    planCode: str
    duration: str
    quantity: int
    prices: list[dict[str, Any]]


class Cart(BaseModel):
    cartId: str
    description: str | None = ""
    expire: str
    readOnly: bool
    items: list[dict[str, Any]] = []


class ServerOption(BaseModel):
    planCode: str
    label: str
    invoiceName: str
    price: Price


class PlanInfo(BaseModel):
    planCode: str
    invoiceName: str
    durations: list[str]
    price: Price


class ServerAvailability(BaseModel):
    planCode: str
    fqn: str
    referencePrice: Price | None = None


class OrderSummary(BaseModel):
    details: list[dict[str, Any]]
    prices: dict[str, Any]
    orderId: int | None = None
    url: str | None = None


class CreateCartRequest(BaseModel):
    description: str = ""


class AddServerRequest(BaseModel):
    plan_code: str
    duration: str = "P1M"
    quantity: int = 1


class AddOptionRequest(BaseModel):
    item_id: int
    plan_code: str
    duration: str = "P1M"


class AddConfigRequest(BaseModel):
    item_id: int
    label: str
    value: str


class CheckoutRequest(BaseModel):
    auto_pay: bool = False
    waive_retractation: bool = False


class AlertCreate(BaseModel):
    plan_code: str
    fqn_pattern: str = "*"


class AlertResponse(BaseModel):
    id: str
    plan_code: str
    fqn_pattern: str
    enabled: bool
    notified_at: str | None = None
    auto_order_profile_id: str | None = None


class AssignProfileRequest(BaseModel):
    profile_id: str | None = None


class PollIntervalRequest(BaseModel):
    poll_interval: int = Field(..., ge=1, le=10)
