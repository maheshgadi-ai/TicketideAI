"""Observe store effects and compare them with a scenario's expected effects."""

from __future__ import annotations

from ticketideai.schemas.agent_evals import AgentScenario, ReturnEffect, StateEffects
from ticketideai.schemas.operations import (
    ChangeSet,
    CreateReturn,
    IssueRefund,
    Restock,
    UpdateOrderStatus,
)
from ticketideai.schemas.workflow import Outcome
from ticketideai.tools.diffing import Snapshot


def effects_from_snapshots(before: Snapshot, after: Snapshot) -> StateEffects:
    """Describe what actually changed in the store."""
    effects = StateEffects()
    for key in after["returns"].keys() - before["returns"].keys():
        row = after["returns"][key]
        effects.returns.append(
            ReturnEffect(order=row["order_id"], sku=row["sku"], quantity=row["quantity"])
        )
    for key in after["refunds"].keys() - before["refunds"].keys():
        row = after["refunds"][key]
        effects.refunds[row["order_id"]] = effects.refunds.get(row["order_id"], 0) + row["amount"]
    for sku, row in after["products"].items():
        delta = row["stock"] - before["products"][sku]["stock"]
        if delta:
            effects.stock[sku] = delta
    for key, row in after["orders"].items():
        if row["status"] != before["orders"][key]["status"]:
            effects.status[row["id"]] = row["status"]
    return effects


def effects_from_change_set(change_set: ChangeSet) -> StateEffects:
    """Describe what a change set proposes."""
    effects = StateEffects()
    for operation in change_set.operations:
        if isinstance(operation, CreateReturn):
            effects.returns.append(
                ReturnEffect(
                    order=operation.order_id, sku=operation.sku, quantity=operation.quantity
                )
            )
        elif isinstance(operation, IssueRefund):
            total = effects.refunds.get(operation.order_id, 0) + operation.amount_cents / 100
            effects.refunds[operation.order_id] = total
        elif isinstance(operation, Restock):
            effects.stock[operation.sku] = effects.stock.get(operation.sku, 0) + operation.quantity
        elif isinstance(operation, UpdateOrderStatus):
            effects.status[operation.order_id] = operation.new_status
    return effects


def state_matches(
    scenario: AgentScenario,
    committed: StateEffects,
    pending: list[ChangeSet],
    outcome: Outcome | None = None,
) -> bool:
    """Check the final store and queue; accepted alternative outcomes must change nothing."""
    expected = scenario.effects.normalized()
    if outcome in scenario.also_accepted:
        return committed.is_empty and not pending
    if scenario.expected_outcome == "committed":
        return committed.normalized() == expected and not pending
    if scenario.expected_outcome == "escalated":
        return (
            committed.is_empty
            and len(pending) == 1
            and effects_from_change_set(pending[0]).normalized() == expected
        )
    return committed.is_empty and not pending
