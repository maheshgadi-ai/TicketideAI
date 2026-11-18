"""Customer notices for staff decisions, written only from recorded facts."""

from __future__ import annotations

from ticketideai.schemas.operations import (
    ChangeSet,
    CreateReturn,
    IssueRefund,
    UpdateOrderStatus,
)


def approved_notice(change_set: ChangeSet) -> str:
    """Describe exactly what an approved change set committed."""
    details: list[str] = []
    for operation in change_set.operations:
        if isinstance(operation, CreateReturn):
            details.append(
                f"Return of {operation.quantity} x {operation.sku} "
                f"from order {operation.order_id} opened."
            )
        elif isinstance(operation, IssueRefund):
            details.append(
                f"Refund of ${operation.amount_cents / 100:.2f} "
                f"on order {operation.order_id} issued."
            )
        elif isinstance(operation, UpdateOrderStatus):
            details.append(f"Order {operation.order_id} is now {operation.new_status}.")
    return " ".join(["A team member approved your request.", *details])


def rejected_notice() -> str:
    """Tell the customer a reviewer declined the request."""
    return (
        "A team member reviewed your request and could not approve it. "
        "Nothing on your order was changed."
    )


def stale_notice() -> str:
    """Tell the customer the request expired because the order changed during review."""
    return (
        "We could not complete your request because your order changed while it was waiting "
        "for review. Nothing further was changed. Please contact us if you still need help."
    )
