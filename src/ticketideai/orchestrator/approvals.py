"""Human approval decisions. Only the staff CLI and UI call this module."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel

from ticketideai.schemas.store import ApprovalItem
from ticketideai.tools.approval_queue import ApprovalError, ApprovalQueue
from ticketideai.tools.errors import PreconditionFailed
from ticketideai.tools.policy import PolicyEngine
from ticketideai.tools.store_api import StoreAPI


class ApprovalOutcome(BaseModel):
    """What happened when a reviewer resolved a queue item."""

    item: ApprovalItem
    committed: bool
    message: str


class ApprovalService:
    """Apply or reject exactly the change set a human reviewed."""

    def __init__(
        self,
        database_url: str,
        *,
        policy_engine: PolicyEngine | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.database_url = database_url
        self.queue = ApprovalQueue(database_url)
        self.queue.initialize()
        self.policy_engine = policy_engine or PolicyEngine.from_file()
        self.clock = clock

    def approve(self, item_id: int, reviewer: str) -> ApprovalOutcome:
        """Commit a pending change set after re-checking policy and preconditions."""
        name = _reviewer(reviewer)
        item = self.queue.get(item_id)
        requester = StoreAPI(self.database_url).find_customer(item.customer_email)
        decision = self.policy_engine.evaluate(
            item.change_set, requester_id=requester.id, as_of=self.clock()
        )
        if decision.action == "block":
            stale = self.queue.mark_stale(item_id, name)
            return ApprovalOutcome(item=stale, committed=False, message="; ".join(decision.reasons))
        try:
            approved = self.queue.commit_approved(item_id, name)
        except PreconditionFailed as exc:
            stale = self.queue.mark_stale(item_id, name)
            return ApprovalOutcome(
                item=stale, committed=False, message=f"state changed, re-plan: {exc}"
            )
        return ApprovalOutcome(item=approved, committed=True, message="change set committed")

    def reject(self, item_id: int, reviewer: str) -> ApprovalOutcome:
        """Close a pending change set without applying it."""
        rejected = self.queue.reject(item_id, _reviewer(reviewer))
        return ApprovalOutcome(item=rejected, committed=False, message="change set rejected")


def _reviewer(reviewer: str) -> str:
    name = reviewer.strip()
    if not name:
        raise ApprovalError("a reviewer name is required")
    return name
