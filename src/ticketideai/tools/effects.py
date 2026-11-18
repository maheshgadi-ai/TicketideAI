"""Capture, verify, and apply recorded operations. This is the only store write path."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ticketideai.schemas.operations import (
    AnyOperation,
    ChangeSet,
    CreateReturn,
    IssueRefund,
    MessageFacts,
    OrderFacts,
    Restock,
    RestockFacts,
    ReturnFacts,
    UpdateOrderStatus,
)
from ticketideai.tools.database import (
    CustomerRow,
    MessageRow,
    OrderLineRow,
    OrderRow,
    ProductRow,
    RefundRow,
    RestockRow,
    ReturnRow,
    write_engine,
)
from ticketideai.tools.errors import PreconditionFailed, StoreError


def to_cents(amount: float | int) -> int:
    """Convert a dollar amount to integer cents, rounding half up."""
    return int(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def order_facts(session: Session, order_id: int) -> OrderFacts:
    """Read the order values that policy and commit depend on."""
    return OrderFacts(**_order_values(session, order_id))


def return_facts(session: Session, order_id: int, sku: str) -> ReturnFacts:
    """Read order values plus the purchased line for one SKU."""
    values = _order_values(session, order_id)
    lines = session.scalars(
        select(OrderLineRow).where(OrderLineRow.order_id == order_id, OrderLineRow.sku == sku)
    ).all()
    product = session.get(ProductRow, sku)
    if not lines or product is None:
        raise StoreError(f"sku {sku} was not purchased in order {order_id}")
    returned = session.scalars(
        select(ReturnRow.quantity).where(ReturnRow.order_id == order_id, ReturnRow.sku == sku)
    ).all()
    return ReturnFacts(
        **values,
        category=product.category,
        purchased_quantity=sum(line.quantity for line in lines),
        already_returned=sum(returned),
        unit_price_cents=min(to_cents(line.unit_price) for line in lines),
    )


def restock_facts(session: Session, sku: str) -> RestockFacts:
    """Read the current stock of a product."""
    product = session.get(ProductRow, sku)
    if product is None:
        raise StoreError(f"product not found: {sku}")
    return RestockFacts(stock_before=product.stock)


def message_facts(session: Session, customer_email: str) -> MessageFacts:
    """Read the identity of a message recipient."""
    email = customer_email.strip().lower()
    customer = session.scalar(select(CustomerRow).where(CustomerRow.email == email))
    if customer is None:
        raise StoreError(f"customer not found: {customer_email}")
    return MessageFacts(customer_id=customer.id)


def capture_facts(session: Session, operation: AnyOperation) -> BaseModel:
    """Read the current store values for the facts an operation recorded."""
    if isinstance(operation, CreateReturn):
        return return_facts(session, operation.order_id, operation.sku)
    if isinstance(operation, IssueRefund | UpdateOrderStatus):
        return order_facts(session, operation.order_id)
    if isinstance(operation, Restock):
        return restock_facts(session, operation.sku)
    return message_facts(session, operation.customer_email)


def verify_preconditions(session: Session, operation: AnyOperation) -> None:
    """Raise `PreconditionFailed` unless the store still matches the recorded facts."""
    try:
        current = capture_facts(session, operation)
    except StoreError as exc:
        raise PreconditionFailed(f"{operation.kind}: {exc}") from exc
    if current == operation.facts:
        return
    recorded, now = operation.facts.model_dump(), current.model_dump()
    changed = [
        f"{key} was {recorded[key]!r}, now {now[key]!r}"
        for key in recorded
        if recorded[key] != now[key]
    ]
    raise PreconditionFailed(f"{operation.kind}: " + "; ".join(changed))


def apply_operation(session: Session, operation: AnyOperation) -> object:
    """Write one operation in the caller's transaction and return the affected row."""
    if isinstance(operation, UpdateOrderStatus):
        order = session.get(OrderRow, operation.order_id)
        if order is None:
            raise StoreError(f"order not found: {operation.order_id}")
        order.status = operation.new_status
        session.flush()
        return order
    row = _new_row(session, operation)
    session.add(row)
    session.flush()
    return row


def record_customer_notice(session: Session, customer_email: str, body: str) -> None:
    """Store a staff decision notice for a customer in the caller's transaction."""
    customer_id = message_facts(session, customer_email).customer_id
    session.add(MessageRow(customer_id=customer_id, body=body))
    session.flush()


def apply_change_set(session: Session, change_set: ChangeSet) -> None:
    """Verify and apply every operation, in order, in the caller's transaction."""
    for operation in change_set.operations:
        verify_preconditions(session, operation)
        apply_operation(session, operation)


def commit_change_set(database_url: str, change_set: ChangeSet) -> None:
    """Apply a change set atomically; any failed precondition rolls everything back."""
    engine = write_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            apply_change_set(session, change_set)
    finally:
        engine.dispose()


def _new_row(session: Session, operation: AnyOperation) -> object:
    if isinstance(operation, CreateReturn):
        return ReturnRow(
            order_id=operation.order_id,
            sku=operation.sku,
            quantity=operation.quantity,
            reason=operation.reason,
            status="approved",
        )
    if isinstance(operation, IssueRefund):
        return RefundRow(
            order_id=operation.order_id, amount=operation.amount_cents / 100, status="issued"
        )
    if isinstance(operation, Restock):
        product = session.get(ProductRow, operation.sku)
        if product is None:
            raise StoreError(f"product not found: {operation.sku}")
        product.stock += operation.quantity
        return RestockRow(sku=operation.sku, quantity=operation.quantity, stock_after=product.stock)
    return MessageRow(customer_id=operation.facts.customer_id, body=operation.body)


def _order_values(session: Session, order_id: int) -> dict[str, object]:
    order = session.get(OrderRow, order_id)
    if order is None:
        raise StoreError(f"order not found: {order_id}")
    prices = {line.sku: to_cents(line.unit_price) for line in order.lines}
    refunds = session.scalars(
        select(RefundRow.amount).where(RefundRow.order_id == order_id, RefundRow.status == "issued")
    ).all()
    returns = session.scalars(select(ReturnRow).where(ReturnRow.order_id == order_id)).all()
    return {
        "customer_id": order.customer_id,
        "status": order.status,
        "ordered_at": order.ordered_at,
        "paid_cents": sum(to_cents(line.unit_price) * line.quantity for line in order.lines),
        "refunded_cents": sum(to_cents(amount) for amount in refunds),
        "returned_value_cents": sum(row.quantity * prices.get(row.sku, 0) for row in returns),
    }
