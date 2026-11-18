"""Translate a workflow result into the facts the responder may state."""

from __future__ import annotations

from typing import Any

from ticketideai.schemas.operations import CreateReturn, IssueRefund, UpdateOrderStatus
from ticketideai.schemas.workflow import ReplyFacts, WorkflowResult


def build_reply_facts(customer_message: str, result: WorkflowResult) -> ReplyFacts:
    """Separate completed effects from pending ones according to the real outcome."""
    operations = result.change_set.operations
    refunds = [
        {"order_id": op.order_id, "amount": op.amount_cents / 100}
        for op in operations
        if isinstance(op, IssueRefund)
    ]
    returns = [
        {"order_id": op.order_id, "sku": op.sku, "quantity": op.quantity}
        for op in operations
        if isinstance(op, CreateReturn)
    ]
    statuses = [
        {"order_id": op.order_id, "status": op.new_status}
        for op in operations
        if isinstance(op, UpdateOrderStatus)
    ]
    committed, escalated = result.outcome == "committed", result.outcome == "escalated"
    return ReplyFacts(
        outcome=result.outcome,
        customer_message=customer_message,
        refunds_issued=refunds if committed else [],
        refunds_pending_approval=refunds if escalated else [],
        returns_created=returns if committed else [],
        returns_pending_approval=returns if escalated else [],
        status_changes=statuses if committed else [],
        reasons=_reasons(result),
        clarifying_question=_text(result.plan_result.get("question")),
        answer=result.plan_result if result.outcome == "answered" else {},
    )


def _reasons(result: WorkflowResult) -> list[str]:
    reasons = [finding.message for finding in result.findings]
    declared = _text(result.plan_result.get("reason"))
    return [*reasons, declared] if declared else reasons


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
