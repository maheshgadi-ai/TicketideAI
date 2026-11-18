"""Build the read-only store snapshot shown to the planner."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ticketideai.tools.database import ProductRow
from ticketideai.tools.effects import order_facts
from ticketideai.tools.store_api import StoreAPI


def build_store_context(database_url: str, customer_email: str, as_of: datetime) -> dict[str, Any]:
    """Return the requester's orders and the product catalog.

    Order notes are customer-supplied text, so they are labeled as untrusted data.
    """
    api = StoreAPI(database_url, scope_email=customer_email)
    customer = api.find_customer(customer_email)
    with Session(api.engine) as session:
        orders = [_order_entry(session, order, as_of) for order in api.list_orders(customer.email)]
        products = session.scalars(select(ProductRow).order_by(ProductRow.sku)).all()
        catalog = [
            {
                "sku": product.sku,
                "name": product.name,
                "category": product.category,
                "price": product.price,
                "stock": product.stock,
            }
            for product in products
        ]
    return {
        "today": as_of.date().isoformat(),
        "customer": {"id": customer.id, "name": customer.name, "email": customer.email},
        "orders": orders,
        "catalog": catalog,
    }


def _order_entry(session: Session, order: Any, as_of: datetime) -> dict[str, Any]:
    facts = order_facts(session, order.id)
    return {
        "order_id": order.id,
        "status": order.status,
        "ordered_on": order.ordered_at.date().isoformat(),
        "age_days": (as_of - order.ordered_at).days,
        "paid": facts.paid_cents / 100,
        "already_refunded": facts.refunded_cents / 100,
        "lines": [line.model_dump() for line in order.lines],
        "untrusted_customer_note": order.note,
    }
