from typing import Any, Dict, List, Optional

from pydantic import BaseModel


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
    prices: List[Dict[str, Any]]


class Cart(BaseModel):
    cartId: str
    description: Optional[str] = ""
    expire: str
    readOnly: bool
    items: List[Dict[str, Any]] = []


class ServerOption(BaseModel):
    planCode: str
    label: str
    invoiceName: str
    price: Price


class PlanInfo(BaseModel):
    planCode: str
    invoiceName: str
    durations: List[str]
    price: Price


class ServerAvailability(BaseModel):
    planCode: str
    fqn: str
    referencePrice: Optional[Price] = None


class OrderSummary(BaseModel):
    details: List[Dict[str, Any]]
    prices: Dict[str, Any]
    orderId: Optional[int] = None
    url: Optional[str] = None


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
