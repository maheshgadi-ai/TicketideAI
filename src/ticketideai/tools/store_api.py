"""Typed store operations for support plans.

Reads work in every mode. Writes exist only in recording mode, where each one is applied
to the sandbox's temporary database and appended to a change set as a typed operation.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ticketideai.schemas.operations import (
    MAX_RECORDED_OPERATIONS,
    AnyOperation,
    ChangeSet,
    CreateReturn,
    IssueRefund,
    Restock,
    SendCustomerMessage,
    UpdateOrderStatus,
)
from ticketideai.schemas.store import (
    Customer,
    Order,
    OrderLine,
    Product,
    RefundRecord,
    RestockRecord,
    ReturnRecord,
)
from ticketideai.tools.database import (
    Base,
    CustomerRow,
    OrderRow,
    ProductRow,
    read_engine,
)
from ticketideai.tools.effects import (
    apply_operation,
    message_facts,
    order_facts,
    restock_facts,
    return_facts,
    to_cents,
)
from ticketideai.tools.errors import StoreError

MAX_QUANTITY = 1_000_000
MAX_AMOUNT = 1_000_000_000


class ChangeRecorder:
    """Collect the operations performed during one dry run."""

    def __init__(self, limit: int = MAX_RECORDED_OPERATIONS) -> None:
        self.limit = limit
        self.operations: list[AnyOperation] = []

    def add(self, operation: AnyOperation) -> None:
        """Append one operation, refusing to grow past the recording limit."""
        if len(self.operations) >= self.limit:
            raise StoreError(f"plan exceeded the limit of {self.limit} recorded operations")
        self.operations.append(operation)

    def change_set(self) -> ChangeSet:
        """Return everything recorded so far."""
        return ChangeSet(operations=tuple(self.operations))


class StoreAPI:
    """The only operations a support plan may invoke.

    Args:
        database_url: SQLite URL or path.
        scope_email: When set, only this customer's records are visible.
        recorder: When set, writes are enabled and recorded.
    """

    def __init__(
        self,
        database_url: str,
        *,
        scope_email: str | None = None,
        recorder: ChangeRecorder | None = None,
    ) -> None:
        self.engine = read_engine(database_url)
        self._scope_email = scope_email.strip().lower() if scope_email else None
        self._recorder = recorder

    def initialize(self) -> None:
        """Create tables for an empty store database."""
        Base.metadata.create_all(self.engine)

    def find_customer(self, email: str) -> Customer:
        """Return a customer by email, or raise `StoreError`."""
        normalized = _text(email, "email").strip().lower()
        with self._session() as session:
            row = session.scalar(select(CustomerRow).where(CustomerRow.email == normalized))
            if row is None or (self._scope_email and normalized != self._scope_email):
                raise StoreError(f"customer not found: {email}")
            return Customer.model_validate(row)

    def get_order(self, order_id: int) -> Order:
        """Return one order with its lines, or raise `StoreError`."""
        with self._session() as session:
            return self._order(self._visible_order(session, order_id))

    def list_orders(self, customer_email: str) -> list[Order]:
        """Return a customer's orders, oldest first."""
        customer = self.find_customer(customer_email)
        with self._session() as session:
            rows = session.scalars(
                select(OrderRow)
                .where(OrderRow.customer_id == customer.id)
                .order_by(OrderRow.ordered_at)
            ).all()
            return [self._order(row) for row in rows]

    def check_stock(self, sku: str) -> Product:
        """Return a product and its current stock, or raise `StoreError`."""
        with self._session() as session:
            row = session.get(ProductRow, _text(sku, "sku"))
            if row is None:
                raise StoreError(f"product not found: {sku}")
            return Product.model_validate(row)

    def create_return(self, order_id: int, sku: str, quantity: int, reason: str) -> ReturnRecord:
        """Record a proposed return of purchased units."""
        with self._session() as session:
            self._visible_order(session, order_id)
            operation = _build(
                CreateReturn,
                order_id=order_id,
                sku=_text(sku, "sku"),
                quantity=_quantity(quantity),
                reason=_text(reason, "reason"),
                facts=return_facts(session, order_id, sku),
            )
            return ReturnRecord.model_validate(self._record(session, operation))

    def issue_refund(self, order_id: int, amount: float) -> RefundRecord:
        """Record a proposed refund against an order."""
        with self._session() as session:
            self._visible_order(session, order_id)
            operation = _build(
                IssueRefund,
                order_id=order_id,
                amount_cents=_amount_cents(amount),
                facts=order_facts(session, order_id),
            )
            return RefundRecord.model_validate(self._record(session, operation))

    def restock(self, sku: str, quantity: int) -> RestockRecord:
        """Record a proposed inventory increase."""
        with self._session() as session:
            operation = _build(
                Restock,
                sku=_text(sku, "sku"),
                quantity=_quantity(quantity),
                facts=restock_facts(session, sku),
            )
            return RestockRecord.model_validate(self._record(session, operation))

    def update_order_status(self, order_id: int, status: str) -> Order:
        """Record a proposed order status change."""
        with self._session() as session:
            self._visible_order(session, order_id)
            operation = _build(
                UpdateOrderStatus,
                order_id=order_id,
                new_status=_text(status, "status"),
                facts=order_facts(session, order_id),
            )
            return self._order(self._record(session, operation))

    def send_customer_message(self, customer_email: str, body: str) -> dict[str, str | int]:
        """Record a proposed message to a customer."""
        customer = self.find_customer(customer_email)
        text = _text(body, "body").strip()
        if not text:
            raise StoreError("customer message cannot be empty")
        with self._session() as session:
            operation = _build(
                SendCustomerMessage,
                customer_email=customer.email,
                body=text,
                facts=message_facts(session, customer.email),
            )
            row: Any = self._record(session, operation)
            return {"id": row.id, "customer_id": row.customer_id, "body": row.body}

    def _record(self, session: Session, operation: AnyOperation) -> Any:
        if self._recorder is None:
            raise StoreError("store writes are only available in recording mode")
        self._recorder.add(operation)
        return apply_operation(session, operation)

    def _visible_order(self, session: Session, order_id: int) -> OrderRow:
        if not isinstance(order_id, int) or isinstance(order_id, bool):
            raise StoreError("order_id must be an integer")
        row = session.get(OrderRow, order_id)
        if row is None or not self._in_scope(session, row):
            raise StoreError(f"order not found: {order_id}")
        return row

    def _in_scope(self, session: Session, order: OrderRow) -> bool:
        if self._scope_email is None:
            return True
        owner = session.get(CustomerRow, order.customer_id)
        return owner is not None and owner.email == self._scope_email

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with Session(self.engine) as session, session.begin():
            yield session

    @staticmethod
    def _order(row: OrderRow) -> Order:
        lines = [
            OrderLine(sku=line.sku, quantity=line.quantity, unit_price=line.unit_price)
            for line in row.lines
        ]
        return Order(
            id=row.id,
            customer_id=row.customer_id,
            status=row.status,
            ordered_at=row.ordered_at,
            note=row.note,
            lines=lines,
        )


def _build(model: type[Any], **values: Any) -> Any:
    try:
        return model(**values)
    except ValidationError as exc:
        details = "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors())
        raise StoreError(f"invalid {model.__name__}: {details}") from exc


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise StoreError(f"{name} must be a string")
    return value


def _quantity(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise StoreError("quantity must be an integer")
    if abs(value) > MAX_QUANTITY:
        raise StoreError("quantity is out of range")
    return value


def _amount_cents(value: object) -> int:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise StoreError("refund amount must be a number")
    if not math.isfinite(value) or abs(value) > MAX_AMOUNT:
        raise StoreError("refund amount is out of range")
    return to_cents(value)
