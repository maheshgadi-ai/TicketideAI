"""Commit must equal the dry run, and must abort when the store has changed."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from tests.support import CUSTOMER_1, FAST_LIMITS, plan_source

from ticketideai.schemas.operations import ChangeSet, IssueRefund
from ticketideai.tools.diffing import snapshot, table_diff
from ticketideai.tools.effects import commit_change_set
from ticketideai.tools.errors import PreconditionFailed
from ticketideai.tools.sandbox import SandboxExecutor, SandboxResult, _Frame, _verified

FULL_PLAN = plan_source(
    'api.create_return(1, "RIDGE-001", 2, "damaged")',
    "api.issue_refund(1, 30.0)",
    "api.issue_refund(1, 20.0)",
    'api.restock("RIDGE-001", 2)',
    f'api.send_customer_message("{CUSTOMER_1}", "Your return is open.")',
    'api.update_order_status(1, "returned")',
)


def dry_run(url: str, source: str = FULL_PLAN) -> SandboxResult:
    result = SandboxExecutor(FAST_LIMITS).execute(source, url, CUSTOMER_1)
    assert result.success, result.error
    return result


def execute_sql(path: Path, statement: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(statement)
        connection.commit()


def test_commit_produces_exactly_the_dry_run_diff(store_url: str, store_path: Path) -> None:
    before = snapshot(store_path)
    run = dry_run(store_url)
    commit_change_set(store_url, run.change_set)
    assert table_diff(before, snapshot(store_path)) == run.diff
    assert set(run.diff["tables"]) == {
        "messages",
        "orders",
        "products",
        "refunds",
        "restocks",
        "returns",
    }


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE orders SET status = 'cancelled' WHERE id = 1",
        "UPDATE orders SET customer_id = 2 WHERE id = 1",
        "UPDATE orders SET ordered_at = '2020-01-01 00:00:00.000000' WHERE id = 1",
        "INSERT INTO refunds (order_id, amount, status) VALUES (1, 5.0, 'issued')",
        "INSERT INTO returns (order_id, sku, quantity, reason, status) "
        "VALUES (1, 'RIDGE-001', 1, 'x', 'approved')",
        "UPDATE order_lines SET quantity = 1 WHERE order_id = 1 AND sku = 'RIDGE-001'",
        "UPDATE order_lines SET unit_price = 1.0 WHERE order_id = 1 AND sku = 'RIDGE-001'",
        "UPDATE products SET stock = 99 WHERE sku = 'RIDGE-001'",
        "UPDATE products SET category = 'clearance' WHERE sku = 'RIDGE-001'",
        "DELETE FROM customers WHERE id = 1",
    ],
)
def test_changed_state_aborts_the_whole_commit(
    store_url: str, store_path: Path, statement: str
) -> None:
    run = dry_run(store_url)
    execute_sql(store_path, statement)
    changed = snapshot(store_path)
    with pytest.raises(PreconditionFailed):
        commit_change_set(store_url, run.change_set)
    assert snapshot(store_path) == changed


def test_a_later_failure_rolls_back_earlier_operations(store_url: str, store_path: Path) -> None:
    run = dry_run(store_url)
    execute_sql(store_path, "UPDATE products SET stock = 99 WHERE sku = 'RIDGE-001'")
    with pytest.raises(PreconditionFailed):
        commit_change_set(store_url, run.change_set)
    assert snapshot(store_path)["refunds"].keys() == {"1", "2"}
    assert "3" not in snapshot(store_path)["returns"]


def test_a_change_set_cannot_be_committed_twice(store_url: str) -> None:
    run = dry_run(store_url)
    commit_change_set(store_url, run.change_set)
    with pytest.raises(PreconditionFailed):
        commit_change_set(store_url, run.change_set)


def test_forged_preconditions_fail_trusted_verification(store_url: str, store_path: Path) -> None:
    run = dry_run(store_url, plan_source("api.issue_refund(5 - 4, 10.0)"))
    original = run.change_set.operations[0]
    assert isinstance(original, IssueRefund)
    forged_facts = original.facts.model_copy(update={"paid_cents": 99_999_999})
    forged = ChangeSet(operations=(original.model_copy(update={"facts": forged_facts}),))
    result = _verified(_Frame(ok=True, change_set=forged), store_path)
    assert result.error_kind == "verification"
    with pytest.raises(PreconditionFailed):
        commit_change_set(store_url, forged)
