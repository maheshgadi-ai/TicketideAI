"""End-to-end workflow tests with scripted models and the real sandbox and policy."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tests.support import (
    CUSTOMER_1,
    CUSTOMER_11,
    FAST_LIMITS,
    build_workflow,
    handle,
    plan_source,
)
from ticketideai.llm import LLMError
from ticketideai.schemas.store import SupportRequest
from ticketideai.tools.diffing import snapshot, table_diff
from ticketideai.tools.sandbox import SandboxExecutor, SandboxResult
from ticketideai.tools.store_api import StoreAPI

RETURN_AND_REFUND = plan_source(
    'api.create_return(1, "RIDGE-001", 1, "damaged")', "api.issue_refund(1, 25.0)"
)
LARGE_REFUND = plan_source(
    'api.create_return(11, "RIDGE-034", 1, "damaged")', "api.issue_refund(11, 250.0)"
)


def refund_rows(path: Path) -> set[str]:
    return set(snapshot(path)["refunds"])


def test_policy_compliant_plan_commits_exactly_the_dry_run(
    store_url: str, store_path: Path
) -> None:
    before = snapshot(store_path)
    result = handle(store_url, RETURN_AND_REFUND)
    assert result.outcome == "committed"
    assert table_diff(before, snapshot(store_path)) == result.diff


def test_read_only_plan_is_answered_not_committed(store_url: str, store_path: Path) -> None:
    before = snapshot(store_path)
    source = 'def plan(api):\n    return {"outcome": "done", "status": api.get_order(1).status}\n'
    result = handle(store_url, source)
    assert result.outcome == "answered"
    assert not result.committed
    assert snapshot(store_path) == before


@pytest.mark.parametrize(
    "lines",
    [
        ["for _ in range(3):", "    api.issue_refund(11, 150)"],
        ["amount = 100 + 150", "api.issue_refund(11, amount)"],
        ["amounts = [120, 130]", "for amount in amounts:", "    api.issue_refund(11, amount)"],
        ["line = api.get_order(11).lines[0]", "api.issue_refund(11, line.unit_price)"],
        ["total = 0", "for i in range(5):", "    total += 60", "api.issue_refund(11, total)"],
        ["api.issue_refund(11, float(str(201)))"],
    ],
    ids=["loop", "computed", "list", "from_order_data", "accumulated", "converted"],
)
def test_refund_bypasses_escalate_and_write_nothing(
    store_url: str, store_path: Path, lines: list[str]
) -> None:
    result = handle(store_url, plan_source(*lines), email=CUSTOMER_11)
    assert result.outcome == "escalated", result.errors
    assert result.approval_id is not None
    assert any(finding.rule == "refund_threshold" for finding in result.findings)
    assert refund_rows(store_path) == {"1", "2"}


def test_loop_refund_above_amount_paid_is_refused(store_url: str, store_path: Path) -> None:
    source = plan_source("for _ in range(9):", "    api.issue_refund(11, 150)")
    result = handle(store_url, source, email=CUSTOMER_11)
    assert result.outcome == "refused"
    assert any(finding.rule == "refund_exceeds_paid" for finding in result.findings)
    assert refund_rows(store_path) == {"1", "2"}


def test_refunds_split_across_requests_still_escalate(store_url: str, store_path: Path) -> None:
    first = plan_source(
        'api.create_return(11, "RIDGE-034", 2, "damaged")', "api.issue_refund(11, 150)"
    )
    assert handle(store_url, first, email=CUSTOMER_11).outcome == "committed"
    second = handle(store_url, plan_source("api.issue_refund(11, 150)"), email=CUSTOMER_11)
    assert second.outcome == "escalated"
    assert len(refund_rows(store_path)) == 3


def test_blocked_plan_is_refused_without_reflection(store_url: str, store_path: Path) -> None:
    workflow = build_workflow(
        store_url, [plan_source('api.create_return(13, "RIDGE-005", 1, "late")')]
    )
    result = workflow.handle(SupportRequest(customer_email=CUSTOMER_1, message="return"))
    assert result.outcome == "refused"
    assert result.reflection_attempts == 0
    assert len(workflow.planner.provider.requests) == 1


def test_failed_plan_is_corrected_by_reflection(store_url: str) -> None:
    workflow = build_workflow(store_url, ["import os\n", RETURN_AND_REFUND])
    result = workflow.handle(SupportRequest(customer_email=CUSTOMER_1, message="return"))
    assert result.outcome == "committed"
    assert result.reflection_attempts == 1
    assert "reflection_error" in workflow.planner.provider.requests[1]
    assert "forbidden" in workflow.planner.provider.requests[1]


def test_reflection_stops_after_two_corrections(store_url: str, store_path: Path) -> None:
    before = snapshot(store_path)
    workflow = build_workflow(store_url, ["import os\n"])
    result = workflow.handle(SupportRequest(customer_email=CUSTOMER_1, message="return"))
    assert result.outcome == "failed_safely"
    assert len(workflow.planner.provider.requests) == 3
    assert snapshot(store_path) == before


class ChangingSandbox(SandboxExecutor):
    """Simulate another writer changing the store right after the first dry run."""

    def __init__(self, path: Path) -> None:
        super().__init__(FAST_LIMITS)
        self.path = path
        self.runs = 0

    def execute(self, source: str, database_url: str, customer_email: str) -> SandboxResult:
        result = super().execute(source, database_url, customer_email)
        self.runs += 1
        if self.runs == 1:
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute(
                    "INSERT INTO returns (order_id, sku, quantity, reason, status) "
                    "VALUES (1, 'RIDGE-006', 1, 'other item', 'approved')"
                )
                connection.commit()
        return result


def test_state_change_before_commit_aborts_and_replans(store_url: str, store_path: Path) -> None:
    workflow = build_workflow(store_url, [RETURN_AND_REFUND], sandbox=ChangingSandbox(store_path))
    result = workflow.handle(SupportRequest(customer_email=CUSTOMER_1, message="return"))
    assert result.outcome == "committed"
    assert result.reflection_attempts == 1
    assert any("state changed, re-plan" in error for error in result.errors)
    assert len(workflow.planner.provider.requests) == 2


def test_clarifying_plan_writes_nothing(store_url: str, store_path: Path) -> None:
    before = snapshot(store_path)
    source = (
        "def plan(api):\n"
        "    api.issue_refund(1, 5.0)\n"
        '    return {"outcome": "clarify", "question": "Which order?"}\n'
    )
    result = handle(store_url, source)
    assert result.outcome == "needs_clarification"
    assert result.change_set.is_empty
    assert snapshot(store_path) == before


def test_refusing_plan_writes_nothing(store_url: str, store_path: Path) -> None:
    before = snapshot(store_path)
    source = 'def plan(api):\n    return {"outcome": "refuse", "reason": "Not allowed."}\n'
    assert handle(store_url, source).outcome == "refused"
    assert snapshot(store_path) == before


def test_unknown_customer_is_asked_to_clarify_without_planning(store_url: str) -> None:
    workflow = build_workflow(store_url, [RETURN_AND_REFUND])
    result = workflow.handle(SupportRequest(customer_email="nobody@example.com", message="hi"))
    assert result.outcome == "needs_clarification"
    assert workflow.planner.provider.requests == []


def test_planner_sees_only_the_requesters_orders(store_url: str) -> None:
    workflow = build_workflow(store_url, [RETURN_AND_REFUND])
    workflow.handle(SupportRequest(customer_email=CUSTOMER_1, message="hi"))
    request = workflow.planner.provider.requests[0]
    assert '"order_id":16' in request
    assert '"order_id":11' not in request


def test_reply_claiming_a_pending_refund_is_done_is_replaced(store_url: str) -> None:
    workflow = build_workflow(
        store_url, [LARGE_REFUND], replies=[{"body": "Your refund of $250.00 has been issued!"}]
    )
    result = workflow.handle(SupportRequest(customer_email=CUSTOMER_11, message="refund"))
    assert result.outcome == "escalated"
    assert result.reply_replaced_by_guard
    assert "has been issued" not in result.reply
    assert "review" in result.reply


def test_grounded_reply_is_kept(store_url: str) -> None:
    body = "Your $250.00 refund is waiting for review by our team."
    workflow = build_workflow(store_url, [LARGE_REFUND], replies=[{"body": body}])
    result = workflow.handle(SupportRequest(customer_email=CUSTOMER_11, message="refund"))
    assert result.reply == body
    assert not result.reply_replaced_by_guard


def test_responder_failure_falls_back_to_a_safe_template(store_url: str) -> None:
    workflow = build_workflow(store_url, [LARGE_REFUND], replies=[LLMError("provider down")])
    result = workflow.handle(SupportRequest(customer_email=CUSTOMER_11, message="refund"))
    assert result.outcome == "escalated"
    assert "waiting for review" in result.reply


def test_store_is_unchanged_for_escalated_plans(store_url: str) -> None:
    handle(store_url, LARGE_REFUND, email=CUSTOMER_11)
    assert StoreAPI(store_url).get_order(11).status == "delivered"
