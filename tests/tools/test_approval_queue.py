"""Approval queue and human approval service tests."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from tests.support import CUSTOMER_11, FAST_LIMITS, plan_source

from ticketideai.orchestrator.approvals import ApprovalService
from ticketideai.schemas.store import ApprovalItem
from ticketideai.schemas.workflow import ReplyFacts
from ticketideai.tools.approval_queue import ApprovalError, ApprovalQueue
from ticketideai.tools.diffing import render_change_set, snapshot, table_diff
from ticketideai.tools.notifications import rejected_notice, stale_notice
from ticketideai.tools.reply_guard import reply_problems
from ticketideai.tools.sandbox import SandboxExecutor
from ticketideai.tools.seed_store import SEED_DATE
from ticketideai.tools.store_api import StoreAPI

REFUND_PLAN = plan_source(
    'api.create_return(11, "RIDGE-034", 1, "damaged")', "api.issue_refund(11, 250.0)"
)


@pytest.fixture()
def service(store_url: str) -> ApprovalService:
    return ApprovalService(store_url, clock=lambda: SEED_DATE)


@pytest.fixture()
def item(store_url: str, service: ApprovalService) -> ApprovalItem:
    run = SandboxExecutor(FAST_LIMITS).execute(REFUND_PLAN, store_url, CUSTOMER_11)
    assert run.success, run.error
    return service.queue.enqueue(
        customer_email=CUSTOMER_11,
        message="refund please",
        plan_source=REFUND_PLAN,
        reasons=["above threshold"],
        change_set=run.change_set,
        diff=run.diff,
        diff_text=render_change_set(run.change_set),
    )


def execute_sql(path: Path, statement: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(statement)
        connection.commit()


def test_queue_stores_the_change_set_and_readable_diff(item: ApprovalItem) -> None:
    assert item.status == "pending"
    assert len(item.change_set.operations) == 2
    assert "Refund $250.00 on order 11" in item.diff_text
    assert item.change_set.digest() == item.change_set_digest


def test_nothing_is_written_until_a_human_approves(
    store_path: Path, service: ApprovalService, item: ApprovalItem
) -> None:
    assert snapshot(store_path)["refunds"].keys() == {"1", "2"}
    assert service.queue.list_pending()[0].id == item.id


def test_approval_applies_exactly_the_reviewed_change_set(
    store_path: Path, service: ApprovalService, item: ApprovalItem
) -> None:
    before = snapshot(store_path)
    outcome = service.approve(item.id, "Dana")
    assert outcome.committed
    assert outcome.item.status == "approved"
    assert outcome.item.reviewer == "Dana"
    actual = table_diff(before, snapshot(store_path))["tables"]
    assert actual.pop("messages")["added"][0]["body"] == outcome.item.customer_notice
    assert actual == item.diff["tables"]


def test_approval_aborts_when_state_changed(
    store_path: Path, service: ApprovalService, item: ApprovalItem
) -> None:
    execute_sql(
        store_path, "INSERT INTO refunds (order_id, amount, status) VALUES (11, 1, 'issued')"
    )
    outcome = service.approve(item.id, "Dana")
    assert not outcome.committed
    assert outcome.item.status == "stale"
    assert "state changed, re-plan" in outcome.message
    assert snapshot(store_path)["returns"].keys() == {"1", "2"}


def test_approval_rechecks_time_based_policy(store_url: str, item: ApprovalItem) -> None:
    late = ApprovalService(store_url, clock=lambda: SEED_DATE + timedelta(days=60))
    outcome = late.approve(item.id, "Dana")
    assert not outcome.committed
    assert outcome.item.status == "stale"


def test_an_item_cannot_be_approved_twice(
    store_path: Path, service: ApprovalService, item: ApprovalItem
) -> None:
    service.approve(item.id, "Dana")
    refunds = snapshot(store_path)["refunds"]
    with pytest.raises(ApprovalError, match="already approved"):
        service.queue.commit_approved(item.id, "Sam")
    assert snapshot(store_path)["refunds"] == refunds


def test_rejected_item_cannot_be_approved(
    store_url: str, service: ApprovalService, item: ApprovalItem
) -> None:
    assert service.reject(item.id, "Dana").item.status == "rejected"
    with pytest.raises(ApprovalError, match="already rejected"):
        service.approve(item.id, "Sam")
    assert StoreAPI(store_url).get_order(11).status == "delivered"


def test_tampered_change_set_fails_the_integrity_check(
    store_path: Path, service: ApprovalService, item: ApprovalItem
) -> None:
    execute_sql(
        store_path,
        "UPDATE approval_queue SET change_set_json = replace(change_set_json, '25000', '75000')",
    )
    with pytest.raises(ApprovalError, match="integrity"):
        service.approve(item.id, "Dana")
    assert snapshot(store_path)["refunds"].keys() == {"1", "2"}


@pytest.mark.parametrize("reviewer", ["", "   "])
def test_a_named_reviewer_is_required(
    service: ApprovalService, item: ApprovalItem, reviewer: str
) -> None:
    with pytest.raises(ApprovalError, match="reviewer"):
        service.approve(item.id, reviewer)


def test_missing_item_is_reported(store_url: str) -> None:
    queue = ApprovalQueue(store_url)
    queue.initialize()
    with pytest.raises(ApprovalError, match="not found"):
        queue.get(404)


def customer_messages(path: Path) -> list[str]:
    return [row["body"] for row in snapshot(path)["messages"].values()]


def test_approval_notifies_the_customer_of_exactly_what_was_committed(
    store_path: Path, service: ApprovalService, item: ApprovalItem
) -> None:
    notice = service.approve(item.id, "Dana").item.customer_notice
    assert notice == (
        "A team member approved your request. Return of 1 x RIDGE-034 from order 11 opened. "
        "Refund of $250.00 on order 11 issued."
    )
    assert customer_messages(store_path) == [notice]


def test_rejection_notifies_the_customer_that_nothing_changed(
    store_path: Path, service: ApprovalService, item: ApprovalItem
) -> None:
    notice = service.reject(item.id, "Dana").item.customer_notice
    assert "could not approve" in notice
    assert "$250" not in notice
    assert customer_messages(store_path) == [notice]
    assert snapshot(store_path)["refunds"].keys() == {"1", "2"}


def test_stale_approval_notifies_the_customer_that_nothing_changed(
    store_path: Path, service: ApprovalService, item: ApprovalItem
) -> None:
    execute_sql(
        store_path, "INSERT INTO refunds (order_id, amount, status) VALUES (11, 1, 'issued')"
    )
    notice = service.approve(item.id, "Dana").item.customer_notice
    assert "order changed" in notice
    assert customer_messages(store_path) == [notice]


def test_a_failed_approval_writes_no_notice_with_the_rollback(
    store_path: Path, service: ApprovalService, item: ApprovalItem
) -> None:
    execute_sql(store_path, "UPDATE approval_queue SET change_set_digest = 'tampered'")
    with pytest.raises(ApprovalError):
        service.approve(item.id, "Dana")
    assert customer_messages(store_path) == []


@pytest.mark.parametrize("build", [rejected_notice, stale_notice])
def test_decline_notices_never_claim_completion(build) -> None:
    facts = ReplyFacts(outcome="refused", customer_message="refund")
    assert reply_problems(build(), facts) == []
