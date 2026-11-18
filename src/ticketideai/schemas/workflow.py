"""Pydantic models describing policy decisions and support-request outcomes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ticketideai.schemas.operations import ChangeSet

PolicyAction = Literal["approve", "escalate", "block"]
Outcome = Literal[
    "committed", "answered", "escalated", "refused", "needs_clarification", "failed_safely"
]
STATE_CHANGED_ERROR = "state changed, re-plan"
TRANSIENT_STORE_ERROR = "store temporarily unavailable, re-plan"


class PolicyFinding(BaseModel):
    """One policy rule that fired, and what it requires."""

    rule: str
    action: Literal["escalate", "block"]
    message: str
    operation_index: int | None = None


class PolicyDecision(BaseModel):
    """Overall verdict on a change set: any block wins over any escalation."""

    action: PolicyAction
    findings: list[PolicyFinding] = Field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        """Return the human-readable reason for each finding."""
        return [finding.message for finding in self.findings]

    def fired(self, rule: str) -> bool:
        """Return True when the named rule produced a finding."""
        return any(finding.rule == rule for finding in self.findings)


class ReplyFacts(BaseModel):
    """Everything the responder may state to the customer."""

    outcome: Outcome
    customer_message: str
    refunds_issued: list[dict[str, Any]] = Field(default_factory=list)
    refunds_pending_approval: list[dict[str, Any]] = Field(default_factory=list)
    returns_created: list[dict[str, Any]] = Field(default_factory=list)
    returns_pending_approval: list[dict[str, Any]] = Field(default_factory=list)
    status_changes: list[dict[str, Any]] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    clarifying_question: str | None = None
    answer: dict[str, Any] = Field(default_factory=dict)


class CustomerReply(BaseModel):
    """The reply text sent back to the customer."""

    body: str


class WorkflowResult(BaseModel):
    """Final record of one handled support request."""

    outcome: Outcome
    reply: str = ""
    policy_action: PolicyAction | None = None
    findings: list[PolicyFinding] = Field(default_factory=list)
    plan_summary: str = ""
    plan_source: str = ""
    change_set: ChangeSet = Field(default_factory=ChangeSet)
    diff: dict[str, Any] = Field(default_factory=dict)
    diff_text: str = ""
    plan_result: dict[str, Any] = Field(default_factory=dict)
    approval_id: int | None = None
    reflection_attempts: int = 0
    errors: list[str] = Field(default_factory=list)
    failed_plans: list[str] = Field(default_factory=list)
    reply_replaced_by_guard: bool = False
    reply_draft: str = ""
    guard_problems: list[str] = Field(default_factory=list)

    @property
    def committed(self) -> bool:
        """True when the change set was written to the store."""
        return self.outcome == "committed"
