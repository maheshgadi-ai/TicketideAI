"""Typed effects a plan proposes, each carrying the store facts it relied on."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

MAX_RECORDED_OPERATIONS = 200
MAX_MESSAGE_CHARS = 2000
MAX_REASON_CHARS = 500


class FrozenModel(BaseModel):
    """Immutable model that rejects unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class OrderFacts(FrozenModel):
    """Order state observed immediately before an operation."""

    customer_id: StrictInt
    status: StrictStr
    ordered_at: datetime
    paid_cents: StrictInt
    refunded_cents: StrictInt
    returned_value_cents: StrictInt


class ReturnFacts(OrderFacts):
    """Order state plus the purchased line an item return applies to."""

    category: StrictStr
    purchased_quantity: StrictInt
    already_returned: StrictInt
    unit_price_cents: StrictInt


class RestockFacts(FrozenModel):
    """Product stock observed immediately before a restock."""

    stock_before: StrictInt


class MessageFacts(FrozenModel):
    """Recipient identity observed immediately before a customer message."""

    customer_id: StrictInt


class CreateReturn(FrozenModel):
    """Open a return for units of one purchased SKU."""

    kind: Literal["create_return"] = "create_return"
    order_id: StrictInt
    sku: StrictStr
    quantity: StrictInt
    reason: StrictStr = Field(max_length=MAX_REASON_CHARS)
    facts: ReturnFacts


class IssueRefund(FrozenModel):
    """Refund an amount, in cents, against one order."""

    kind: Literal["issue_refund"] = "issue_refund"
    order_id: StrictInt
    amount_cents: StrictInt
    facts: OrderFacts


class Restock(FrozenModel):
    """Add units of one SKU back to inventory."""

    kind: Literal["restock"] = "restock"
    sku: StrictStr
    quantity: StrictInt
    facts: RestockFacts


class UpdateOrderStatus(FrozenModel):
    """Move one order to a new status."""

    kind: Literal["update_order_status"] = "update_order_status"
    order_id: StrictInt
    new_status: StrictStr = Field(max_length=32)
    facts: OrderFacts


class SendCustomerMessage(FrozenModel):
    """Store one outbound message for a customer."""

    kind: Literal["send_customer_message"] = "send_customer_message"
    customer_email: StrictStr
    body: StrictStr = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    facts: MessageFacts


AnyOperation = CreateReturn | IssueRefund | Restock | UpdateOrderStatus | SendCustomerMessage
Operation = Annotated[AnyOperation, Field(discriminator="kind")]


class ChangeSet(FrozenModel):
    """Ordered operations recorded during one sandboxed dry run."""

    operations: tuple[Operation, ...] = Field(default=(), max_length=MAX_RECORDED_OPERATIONS)

    @property
    def is_empty(self) -> bool:
        """True when the plan proposed no writes."""
        return not self.operations

    def digest(self) -> str:
        """Return a SHA-256 fingerprint of the exact recorded operations."""
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()
