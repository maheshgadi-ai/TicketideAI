"""Evaluate store policy against the effects a plan actually produced."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ticketideai.config.models import CONFIG_DIR
from ticketideai.schemas.operations import (
    AnyOperation,
    ChangeSet,
    CreateReturn,
    IssueRefund,
    Restock,
    SendCustomerMessage,
    UpdateOrderStatus,
)
from ticketideai.schemas.workflow import PolicyDecision, PolicyFinding
from ticketideai.tools.effects import to_cents

DEFAULT_POLICY_PATH = CONFIG_DIR / "policy.yaml"


class StorePolicy(BaseModel):
    """Configurable limits behind the policy rules."""

    return_window_days: int = Field(gt=0)
    non_returnable_categories: list[str] = Field(default_factory=list)
    returnable_statuses: list[str] = Field(min_length=1)
    refund_approval_threshold: float = Field(gt=0)
    max_operations_per_plan: int = Field(gt=0)
    agent_settable_statuses: list[str] = Field(default_factory=list)
    status_transitions: dict[str, list[str]]


def load_policy(path: str | Path = DEFAULT_POLICY_PATH) -> StorePolicy:
    """Read and validate a policy file."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return StorePolicy.model_validate(data)


@dataclass
class _OrderLedger:
    """Running refund and return totals for one order across a change set."""

    paid: int
    refunded: int
    returned_value: int
    new_refunds: int = 0


class PolicyEngine:
    """Decide whether a change set commits, escalates to a human, or is blocked."""

    def __init__(self, policy: StorePolicy) -> None:
        self.policy = policy
        self.threshold_cents = to_cents(policy.refund_approval_threshold)

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_POLICY_PATH) -> PolicyEngine:
        """Build an engine from a policy file."""
        return cls(load_policy(path))

    def evaluate(
        self, change_set: ChangeSet, *, requester_id: int, as_of: datetime
    ) -> PolicyDecision:
        """Evaluate every rule; any block outranks any escalation."""
        operations = change_set.operations
        findings: list[PolicyFinding] = []
        if len(operations) > self.policy.max_operations_per_plan:
            findings.append(
                _block(
                    "operation_cap",
                    f"plan proposes {len(operations)} operations; "
                    f"the limit is {self.policy.max_operations_per_plan}",
                )
            )
        for index, operation in enumerate(operations):
            findings += self._check_operation(index, operation, requester_id, as_of)
        findings += self._check_refund_totals(operations)
        findings += self._check_restock_backing(operations)
        return _decide(findings)

    def _check_operation(
        self, index: int, operation: AnyOperation, requester_id: int, as_of: datetime
    ) -> list[PolicyFinding]:
        found = self._check_ownership(index, operation, requester_id)
        if isinstance(operation, CreateReturn):
            found += self._check_return(index, operation, as_of)
        elif isinstance(operation, IssueRefund) and operation.amount_cents <= 0:
            found.append(_block("refund_positive", "refund amount must be positive", index))
        elif isinstance(operation, Restock) and operation.quantity < 1:
            found.append(_block("restock_positive", "restock quantity must be positive", index))
        elif isinstance(operation, UpdateOrderStatus):
            found += self._check_status(index, operation)
        return found

    @staticmethod
    def _check_ownership(
        index: int, operation: AnyOperation, requester_id: int
    ) -> list[PolicyFinding]:
        if isinstance(operation, Restock):
            return []
        if operation.facts.customer_id == requester_id:
            return []
        target = (
            "message recipient"
            if isinstance(operation, SendCustomerMessage)
            else f"order {operation.order_id}"
        )
        return [_block("ownership", f"{target} does not belong to the requester", index)]

    def _check_return(
        self, index: int, operation: CreateReturn, as_of: datetime
    ) -> list[PolicyFinding]:
        facts = operation.facts
        found: list[PolicyFinding] = []
        window = timedelta(days=self.policy.return_window_days)
        if as_of - facts.ordered_at > window:
            message = (
                f"order {operation.order_id} is outside the "
                f"{self.policy.return_window_days}-day return window"
            )
            found.append(_block("return_window", message, index))
        excluded = {category.lower() for category in self.policy.non_returnable_categories}
        if facts.category.lower() in excluded:
            found.append(
                _block("non_returnable", f"{facts.category} items cannot be returned", index)
            )
        if facts.status not in self.policy.returnable_statuses:
            found.append(
                _block("return_status", f"a {facts.status} order cannot be returned", index)
            )
        if operation.quantity < 1:
            found.append(_block("return_quantity", "return quantity must be positive", index))
        elif facts.already_returned + operation.quantity > facts.purchased_quantity:
            remaining = facts.purchased_quantity - facts.already_returned
            message = (
                f"return of {operation.quantity} x {operation.sku} exceeds "
                f"the {remaining} unit(s) still returnable"
            )
            found.append(_block("return_quantity", message, index))
        return found

    def _check_status(self, index: int, operation: UpdateOrderStatus) -> list[PolicyFinding]:
        found: list[PolicyFinding] = []
        current, target = operation.facts.status, operation.new_status
        if target not in self.policy.agent_settable_statuses:
            found.append(
                _block("status_not_settable", f"support cannot set status to {target}", index)
            )
        if target not in self.policy.status_transitions.get(current, []):
            found.append(
                _block("status_transition", f"cannot move from {current} to {target}", index)
            )
        return found

    def _check_refund_totals(self, operations: tuple[AnyOperation, ...]) -> list[PolicyFinding]:
        ledgers = _order_ledgers(operations)
        found: list[PolicyFinding] = []
        for order_id, ledger in ledgers.items():
            if ledger.new_refunds:
                found += self._check_ledger(order_id, ledger)
        plan_total = sum(ledger.new_refunds for ledger in ledgers.values())
        if plan_total > self.threshold_cents:
            message = (
                f"refunds in this plan total {_money(plan_total)}, above the "
                f"{_money(self.threshold_cents)} approval threshold"
            )
            found.append(_escalate("refund_threshold", message))
        return found

    def _check_ledger(self, order_id: int, ledger: _OrderLedger) -> list[PolicyFinding]:
        found: list[PolicyFinding] = []
        total = _money(ledger.refunded)
        if ledger.refunded > ledger.paid:
            message = (
                f"order {order_id} refunds would total {total}, "
                f"more than the {_money(ledger.paid)} paid"
            )
            found.append(_block("refund_exceeds_paid", message))
        if ledger.refunded > self.threshold_cents:
            message = (
                f"order {order_id} refunds would total {total}, above the "
                f"{_money(self.threshold_cents)} approval threshold"
            )
            found.append(_escalate("refund_threshold", message))
        if ledger.refunded > ledger.returned_value:
            message = (
                f"order {order_id} refunds would total {total}, more than the "
                f"{_money(ledger.returned_value)} of returned items"
            )
            found.append(_escalate("refund_without_return", message))
        return found

    @staticmethod
    def _check_restock_backing(operations: tuple[AnyOperation, ...]) -> list[PolicyFinding]:
        returned: Counter[str] = Counter()
        restocked: Counter[str] = Counter()
        for operation in operations:
            if isinstance(operation, CreateReturn) and operation.quantity > 0:
                returned[operation.sku] += operation.quantity
            elif isinstance(operation, Restock) and operation.quantity > 0:
                restocked[operation.sku] += operation.quantity
        return [
            _block(
                "restock_without_return",
                f"restock of {quantity} x {sku} exceeds the {returned[sku]} returned in this plan",
            )
            for sku, quantity in restocked.items()
            if quantity > returned[sku]
        ]


def _order_ledgers(operations: tuple[AnyOperation, ...]) -> dict[int, _OrderLedger]:
    ledgers: dict[int, _OrderLedger] = {}
    for operation in operations:
        if not isinstance(operation, CreateReturn | IssueRefund):
            continue
        facts = operation.facts
        ledger = ledgers.setdefault(
            operation.order_id,
            _OrderLedger(facts.paid_cents, facts.refunded_cents, facts.returned_value_cents),
        )
        if isinstance(operation, CreateReturn):
            ledger.returned_value += max(operation.quantity, 0) * facts.unit_price_cents
        else:
            amount = max(operation.amount_cents, 0)
            ledger.refunded += amount
            ledger.new_refunds += amount
    return ledgers


def _decide(findings: list[PolicyFinding]) -> PolicyDecision:
    actions = {finding.action for finding in findings}
    action = "block" if "block" in actions else "escalate" if actions else "approve"
    return PolicyDecision(action=action, findings=findings)


def _block(rule: str, message: str, index: int | None = None) -> PolicyFinding:
    return PolicyFinding(rule=rule, action="block", message=message, operation_index=index)


def _escalate(rule: str, message: str) -> PolicyFinding:
    return PolicyFinding(rule=rule, action="escalate", message=message)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"
