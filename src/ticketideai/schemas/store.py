"""Pydantic models for the TicketideAI store API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ticketideai.schemas.operations import ChangeSet


class StoreModel(BaseModel):
    """Base model that can be built directly from database rows."""

    model_config = ConfigDict(from_attributes=True)


class Product(StoreModel):
    sku: str
    name: str
    category: str
    price: float
    stock: int


class Customer(StoreModel):
    id: int
    name: str
    email: str


class OrderLine(StoreModel):
    sku: str
    quantity: int
    unit_price: float


class Order(StoreModel):
    id: int
    customer_id: int
    status: str
    ordered_at: datetime
    note: str = ""
    lines: list[OrderLine] = Field(default_factory=list)


class ReturnRecord(StoreModel):
    id: int
    order_id: int
    sku: str
    quantity: int
    reason: str
    status: str


class RefundRecord(StoreModel):
    id: int
    order_id: int
    amount: float
    status: str


class RestockRecord(StoreModel):
    sku: str
    quantity: int
    stock_after: int


class SupportRequest(BaseModel):
    """An inbound message from an identified customer."""

    customer_email: str
    message: str


class PlanRequest(BaseModel):
    """Everything the planner sees when writing or correcting a plan."""

    customer_email: str
    message: str
    store_context: dict[str, Any] = Field(default_factory=dict)
    previous_source: str | None = None
    reflection_error: str | None = None
    reflection_attempt: int = 0


class PlanResponse(BaseModel):
    """A plain-English summary and the `plan(api)` source implementing it."""

    summary: str
    source: str


class ApprovalItem(StoreModel):
    """A change set waiting for, or resolved by, a human reviewer."""

    id: int
    customer_email: str
    message: str
    plan_source: str
    reasons: list[str] = Field(default_factory=list)
    change_set: ChangeSet
    change_set_digest: str
    diff: dict[str, Any] = Field(default_factory=dict)
    diff_text: str
    status: Literal["pending", "approved", "rejected", "stale"]
    reviewer: str | None = None
    customer_notice: str | None = None
