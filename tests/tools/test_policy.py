"""Policy rules evaluated against recorded change sets."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ticketideai.schemas.operations import (
    ChangeSet,
    CreateReturn,
    IssueRefund,
    MessageFacts,
    OrderFacts,
    Restock,
    RestockFacts,
    ReturnFacts,
    SendCustomerMessage,
    UpdateOrderStatus,
)
from ticketideai.tools.policy import PolicyEngine, load_policy

NOW = datetime(2026, 11, 17)
REQUESTER = 1
ENGINE = PolicyEngine.from_file()


def order_facts(**overrides: object) -> OrderFacts:
    values = {
        "customer_id": REQUESTER,
        "status": "delivered",
        "ordered_at": NOW - timedelta(days=5),
        "paid_cents": 100_000,
        "refunded_cents": 0,
        "returned_value_cents": 0,
    }
    return OrderFacts(**{**values, **overrides})


def return_op(quantity: int = 1, sku: str = "RIDGE-001", **overrides: object) -> CreateReturn:
    values = {
        **order_facts().model_dump(),
        "category": "tents",
        "purchased_quantity": 4,
        "already_returned": 0,
        "unit_price_cents": 10_000,
    }
    facts = ReturnFacts(**{**values, **overrides})
    return CreateReturn(order_id=1, sku=sku, quantity=quantity, reason="damaged", facts=facts)


def refund_op(cents: int, order_id: int = 1, **overrides: object) -> IssueRefund:
    return IssueRefund(order_id=order_id, amount_cents=cents, facts=order_facts(**overrides))


def restock_op(quantity: int, sku: str = "RIDGE-001") -> Restock:
    return Restock(sku=sku, quantity=quantity, facts=RestockFacts(stock_before=3))


def status_op(new: str, current: str = "delivered") -> UpdateOrderStatus:
    return UpdateOrderStatus(order_id=1, new_status=new, facts=order_facts(status=current))


def evaluate(*operations: object):
    return ENGINE.evaluate(ChangeSet(operations=operations), requester_id=REQUESTER, as_of=NOW)


def test_empty_change_set_is_approved() -> None:
    assert evaluate().action == "approve"


def test_return_with_matching_refund_and_restock_is_approved() -> None:
    decision = evaluate(return_op(), refund_op(10_000), restock_op(1))
    assert decision.action == "approve", decision.reasons


def test_refund_at_the_threshold_is_approved_when_backed_by_returns() -> None:
    decision = evaluate(return_op(2), refund_op(20_000))
    assert decision.action == "approve", decision.reasons


def test_refund_just_above_the_threshold_escalates() -> None:
    decision = evaluate(return_op(3), refund_op(20_001))
    assert decision.action == "escalate"
    assert decision.fired("refund_threshold")


def test_many_small_refunds_are_summed() -> None:
    decision = evaluate(return_op(4), refund_op(15_000), refund_op(15_000), refund_op(15_000))
    assert decision.action == "escalate"
    assert decision.fired("refund_threshold")


def test_prior_refunds_count_toward_the_threshold() -> None:
    decision = evaluate(
        refund_op(5_000, refunded_cents=18_000, returned_value_cents=40_000),
    )
    assert decision.action == "escalate"
    assert decision.fired("refund_threshold")


def test_refunds_across_orders_are_summed_for_the_plan() -> None:
    decision = evaluate(
        refund_op(15_000, order_id=1, returned_value_cents=20_000),
        refund_op(15_000, order_id=2, returned_value_cents=20_000),
    )
    assert decision.action == "escalate"
    assert decision.fired("refund_threshold")


def test_refund_without_a_return_escalates() -> None:
    decision = evaluate(refund_op(1_000))
    assert decision.action == "escalate"
    assert decision.fired("refund_without_return")


def test_refund_above_amount_paid_is_blocked() -> None:
    decision = evaluate(refund_op(50_000, paid_cents=40_000))
    assert decision.action == "block"
    assert decision.fired("refund_exceeds_paid")


def test_prior_refunds_count_toward_amount_paid() -> None:
    decision = evaluate(refund_op(10_000, paid_cents=40_000, refunded_cents=35_000))
    assert decision.action == "block"
    assert decision.fired("refund_exceeds_paid")


@pytest.mark.parametrize("cents", [0, -500])
def test_refund_must_be_positive(cents: int) -> None:
    assert evaluate(refund_op(cents)).fired("refund_positive")


def test_return_on_the_last_day_of_the_window_is_allowed() -> None:
    decision = evaluate(return_op(ordered_at=NOW - timedelta(days=30)))
    assert decision.action == "approve", decision.reasons


def test_return_after_the_window_is_blocked() -> None:
    decision = evaluate(return_op(ordered_at=NOW - timedelta(days=30, seconds=1)))
    assert decision.action == "block"
    assert decision.fired("return_window")


def test_non_returnable_category_is_blocked() -> None:
    assert evaluate(return_op(category="Clearance")).fired("non_returnable")


@pytest.mark.parametrize("status", ["placed", "processing", "shipped", "cancelled", "returned"])
def test_undelivered_orders_cannot_be_returned(status: str) -> None:
    assert evaluate(return_op(status=status)).fired("return_status")


def test_return_quantity_counts_units_already_returned() -> None:
    allowed = evaluate(return_op(2, already_returned=2))
    blocked = evaluate(return_op(3, already_returned=2))
    assert allowed.action == "approve", allowed.reasons
    assert blocked.fired("return_quantity")


@pytest.mark.parametrize("quantity", [0, -1])
def test_return_quantity_must_be_positive(quantity: int) -> None:
    assert evaluate(return_op(quantity)).fired("return_quantity")


@pytest.mark.parametrize("quantity", [0, -5])
def test_restock_must_be_positive(quantity: int) -> None:
    assert evaluate(restock_op(quantity)).fired("restock_positive")


def test_restock_must_be_backed_by_a_return_in_the_same_plan() -> None:
    assert evaluate(restock_op(1)).fired("restock_without_return")
    assert evaluate(return_op(1), restock_op(2)).fired("restock_without_return")
    assert evaluate(return_op(1, sku="RIDGE-002"), restock_op(1)).fired("restock_without_return")


@pytest.mark.parametrize(
    ("current", "new"),
    [("placed", "cancelled"), ("processing", "cancelled"), ("delivered", "returned")],
)
def test_valid_status_transitions_are_approved(current: str, new: str) -> None:
    decision = evaluate(status_op(new, current))
    assert decision.action == "approve", decision.reasons


@pytest.mark.parametrize(
    ("current", "new"),
    [("delivered", "cancelled"), ("shipped", "returned"), ("cancelled", "returned")],
)
def test_invalid_status_transitions_are_blocked(current: str, new: str) -> None:
    assert evaluate(status_op(new, current)).fired("status_transition")


@pytest.mark.parametrize("new", ["processing", "shipped", "delivered", "made-up"])
def test_support_cannot_set_fulfilment_statuses(new: str) -> None:
    assert evaluate(status_op(new, "placed")).fired("status_not_settable")


def test_operations_on_another_customers_order_are_blocked() -> None:
    assert evaluate(refund_op(1_000, customer_id=2)).fired("ownership")
    assert evaluate(return_op(customer_id=2)).fired("ownership")


def test_messages_to_another_customer_are_blocked() -> None:
    operation = SendCustomerMessage(
        customer_email="other@example.com", body="hi", facts=MessageFacts(customer_id=2)
    )
    assert evaluate(operation).fired("ownership")


def test_operation_cap_blocks_long_plans() -> None:
    cap = load_policy().max_operations_per_plan
    message = SendCustomerMessage(
        customer_email="a@example.com", body="hi", facts=MessageFacts(customer_id=REQUESTER)
    )
    assert evaluate(*[message] * cap).action == "approve"
    assert evaluate(*[message] * (cap + 1)).fired("operation_cap")


def test_a_block_outranks_an_escalation() -> None:
    decision = evaluate(return_op(3), refund_op(25_000), restock_op(99))
    assert decision.fired("refund_threshold")
    assert decision.action == "block"
