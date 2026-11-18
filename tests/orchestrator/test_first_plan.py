"""Typical first plans must validate and run without needing a correction."""

from __future__ import annotations

import re

import pytest

from tests.support import CUSTOMER_1, FAST_LIMITS, handle
from ticketideai.agents.planner import PROMPT_PATH as PLANNER_PROMPT
from ticketideai.agents.responder import PROMPT_PATH as RESPONDER_PROMPT
from ticketideai.schemas.workflow import ReplyFacts
from ticketideai.tools.reply_guard import reply_problems
from ticketideai.tools.sandbox import SandboxExecutor
from ticketideai.tools.validator import PlanValidator

CANCEL = """def plan(api):
    order = api.get_order(6)
    customer = api.find_customer("customer6@example.com")
    if order.customer_id != customer.id:
        return {"outcome": "refuse", "reason": "Order not found."}
    if order.status not in ("placed", "processing"):
        return {"outcome": "refuse", "reason": "This order cannot be cancelled."}
    api.update_order_status(6, "cancelled")
    return {"outcome": "done", "summary": "Order 6 cancelled."}
"""
LOOKUP = """def plan(api):
    order = api.get_order(8)
    status = order.status
    return {"outcome": "done", "summary": f"Order 8 is {status}.", "ordered": str(order.ordered_at)}
"""
RETURN_ALL = """def plan(api):
    order = api.get_order(12)
    total = 0.0
    for line in order.lines:
        api.create_return(12, line.sku, line.quantity, "unused")
        api.restock(line.sku, line.quantity)
        total += line.quantity * line.unit_price
    api.issue_refund(12, round(total, 2))
    api.update_order_status(12, "returned")
    return {"outcome": "done", "summary": f"Returned the order for {round(total, 2)}."}
"""
GOODWILL = """def plan(api):
    order = api.get_order(15)
    paid = 0.0
    for line in order.lines:
        paid += line.quantity * line.unit_price
    if 20 > paid:
        return {"outcome": "refuse", "reason": "That is more than was paid."}
    api.issue_refund(15, 20)
    return {"outcome": "done", "summary": "Proposed a $20 goodwill refund."}
"""


def prompt_example() -> str:
    text = PLANNER_PROMPT.read_text(encoding="utf-8")
    return re.search(r"```python\n(.*?)```", text, re.DOTALL).group(1)


@pytest.mark.parametrize(
    ("source", "email", "outcome"),
    [
        (CANCEL, "customer6@example.com", "committed"),
        (LOOKUP, "customer8@example.com", "answered"),
        (RETURN_ALL, "customer12@example.com", "escalated"),
        (GOODWILL, "customer3@example.com", "escalated"),
    ],
    ids=["cancel", "lookup", "return_whole_order", "goodwill"],
)
def test_typical_first_plan_succeeds_without_reflection(
    store_url: str, source: str, email: str, outcome: str
) -> None:
    PlanValidator().validate_or_raise(source)
    result = handle(store_url, source, email=email)
    assert result.outcome == outcome, result.errors
    assert result.reflection_attempts == 0
    assert result.errors == []


def test_the_prompts_example_plan_validates_and_runs(store_url: str) -> None:
    result = SandboxExecutor(FAST_LIMITS).execute(prompt_example(), store_url, CUSTOMER_1)
    assert result.success, result.error
    assert result.result["summary"] == "Order 16 is delivered with 1 unit(s)."


def test_the_prompt_explains_attribute_access() -> None:
    text = PLANNER_PROMPT.read_text(encoding="utf-8")
    assert "`order.status`" in text
    assert 'Never index them: `order["status"]` fails.' in text


def test_indexing_an_api_result_is_the_failure_the_prompt_warns_about(store_url: str) -> None:
    source = 'def plan(api):\n    return {"status": api.get_order(1)["status"]}\n'
    result = SandboxExecutor(FAST_LIMITS).execute(source, store_url, CUSTOMER_1)
    assert not result.success
    assert "not subscriptable" in result.error


def test_the_responders_pending_examples_pass_the_guard() -> None:
    text = RESPONDER_PROMPT.read_text(encoding="utf-8")
    examples = re.findall(r'"(Your [^"]+pending review by our team\.)"', text)
    facts = ReplyFacts(
        outcome="escalated",
        customer_message="refund",
        refunds_pending_approval=[{"order_id": 2, "amount": 238.5}],
        returns_pending_approval=[{"order_id": 2, "sku": "RIDGE-014", "quantity": 2}],
    )
    assert len(examples) == 2
    filled = [example.replace("<amount>", "238.50") for example in examples]
    assert all(reply_problems(example, facts) == [] for example in filled)
