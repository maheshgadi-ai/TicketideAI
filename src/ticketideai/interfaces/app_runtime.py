"""Runtime services and table data for the Streamlit interface."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ticketideai.config import Settings
from ticketideai.interfaces.demo import build_demo_workflow
from ticketideai.llm import ModelRouter
from ticketideai.observability.tracing import build_tracer
from ticketideai.orchestrator.approvals import ApprovalOutcome, ApprovalService
from ticketideai.orchestrator.workflow import SupportWorkflow
from ticketideai.schemas.store import ApprovalItem, SupportRequest
from ticketideai.schemas.workflow import WorkflowResult
from ticketideai.tools.database import sqlite_path, sqlite_url
from ticketideai.tools.diffing import snapshot
from ticketideai.tools.effects import to_cents
from ticketideai.tools.seed_store import SEED_DATE, generate_store


@dataclass(frozen=True)
class AppRuntime:
    """The workflow and staff services behind one running app."""

    live: bool
    database_url: str
    workflow: SupportWorkflow
    approvals: ApprovalService

    def handle(self, customer_email: str, message: str) -> WorkflowResult:
        """Handle one customer message."""
        return self.workflow.handle(SupportRequest(customer_email=customer_email, message=message))

    def pending(self) -> list[ApprovalItem]:
        """Return change sets waiting for a human decision."""
        return self.approvals.queue.list_pending()

    def approve(self, item_id: int, reviewer: str) -> ApprovalOutcome:
        """Commit exactly the reviewed change set."""
        return self.approvals.approve(item_id, reviewer)

    def reject(self, item_id: int, reviewer: str) -> ApprovalOutcome:
        """Close a change set without applying it."""
        return self.approvals.reject(item_id, reviewer)


def build_runtime(settings: Settings) -> AppRuntime:
    """Use live models when both keys are set; otherwise run the scripted demo."""
    if settings.api_key("openai") and settings.api_key("anthropic"):
        return _live_runtime(settings)
    work = Path(tempfile.mkdtemp(prefix="ticketide-app-"))
    database = work / "store.sqlite"
    generate_store(database)
    url = f"sqlite:///{database}"
    return AppRuntime(False, url, build_demo_workflow(url), _approvals(url, lambda: SEED_DATE))


def _live_runtime(settings: Settings) -> AppRuntime:
    url = sqlite_url(settings.database_url)
    if not sqlite_path(url).exists():
        generate_store(sqlite_path(url))
    tracer = build_tracer(settings)
    workflow = SupportWorkflow(
        ModelRouter.from_settings(settings, tracer=tracer),
        url,
        tracer=tracer,
        max_reflections=settings.max_reflections,
        clock=settings.now,
    )
    return AppRuntime(True, url, workflow, _approvals(url, settings.now))


def _approvals(url: str, clock: Callable[[], datetime]) -> ApprovalService:
    return ApprovalService(url, clock=clock)


def customer_emails(database_url: str) -> list[str]:
    """Return every customer email, in account order."""
    customers = _tables(database_url)["customers"]
    return [row["email"] for row in sorted(customers.values(), key=lambda row: row["id"])]


def orders_table(database_url: str) -> list[dict[str, Any]]:
    """Return one row per order with its customer, total, and refunds."""
    tables = _tables(database_url)
    paid: dict[int, int] = {}
    for line in tables["order_lines"].values():
        cents = to_cents(line["unit_price"]) * line["quantity"]
        paid[line["order_id"]] = paid.get(line["order_id"], 0) + cents
    refunded: dict[int, int] = {}
    for row in tables["refunds"].values():
        refunded[row["order_id"]] = refunded.get(row["order_id"], 0) + to_cents(row["amount"])
    return [
        {
            "order": order["id"],
            "customer": tables["customers"][str(order["customer_id"])]["email"],
            "status": order["status"],
            "ordered": order["ordered_at"][:10],
            "paid": paid.get(order["id"], 0) / 100,
            "refunded": refunded.get(order["id"], 0) / 100,
        }
        for order in sorted(tables["orders"].values(), key=lambda row: row["id"])
    ]


def inventory_table(database_url: str) -> list[dict[str, Any]]:
    """Return the product catalog with current stock."""
    products = _tables(database_url)["products"]
    return [
        {key: row[key] for key in ("sku", "name", "category", "price", "stock")}
        for row in sorted(products.values(), key=lambda row: row["sku"])
    ]


def _tables(database_url: str) -> dict[str, dict[str, dict[str, Any]]]:
    return snapshot(sqlite_path(database_url))
